import io
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest

import numpy as np
from pymavlink.dialects.v20 import ardupilotmega as mav

from tuner_analysis import from_bin, from_telemetry, rate_metrics, spectrum
from tuner_parameters import Change, ParameterSession, TunerEndpoint, TunerError
from tools.tuner_sitl import compare

R, P, Y = ("ATC_RAT_RLL_P", "ATC_RAT_PIT_P", "ATC_RAT_YAW_P")


class AnalysisTests(unittest.TestCase):
    def test_metrics_and_missing_input(self):
        t = np.arange(301) / 100
        target = np.where(t >= 1, -30, 0)
        actual = target * (1-np.exp(-np.maximum(t-1, 0)/.1))
        result = rate_metrics(np.column_stack((t, target, actual)))
        self.assertEqual(result["steps"], 1)
        self.assertEqual(result["overshoot"], 0)
        self.assertAlmostEqual(result["settling"], .30, places=2)
        self.assertGreater(result["rms"], 0)
        self.assertIsNone(rate_metrics(np.column_stack((t, t*0, actual)))["rms"])
        self.assertIsNone(rate_metrics(np.column_stack((t[::20], target[::20], actual[::20])))["rms"])

    def test_fft_uses_sample_rate(self):
        signal = np.sin(2*np.pi*60*np.arange(1200)/600)
        result = spectrum(signal, 600)
        self.assertAlmostEqual(result["peaks"][0], 60)
        self.assertAlmostEqual(result["magnitude"][120], 1, places=3)
        self.assertIsNone(spectrum(signal[:10], 600))

    def test_mask_and_stale_target(self):
        records = []
        for i in range(100):
            t = i*.02
            records.extend([("target", t, t, (None, t, t)), ("actual", t, t, (10., t, t))])
        records.append(("actual", 9, 9, (10., 10., 10.)))
        data = from_telemetry(records)["rates"]
        self.assertEqual(len(data["Roll"][0]), 0)
        self.assertEqual(len(data["Pitch"][0]), 100)
        self.assertEqual(data["Yaw"][1]["rms"], 0)

    def test_real_dfreader_bin_formats(self):
        stream = io.BytesIO()

        def fmt(mid, name, data_format, labels, wire):
            stream.write(b"\xa3\x95\x80" + struct.pack("<BB4s16s64s", mid, 3+struct.calcsize(wire),
                         name.encode(), data_format.encode(), labels.encode()))

        def frame(mid, wire, *values):
            stream.write(b"\xa3\x95" + bytes([mid]) + struct.pack(wire, *values))

        fmt(1, "MSG", "QZ", "TimeUS,Message", "<Q64s")
        fmt(2, "ARM", "QB", "TimeUS,ArmState", "<QB")
        fmt(3, "RATE", "Qffffff", "TimeUS,RDes,R,PDes,P,YDes,Y", "<Q6f")
        fmt(4, "GYR", "QBQfff", "TimeUS,I,SampleUS,GyrX,GyrY,GyrZ", "<QBQ3f")
        fmt(5, "PARM", "QNf", "TimeUS,Name,Value", "<Q16sf")
        frame(1, "<Q64s", 1000, b"ArduCopter 4.6.0")
        frame(5, "<Q16sf", 1500, R.encode(), .125)
        frame(2, "<QB", 2000, 1)
        for i in range(300):
            target = 30 if i >= 100 else 0
            frame(3, "<Q6f", 10_000+i*10_000, target, target*.9, target, target, 0., 0.)
            stamp = 10_000+i*2000
            frame(4, "<QBQ3f", stamp, 0, stamp, np.sin(2*np.pi*50*i/500), 0., 0.)
        frame(2, "<QB", 3_100_000, 0)
        frame(3, "<Q6f", 3_110_000, 500, 0, 0, 0, 0, 0)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.BIN"
            path.write_bytes(stream.getvalue())
            data = from_bin(path)
            candidate = Path(folder) / "candidate.BIN"
            before = struct.pack("<Q16sf", 1500, R.encode(), .125)
            after = struct.pack("<Q16sf", 1500, R.encode(), .15625)
            candidate.write_bytes(stream.getvalue().replace(before, after))
            comparison = compare(path, candidate)
        self.assertEqual(data["firmware"], "ArduCopter 4.6.0")
        self.assertEqual(data["rates"]["Roll"][1]["steps"], 1)
        self.assertIsNone(data["rates"]["Yaw"][1]["rms"])
        self.assertAlmostEqual(data["spectra"]["Gyro 0 X (rad/s)"]["peaks"][0], 50)
        self.assertEqual(comparison["logged_gain_changes"], {R: [.125, .15625]})
        self.assertEqual(comparison["status"], "operator_review_required")


