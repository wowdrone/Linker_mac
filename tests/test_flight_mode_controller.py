"""Flight mode change transaction: one send, HEARTBEAT confirmation, no automatic retry."""
from types import SimpleNamespace
import unittest

from pymavlink import mavutil

import flight_mode_controller as fmc
from flight_mode_controller import FlightModeController, ModeRequestError, mode_options

M = mavutil.mavlink


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Mav:
    def __init__(self):
        self.sent = []

    def command_long_send(self, *args):
        self.sent.append(args)


class Vehicle:
    def __init__(self, clock, autopilot=M.MAV_AUTOPILOT_ARDUPILOTMEGA, mav_type=M.MAV_TYPE_QUADROTOR,
                 mode='STABILIZE', armed=False, sysid=1):
        self.clock = clock
        self.autopilot, self.mav_type = autopilot, mav_type
        self.sysid, self.compid, self.name = sysid, 1, f'SYS {sysid}'
        self.link = SimpleNamespace(link_id=1, human='udp')
        self.connected, self.connection_epoch = True, 1
        self.master = SimpleNamespace(mav=Mav())
        self.tracking_telemetry = {'GPS_RAW_INT': {'fix_type': 3}}
        self.hb(mode, armed)

    def hb(self, mode, armed=False, t=None):
        self.heartbeat_state = (self.clock() if t is None else t, mode, armed, 0, 0)

    @property
    def sent(self):
        return self.master.mav.sent


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.v = Vehicle(self.clock)
        self.selected = self.v
        self.c = FlightModeController(selected=lambda: self.selected, clock=self.clock)

    def advance(self, dt):
        self.clock.t += dt
        self.c.tick()

    def request(self, mode='LOITER', source='app'):
        ticket = self.c.prepare(self.v, mode, source)
        self.c.commit(ticket['id'], source)
        return ticket

    # -- sending ---------------------------------------------------------------
    def test_ardupilot_uses_firmware_mapping_and_do_set_mode_once(self):
        self.request('LOITER')
        self.assertEqual(len(self.v.sent), 1)
        args = self.v.sent[0]
        mapping = mavutil.mode_mapping_byname(M.MAV_TYPE_QUADROTOR)
        self.assertEqual(args[:4], (1, 1, M.MAV_CMD_DO_SET_MODE, 0))
        self.assertEqual(args[4:7], (float(M.MAV_MODE_FLAG_CUSTOM_MODE_ENABLED), float(mapping['LOITER']), 0.0))

    def test_px4_uses_px4_mapping_not_ardupilot_numbers(self):
        v = Vehicle(self.clock, autopilot=M.MAV_AUTOPILOT_PX4, mode='MANUAL')
        self.selected = v
        names = {o.name for o in mode_options(v) if o.enabled}
        self.assertIn('POSCTL', names)
        self.assertNotIn('GUIDED', names)   # ArduPilot-only name never offered for PX4
        ticket = self.c.prepare(v, 'POSCTL')
        self.c.commit(ticket['id'])
        self.assertEqual(v.sent[0][4:7], tuple(float(x) for x in mavutil.px4_map['POSCTL']))

    def test_unknown_autopilot_has_no_modes(self):
        v = Vehicle(self.clock, autopilot=M.MAV_AUTOPILOT_GENERIC)
        self.selected = v
        self.assertEqual(mode_options(v), [])
        with self.assertRaises(ModeRequestError):
            self.c.prepare(v, 'LOITER')
        self.assertEqual(v.sent, [])

    def test_forbidden_modes_are_never_sent(self):
        px4 = Vehicle(self.clock, autopilot=M.MAV_AUTOPILOT_PX4, mode='MANUAL')
        for vehicle, modes in ((self.v, ('AUTO', 'TAKEOFF', 'FLIP', 'THROW', 'ACRO')),
                               (px4, ('MISSION', 'TAKEOFF', 'OFFBOARD', 'ACRO'))):
            self.selected = vehicle
            for mode in modes:
                with self.assertRaises(ModeRequestError, msg=mode):
                    self.c.prepare(vehicle, mode, 'web')
            self.assertEqual(vehicle.sent, [])
        disabled = {o.name: o.reason for o in mode_options(px4) if not o.enabled}
        self.assertIn('setpoint', disabled['OFFBOARD'])

    # -- confirmation ------------------------------------------------------------
    def test_success_needs_heartbeat_after_send(self):
        self.request('LOITER')
        self.v.hb('LOITER', t=self.clock() - .5)   # older than the send: must not count
        self.advance(.1)
        self.assertEqual(self.c.state, 'REQUESTING')
        self.clock.t += .2
        self.v.hb('LOITER')
        self.c.tick()
        self.assertEqual(self.c.state, 'CONFIRMED')
        self.assertIn('ACK 없음', self.c.detail)

    def test_ack_accepted_but_mode_unchanged_fails(self):
        self.request('LOITER')
        self.c.on_command_feedback(self.v, 'MAV_CMD_DO_SET_MODE', True, 'MAV_CMD_DO_SET_MODE: MAV_RESULT_ACCEPTED')
        self.assertEqual(self.c.state, 'REQUESTING')
        for _ in range(fmc.MISMATCH_AFTER_ACK):
            self.clock.t += 1
            self.v.hb('STABILIZE')
            self.c.tick()
        self.assertEqual(self.c.state, 'FAILED')
        self.assertEqual(len(self.v.sent), 1)

    def test_rejected_ack(self):
        self.request('LOITER')
        self.c.on_command_feedback(self.v, 'MAV_CMD_DO_SET_MODE', False, 'MAV_CMD_DO_SET_MODE: MAV_RESULT_DENIED')
        self.assertEqual(self.c.state, 'REJECTED')

    def test_unrelated_ack_or_other_vehicle_ignored(self):
        self.request('LOITER')
        other = Vehicle(self.clock, sysid=2)
        self.c.on_command_feedback(other, 'MAV_CMD_DO_SET_MODE', False, 'x: MAV_RESULT_DENIED')
        self.c.on_command_feedback(self.v, 'MAV_CMD_COMPONENT_ARM_DISARM', False, 'x: MAV_RESULT_DENIED')
        self.assertEqual(self.c.state, 'REQUESTING')

    def test_timeout_without_confirmation_and_no_retry(self):
        self.request('LOITER')
        for _ in range(8):
            self.clock.t += 1
            self.v.hb('STABILIZE')
            self.c.tick()
        self.assertEqual(self.c.state, 'TIMEOUT')
        self.assertEqual(len(self.v.sent), 1)

    # -- re-validation before the single send ------------------------------------
    def test_commit_revalidates_state(self):
        cases = {
            'stale heartbeat': lambda: self.v.hb('STABILIZE', t=self.clock() - 10),
            'mode changed': lambda: self.v.hb('ALT_HOLD'),
            'armed changed': lambda: self.v.hb('STABILIZE', armed=True),
            'reconnected': lambda: setattr(self.v, 'connection_epoch', 2),
            'disconnected': lambda: setattr(self.v, 'connected', False),
            'sysid changed': lambda: setattr(self.v, 'sysid', 9),
            'link changed': lambda: setattr(self.v, 'link', SimpleNamespace(link_id=7, human='tcp')),
            'selection changed': lambda: setattr(self, 'selected', Vehicle(self.clock, sysid=3)),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.setUp()
                ticket = self.c.prepare(self.v, 'LOITER')
                change()
                with self.assertRaises(ModeRequestError):
                    self.c.commit(ticket['id'])
                self.assertEqual(self.v.sent, [])
                self.assertEqual(self.c.state, 'CANCELLED')

    def test_expired_ticket_not_sent(self):
        ticket = self.c.prepare(self.v, 'LOITER')
        self.clock.t += fmc.TICKET_TTL_S + 1
        self.v.hb('STABILIZE')
        with self.assertRaises(ModeRequestError):
            self.c.commit(ticket['id'])
        self.assertEqual(self.v.sent, [])

    def test_vehicle_change_or_disconnect_while_requesting_fails(self):
        self.request('LOITER')
        self.selected = Vehicle(self.clock, sysid=2)
        self.advance(.1)
        self.assertEqual(self.c.state, 'FAILED')
        self.selected = self.v
        self.clock.t += 61
        self.v.hb('STABILIZE')
        self.request('LOITER')
        self.v.connected = False
        self.advance(.1)
        self.assertEqual(self.c.state, 'FAILED')

    def confirm(self, mode):
        self.clock.t += .1
        self.v.hb(mode)
        self.c.tick()
        self.assertEqual(self.c.state, 'CONFIRMED')

    def test_duplicate_clicks_and_rate_limit(self):
        ticket = self.c.prepare(self.v, 'LOITER')
        self.c.commit(ticket['id'])
        with self.assertRaises(ModeRequestError):
            self.c.commit(ticket['id'])          # double click
        with self.assertRaises(ModeRequestError):
            self.c.prepare(self.v, 'ALT_HOLD')   # new request while the result is pending
        self.assertEqual(len(self.v.sent), 1)
        self.confirm('LOITER')
        for mode in ('ALT_HOLD', 'LOITER'):
            self.request(mode)
            self.confirm(mode)
        ticket = self.c.prepare(self.v, 'ALT_HOLD')
        with self.assertRaises(ModeRequestError) as error:
            self.c.commit(ticket['id'])
        self.assertIn('한도', str(error.exception))
        self.assertEqual(len(self.v.sent), fmc.RATE_MAX)

    # -- concurrent screens --------------------------------------------------------
    def test_one_ticket_valid_and_source_bound(self):
        app = self.c.prepare(self.v, 'LOITER', 'app')
        web = self.c.prepare(self.v, 'ALT_HOLD', 'web')     # supersedes the app ticket
        with self.assertRaises(ModeRequestError):
            self.c.commit(app['id'], 'app')
        self.assertEqual(self.c.state, 'CONFIRMING')       # the valid ticket survives a stale commit
        with self.assertRaises(ModeRequestError):
            self.c.commit(web['id'], 'app')                 # confirm only where it was requested
        self.assertEqual(self.v.sent, [])
        self.c.commit(web['id'], 'web')
        self.assertEqual(len(self.v.sent), 1)
        events = [(a['event'], a['source']) for a in self.c.audit]
        self.assertIn(('SUPERSEDED', 'app'), events)
        self.assertIn(('SENT', 'web'), events)
        sent = [a for a in self.c.audit if a['event'] == 'SENT'][0]
        self.assertEqual((sent['from_mode'], sent['to_mode'], sent['armed']), ('STABILIZE', 'ALT_HOLD', False))
        self.assertEqual((sent['vehicle']['link_id'], sent['vehicle']['sysid'], sent['vehicle']['compid']), (1, 1, 1))

    def test_impact_warns_when_armed(self):
        self.v.hb('LOITER', armed=True)
        ticket = self.c.prepare(self.v, 'RTL')
        self.assertTrue(ticket['armed'])
        self.assertTrue(any('ARMED' in line for line in ticket['impact']))
        self.assertTrue(any('ARM·이륙·미션' in line for line in ticket['impact']))


if __name__ == '__main__':
    unittest.main()
