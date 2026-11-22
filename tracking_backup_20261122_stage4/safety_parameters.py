"""Target-scoped parameter collection and confirmed writes for Safety."""
import math
import time
from pymavlink.dialects.v20 import ardupilotmega as mav
from tuner_parameters import ParameterSession, TunerError, _name, _float32


class SafetySession(ParameterSession):
    def read_all(self, timeout=15):
        ep, v = self.endpoint, self.vehicle
        with ep.operation:
            with ep.condition:
                self._guard()
                capture = []
                ep.parameter_capture = capture
                try:
                    v.master.mav.param_request_list_send(*self.ids)
                    deadline = time.monotonic() + timeout
                    expected = None
                    by_index = {}
                    last_progress = time.monotonic()
                    next_retry = last_progress + 1.5
                    while time.monotonic() < deadline:
                        self._guard()
                        while capture:
                            msg = capture.pop(0)
                            count, index = int(msg.param_count), int(msg.param_index)
                            if not 0 < count <= 65535 or not 0 <= index < count:
                                continue
                            if expected is None:
                                expected = count
                            if count != expected:
                                raise TunerError('파라미터 목록이 수신 중 변경되었습니다. 다시 읽으세요.')
                            if index not in by_index:
                                last_progress = time.monotonic()
                                next_retry = last_progress + 1.5
                            by_index[index] = msg
                        if expected is not None and len(by_index) == expected:
                            # PX4 integer encoding depends on autopilot capabilities. Do not
                            # interpret its bit pattern as a numerical float without negotiation.
                            return {_name(m.param_id): {
                                'value': (None if v.autopilot == mav.MAV_AUTOPILOT_PX4 and
                                          m.param_type != mav.MAV_PARAM_TYPE_REAL32 else m.param_value),
                                'type': m.param_type} for m in by_index.values()}
                        if expected is not None and time.monotonic() >= next_retry:
                            for index in (i for i in range(expected) if i not in by_index):
                                v.master.mav.param_request_read_send(*self.ids, b'', index)
                            next_retry = time.monotonic() + 1.5
                        ep.condition.wait(.1)
                    raise TunerError(f'파라미터 목록 수신 미완료 ({len(by_index)}/{expected or "?"}). 다시 읽으세요.')
                finally:
                    ep.parameter_capture = None

    def apply(self, changes, *, approved=False):
        if not approved or not changes:
            raise TunerError('변경 목록 확인이 필요합니다.')
        ep, v = self.endpoint, self.vehicle
        results = []
        with ep.operation:
            with ep.condition:
                self._guard()
                if ep.last_armed:
                    raise TunerError('시동 상태에서는 변경할 수 없습니다.')
                epoch = ep.arm_epoch
            for name, old, new, typ in changes:
                try:
                    if typ != mav.MAV_PARAM_TYPE_REAL32:
                        raise TunerError('이 파라미터 형식의 쓰기는 지원하지 않습니다.')
                    old, new = _float32(old), _float32(new)
                    if not math.isfinite(new):
                        raise TunerError('유한한 값만 입력할 수 있습니다.')
                    read = lambda: v.master.mav.param_request_read_send(*self.ids, name.encode('ascii'), -1)
                    match = lambda m: m.get_type() == 'PARAM_VALUE' and _name(m.param_id) == name
                    before = self._exchange(read, match, epoch)
                    if before.param_type != typ or _float32(before.param_value) != old:
                        raise TunerError('현재 값이 변경되었습니다. 다시 읽으세요.')
                    send = lambda: v.master.mav.param_set_send(*self.ids, name.encode('ascii'), new, typ)
                    reply = self._exchange(send, match, epoch, attempts=1)
                    if reply.param_type != typ or _float32(reply.param_value) != new:
                        raise TunerError('PARAM_VALUE 응답 불일치')
                    verify = self._exchange(read, match, epoch)
                    if verify.param_type != typ or _float32(verify.param_value) != new:
                        raise TunerError('재읽기 값 불일치')
                    results.append((name, True, '반영 확인'))
                except (TunerError, ValueError, OSError, OverflowError) as exc:
                    results.append((name, False, str(exc)))
                    break
        return results
