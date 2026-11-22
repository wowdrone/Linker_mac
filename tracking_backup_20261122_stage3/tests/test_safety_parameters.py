import threading
import time
import unittest
from types import SimpleNamespace
from pymavlink.dialects.v20 import ardupilotmega as mav
from tuner_parameters import TunerEndpoint, TunerError
from safety_parameters import SafetySession


class Message:
    param_count = 2
    param_type = mav.MAV_PARAM_TYPE_REAL32
    def __init__(self, index, name, value): self.param_index=index; self.param_id=name; self.param_value=value
    def get_type(self): return 'PARAM_VALUE'
    def get_srcSystem(self): return 1
    def get_srcComponent(self): return 1


class SessionTest(unittest.TestCase):
    def setUp(self):
        self.vehicle=SimpleNamespace(sysid=1,compid=1,autopilot=mav.MAV_AUTOPILOT_ARDUPILOTMEGA,mav_type=2,connected=True)
        self.vehicle.link=SimpleNamespace(master=object(),_running=True)
        self.endpoint=TunerEndpoint(self.vehicle); self.endpoint.heartbeat=time.monotonic()
        self.vehicle.tuner_endpoint=self.endpoint
        self.mav=SimpleNamespace(param_request_list_send=self.send_list)
        self.vehicle.master=SimpleNamespace(mav=self.mav)
        self.session=SafetySession(self.endpoint,lambda:True,timeout=.05)
    def send_list(self,*args):
        self.endpoint.observe(Message(0,'A',1.5)); self.endpoint.observe(Message(1,'B',2.0))
    def test_read_all_collects_complete_list(self):
        self.assertEqual(self.session.read_all(timeout=.1)['A']['value'],1.5)
        self.assertIsNone(self.endpoint.parameter_capture)
    def test_incomplete_is_not_reported_as_missing(self):
        self.mav.param_request_list_send=lambda *args:self.endpoint.observe(Message(0,'A',1))
        with self.assertRaises(TunerError): self.session.read_all(timeout=.02)
        self.assertIsNone(self.endpoint.parameter_capture)
    def test_armed_blocks_write(self):
        self.endpoint.last_armed=True
        with self.assertRaises(TunerError): self.session.apply([('A',1,2,mav.MAV_PARAM_TYPE_REAL32)],approved=True)
    def test_write_requires_approval_and_verifies_reread(self):
        value = [1.0]
        def read(*args): self.endpoint.observe(Message(0, 'A', value[0]))
        def write(*args):
            value[0] = args[3]
            self.endpoint.observe(Message(0, 'A', value[0]))
        self.mav.param_request_read_send = read
        self.mav.param_set_send = write
        change = [('A',1.0,2.0,mav.MAV_PARAM_TYPE_REAL32)]
        with self.assertRaises(TunerError): self.session.apply(change)
        self.assertEqual(value[0], 1.0)
        self.assertEqual(self.session.apply(change,approved=True), [('A',True,'반영 확인')])
        self.assertEqual(value[0], 2.0)
    def test_px4_integer_is_not_misread_as_float(self):
        self.vehicle.autopilot = mav.MAV_AUTOPILOT_PX4
        def send(*args):
            msg=Message(0,'A',1.0); msg.param_type=mav.MAV_PARAM_TYPE_INT32
            self.endpoint.observe(msg); self.endpoint.observe(Message(1,'B',2.0))
        self.mav.param_request_list_send=send
        self.assertIsNone(self.session.read_all(timeout=.1)['A']['value'])
