"""Received health samples, with unknown values kept distinct from zero."""
import math
import time

def number(value, invalid=()):
    if value is None or value in invalid:
        return None
    value = float(value)
    return value if math.isfinite(value) else None

class HealthTelemetry:

    def __init__(self):
        self.samples = {}
        self.legacy = {}

    def consume(self, msg):
        kind = msg.get_type()
        values, detail = ({}, '')
        updated_fields = None
        if kind == 'VIBRATION':
            group = 'vibration'
            values = {axis.upper(): number(getattr(msg, 'vibration_' + axis)) for axis in 'xyz'}
            for axis in 'xyz':
                self.legacy['vibe_' + axis] = values[axis.upper()]
            clips = [getattr(msg, 'clipping_' + str(i)) for i in range(3)]
            self.legacy.update({'vibe_clip' + str(i): value for i, value in enumerate(clips)})
            detail = 'Clip0/1/2: ' + ' / '.join(map(str, clips))
        elif kind == 'GPS_RAW_INT':
            group = 'gps'
            sats, hdop = (number(msg.satellites_visible, (255,)), number(msg.eph, (65535,)))
            hdop = hdop / 100 if hdop is not None else None
            values = {'Sats': sats, 'HDOP': hdop}
            self.legacy.update(gps_sats=sats, gps_hdop=hdop, gps_fix_type=msg.fix_type)
            detail = f"Fix: {msg.fix_type} · HDOP: {(hdop if hdop is not None else '—')}"
        elif kind in ('RADIO_STATUS', 'RADIO'):
            group = 'radio'
            values = {'Local': number(msg.rssi, (255,)), 'Remote': number(msg.remrssi, (255,))}
            self.legacy.update(radio_rssi=values['Local'], radio_remrssi=values['Remote'])
            detail = f'TX buffer: {msg.txbuf}% · RX errors: {msg.rxerrors} · Fixed: {msg.fixed}'
        elif kind in ('EKF_STATUS_REPORT', 'ESTIMATOR_STATUS'):
            group = 'ekf'
            fields = ('velocity_variance', 'pos_horiz_variance', 'pos_vert_variance', 'compass_variance', 'terrain_alt_variance') if kind == 'EKF_STATUS_REPORT' else ('vel_ratio', 'pos_horiz_ratio', 'pos_vert_ratio', 'mag_ratio', 'hagl_ratio')
            values = {label: number(getattr(msg, field, None)) for label, field in zip(('Velocity', 'Pos H', 'Pos V', 'Compass', 'Terrain'), fields)}
            flags = int(msg.flags)
            detail = f"Flags: 0x{flags:04X} · Attitude: {('OK' if flags & 1 else '—')} · Vel H: {('OK' if flags & 2 else '—')} · Pos H abs: {('OK' if flags & 16 else '—')}"
        elif kind in ('RC_CHANNELS', 'RC_CHANNELS_RAW'):
            group = 'rc_in'
            if kind == 'RC_CHANNELS':
                count, offset = (min(int(msg.chancount), 18), 0)
                values = {f'CH{i}': number(getattr(msg, f'chan{i}_raw'), (65535, 0)) if i <= count else None for i in range(1, 19)}
            else:
                offset = int(msg.port) * 8
                values = dict(self.samples.get(group, {}).get('values', {}))
                values.update({f'CH{offset + i}': number(getattr(msg, f'chan{i}_raw'), (65535, 0)) for i in range(1, 9) if offset + i <= 18})
                count = len([v for v in values.values() if v is not None])
                updated_fields = {f'CH{offset + i}' for i in range(1, 9) if offset + i <= 18}
            detail = f"Channels: {count} · RSSI raw: {(msg.rssi if msg.rssi != 255 else '—')}"
        elif kind == 'SERVO_OUTPUT_RAW':
            group = 'rc_out'
            offset = int(msg.port) * 8
            values = dict(self.samples.get(group, {}).get('values', {}))
            for i in range(1, 17):
                if offset + i <= 32:
                    values[f'CH{offset + i}'] = number(getattr(msg, f'servo{i}_raw', None), (65535,))
            updated_fields = {f'CH{offset + i}' for i in range(1, 17) if offset + i <= 32}
            detail = f'Output bank: {msg.port} · PWM outputs (µs)'
        elif kind == 'ATTITUDE':
            group = 'attitude'
            values = {label: math.degrees(getattr(msg, field)) for label, field in (('Roll', 'roll'), ('Pitch', 'pitch'), ('Yaw', 'yaw'))}
        else:
            return False
        previous = self.samples.get(group, {})
        now = time.monotonic()
        field_at = dict(previous.get('field_at', {}))
        field_at.update({field: now for field in (values if updated_fields is None else updated_fields)})
        self.samples[group] = dict(values=values, detail=detail, source=kind, field_at=field_at, seq=previous.get('seq', 0) + 1, at=now)
        return True

    def snapshot(self):
        return dict(self.samples)