class MockLink:
    def __init__(self):
        self.values = {R: .125, P: .25, Y: .375}
        self.commands, self.writes = [], []
        self.bad = None
        self.wrong_source = False
        self.disconnect_after_set = False
        self.arm_on_ground_reply = False
        self.board_uid = 42
        self.link = SimpleNamespace(_running=True, master=object(), kind="tcp")
        self.vehicle = SimpleNamespace(sysid=17, compid=1, connected=True, link=self.link,
                                       mav_type=mav.MAV_TYPE_QUADROTOR,
                                       autopilot=mav.MAV_AUTOPILOT_ARDUPILOTMEGA,
                                       master=SimpleNamespace(mav=self))
        self.endpoint = TunerEndpoint(self.vehicle)
        self.heartbeat(armed=False)

    def emit(self, message, src=17, comp=1):
        message._header.srcSystem = src
        message._header.srcComponent = comp
        self.endpoint.observe(message)

    def heartbeat(self, armed):
        self.emit(mav.MAVLink_heartbeat_message(
            self.vehicle.mav_type, self.vehicle.autopilot,
            mav.MAV_MODE_FLAG_SAFETY_ARMED if armed else 0, 0, 3, 3))

    def command_long_send(self, system, component, command, _, message_id, *params):
        self.commands.append((system, component, command))
        assert command == mav.MAV_CMD_REQUEST_MESSAGE
        if message_id == mav.MAVLINK_MSG_ID_AUTOPILOT_VERSION:
            self.emit(mav.MAVLink_autopilot_version_message(
                0, 0x040600ff, 0, 0, 9, list(b"testhash"), [0]*8, [0]*8, 0, 0, self.board_uid))
        elif message_id == mav.MAVLINK_MSG_ID_EXTENDED_SYS_STATE:
            self.emit(mav.MAVLink_extended_sys_state_message(0, mav.MAV_LANDED_STATE_ON_GROUND))
            if self.arm_on_ground_reply:
                self.heartbeat(armed=True)

    def param_request_read_send(self, system, component, name, index):
        self.commands.append((system, component, "read"))
        value = self.values.get(name.decode())
        if value is not None:
            self.emit(mav.MAVLink_param_value_message(name, value, mav.MAV_PARAM_TYPE_REAL32, 3, 0),
                      src=18 if self.wrong_source else 17)

    def param_set_send(self, system, component, name, value, kind):
        self.commands.append((system, component, "set"))
        self.writes.append(name.decode())
        if name.decode() != self.bad:
            self.values[name.decode()] = value
        if self.disconnect_after_set:
            self.link.master = None
        self.emit(mav.MAVLink_param_value_message(name, self.values[name.decode()], kind, 3, 0))

    def session(self):
        return ParameterSession(self.endpoint, lambda: True, timeout=.01)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.wire = MockLink()
        self.session = self.wire.session()
        self.identity, _ = self.session.inspect((R,))
        self.meta = dict(firmware={k: getattr(self.identity, k)
                                   for k in ("version", "build", "board", "autopilot", "vehicle_type")},
                         parameters={k: dict(type="REAL32", writable=True, min=0, max=1)
                                     for k in (R, P, Y)})
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)

    def apply(self, *changes, approved=True):
        return self.session.transact(self.identity, changes, self.meta, self.folder, approved=approved)

    def test_backup_readback_and_restore(self):
        result = self.apply(Change(R, .125, .15625))
        self.assertEqual(result["verified"], [R])
        self.assertEqual(self.wire.commands[-2:], [(17, 1, "set"), (17, 1, "read")])
        backup = json.loads((self.folder / self.identity.uid / result["backup"]).read_text())
        self.assertEqual(backup["values"], {R: .125})
        result = self.apply(Change(R, .15625, backup["values"][R]))
        self.assertEqual(result["verified"], [R])
        self.assertEqual(self.wire.values[R], .125)

    def test_armed_unapproved_stale_heartbeat_and_wrong_firmware(self):
        change = Change(R, .125, .15625)
        with self.assertRaisesRegex(TunerError, "승인"):
            self.apply(change, approved=False)
        self.wire.heartbeat(armed=True)
        with self.assertRaisesRegex(TunerError, "Armed"):
            self.apply(change)
        self.wire.heartbeat(armed=False)
        self.wire.endpoint.heartbeat -= 3
        with self.assertRaisesRegex(TunerError, "heartbeat"):
            self.apply(change)
        self.wire.heartbeat(armed=False)
        self.meta["firmware"]["board"] = 999
        with self.assertRaisesRegex(TunerError, "메타데이터"):
            self.apply(change)
        self.assertFalse(self.wire.writes)

    def test_wrong_target_missing_parameter_identity_and_stale_value(self):
        change = Change(R, .125, .15625)
        self.wire.wrong_source = True
        with self.assertRaisesRegex(TunerError, "응답"):
            self.apply(change)
        self.wire.wrong_source = False
        _, values = self.session.inspect(("ATC_ANG_RLL_P",))
        self.assertIsInstance(values["ATC_ANG_RLL_P"], str)
        self.wire.board_uid = 43
        with self.assertRaisesRegex(TunerError, "ID"):
            self.apply(change)
        self.wire.board_uid = 42
        with self.assertRaisesRegex(TunerError, "미리보기"):
            self.apply(Change(R, .25, .3125))
        self.assertFalse(self.wire.writes)

    def test_partial_mismatch_stops_and_retains_backup(self):
        self.wire.bad = P
        report = self.apply(Change(R, .125, .15625), Change(P, .25, .3125), Change(Y, .375, .5))
        self.assertEqual(self.wire.writes, [R, P])
        self.assertEqual(report["verified"], [R])
        self.assertIn("불일치", report["error"])
        self.assertEqual(len(list(self.folder.rglob("transaction-*.json"))), 1)

    def test_disconnection_after_send_leaves_unverified(self):
        self.wire.disconnect_after_set = True
        report = self.apply(Change(R, .125, .15625), Change(P, .25, .3125))
        self.assertEqual(self.wire.writes, [R])
        self.assertEqual(report["verified"], [])
        self.assertIn("다시 읽어", report["error"])

    def test_arm_transition_between_preview_and_send_blocks_all_writes(self):
        self.wire.arm_on_ground_reply = True
        report = self.apply(Change(R, .125, .15625))
        self.assertIn("Armed", report["error"])
        self.assertFalse(self.wire.writes)

    def test_unsupported_firmware_only_allows_identity_read(self):
        self.wire.vehicle.autopilot = mav.MAV_AUTOPILOT_PX4
        self.wire.heartbeat(armed=False)
        self.assertFalse(self.session.inspect(())[0].copter)
        with self.assertRaisesRegex(TunerError, "스키마"):
            self.session.inspect((R,))
        self.assertFalse(self.wire.writes)


if __name__ == "__main__":
    unittest.main()
