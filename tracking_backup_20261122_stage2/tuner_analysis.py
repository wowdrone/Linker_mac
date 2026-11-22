"""Read-only ArduPilot DataFlash and observed MAVLink rate analysis."""

from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

AXES = ("Roll", "Pitch", "Yaw")
TYPES = {"VER", "MSG", "ARM", "EV", "RATE", "ATT", "IMU", "GYR", "ISBH",
         "ISBD", "VIBE", "PIDR", "PIDP", "PIDY", "FTN1", "FTN2", "ESC", "PARM"}


def segments(times: np.ndarray):
    dt = np.diff(times)
    positive = dt[dt > 0]
    if len(positive):
        cuts = np.flatnonzero(~np.isfinite(dt) | (dt <= 0) | (dt > 2.5 * np.median(positive))) + 1
        yield from np.split(np.arange(len(times)), cuts)


def rate_metrics(rows: np.ndarray) -> dict:
    result = dict(rms=None, overshoot=None, settling=None, oscillation=None, steps=0)
    errors, weights, overshoots, settling, ringing = [], [], [], [], []
    for idx in segments(rows[:, 0]):
        t, target, actual = rows[idx].T
        if (len(t) < 20 or not np.isfinite(rows[idx]).all() or t[-1]-t[0] < 1
                or np.ptp(target) < 5 or np.median(np.diff(t)) > .05):
            continue
        squared = (target-actual)**2
        errors.extend((squared[1:] + squared[:-1])/2)
        weights.extend(np.diff(t))
        for i in np.flatnonzero(np.abs(np.diff(target)) >= 10)+1:
            lo, hi = np.searchsorted(t, (t[i]-.3, t[i]+1.5))
            if i-lo < 4 or hi-i < 10 or t[hi-1]-t[i] < 1.4:
                continue
            before, after = np.median(target[lo:i]), np.median(target[i:hi])
            delta = after-before
            if abs(delta) < 10 or max(np.ptp(target[lo:i]), np.ptp(target[i:hi])) > .05*abs(delta):
                continue
            if abs(np.median(actual[lo:i])-before) > .10*abs(delta):
                continue
            response = (actual[i:hi]-before)/delta
            overshoots.append(max(0., float(np.max(response)-1))*100)
            outside = np.flatnonzero(np.abs(response-1) > .05)
            start = outside[-1]+1 if len(outside) else 0
            if start < len(response) and t[hi-1]-t[i+start] >= .3:
                settling.append(float(t[i+start]-t[i]))
            ringing.append(np.count_nonzero(np.diff(np.sign(response[outside]-1))))
    if weights:
        result["rms"] = float(np.sqrt(np.average(errors, weights=weights)))
    if overshoots:
        result.update(overshoot=float(np.median(overshoots)), oscillation=float(np.median(ringing)),
                      steps=len(overshoots))
        if len(settling) == len(overshoots):
            result["settling"] = float(np.median(settling))
    return result


def spectrum(values: np.ndarray, hz: float) -> dict | None:
    if len(values) < 128 or not np.isfinite(values).all() or not np.isfinite(hz) or hz <= 0:
        return None
    window = np.hanning(len(values))
    magnitude = np.abs(np.fft.rfft((values-values.mean()) * window))*2/window.sum()
    if len(values) % 2 == 0:
        magnitude[-1] *= .5
    freq = np.fft.rfftfreq(len(values), 1/hz)
    candidates = np.flatnonzero((magnitude[1:-1] > magnitude[:-2]) &
                                (magnitude[1:-1] > magnitude[2:]))+1
    floor = max(5*np.median(magnitude), 1e-9)
    peaks = [i for i in candidates if 5 <= freq[i] < .45*hz
             and magnitude[i] > floor]
    peaks.sort(key=lambda i: magnitude[i], reverse=True)
    return dict(hz=hz, freq=freq, magnitude=magnitude, peaks=[float(freq[i]) for i in peaks[:3]])


def report_rates(rows):
    result = {}
    for axis in AXES:
        values = np.asarray(rows.get(axis, ()), dtype=float).reshape(-1, 3)
        if len(values):
            valid = np.isfinite(values).all(axis=1)
            if not valid.all():
                # Prevent missing values from joining two usable runs.
                values[~valid] = np.nan
        result[axis] = (values, rate_metrics(values))
    return result


def from_telemetry(records: list) -> dict:
    rows, target = defaultdict(list), None
    for kind, timestamp, received, values in records:
        if kind == "gap":
            target = None
            for axis in AXES:
                if rows[axis]:
                    rows[axis].append((np.nan, np.nan, np.nan))
        elif kind == "target":
            target = (timestamp, received, values)
        elif target and 0 <= timestamp-target[0] <= .1 and 0 <= received-target[1] <= .2:
            for axis, desired, actual in zip(AXES, target[2], values):
                if desired is not None:
                    rows[axis].append((timestamp, np.degrees(desired), np.degrees(actual)))
    return dict(source="MAVLink ATTITUDE_TARGET / ATTITUDE", firmware="연결 기체",
                rates=report_rates(rows), spectra={}, params={}, notes=[
                    "최근 Armed 수신 구간. 목표축 마스크를 적용하고 100 ms 이내의 목표를 사용합니다.",
                    "스트림 다운샘플링과 지연의 영향이 있습니다. 원본 RATE 로그로 교차 검증하세요."])


def from_bin(path: Path) -> dict:
    from pymavlink.DFReader import DFReader_binary

    if path.suffix.lower() != ".bin" or not path.is_file() or not path.stat().st_size:
        raise ValueError("유효한 ArduPilot DataFlash .BIN 파일을 선택하세요.")
    reader = DFReader_binary(str(path))
    counts, rows, gyro, params = Counter(), defaultdict(list), defaultdict(list), {}
    imu, batch, chunks = {}, None, []
    ftn, esc, vibes, limits = [], [], [], Counter()
    attitude, firmware, armed = None, "", False
    invalid_rate = 0
    try:
        while (msg := reader.recv_match(type=TYPES)) is not None:
            kind, m = msg.get_type(), msg.to_dict()
            counts[kind] += 1
            if counts.total() > 1_000_000:
                raise ValueError("분석 샘플 한도를 초과했습니다. 더 짧은 로그를 선택하세요.")
            t = m.get("TimeUS", 0)*1e-6
            if kind == "VER":
                firmware = str(m.get("FWS", ""))
            elif kind == "MSG" and not firmware and str(m.get("Message", "")).startswith("Ardu"):
                firmware = m["Message"]
            elif kind == "PARM" and {"Name", "Value"} <= m.keys():
                params[str(m["Name"])] = float(m["Value"])
            elif kind == "ARM" or kind == "EV" and m.get("Id") in (10, 11):
                new_armed = m.get("ArmState") == 1 if kind == "ARM" else m["Id"] == 10
                if armed and not new_armed:
                    for series in rows.values():
                        if series:
                            series.append((np.nan, np.nan, np.nan))
                    batch = None
                    for series in gyro.values():
                        if series:
                            series.append((np.nan, np.nan, np.nan, np.nan))
                armed = new_armed
            elif not armed:
                continue
            elif kind == "RATE" and t > 0:
                for axis in AXES:
                    k = axis[0]
                    if not {k, k+"Des"} <= m.keys():
                        continue
                    units = [msg.fmt.get_unit(field) for field in (k, k+"Des")]
                    if all(u in ("deg/s", "°/s") for u in units) or (
                            not any(units) and firmware.startswith("ArduCopter")):
                        scale = 1.
                    elif units == ["rad/s", "rad/s"]:
                        scale = 180/np.pi
                    else:
                        invalid_rate += 1
                        continue
                    rows[axis].append((t, m[k+"Des"]*scale, m[k]*scale))
            elif kind == "ATT" and {"Roll", "Pitch", "Yaw"} <= m.keys():
                attitude = tuple(m[k] for k in AXES)
            elif kind == "VIBE" and all("Vibe"+a in m for a in "XYZ"):
                vibes.append(tuple(m["Vibe"+a] for a in "XYZ"))
            elif kind in ("PIDR", "PIDP", "PIDY") and int(m.get("Flags", 0)) & 3:
                limits[kind] += 1
            elif kind == "GYR" and {"I", "SampleUS", "GyrX", "GyrY", "GyrZ"} <= m.keys():
                gyro[m["I"]].append((m["SampleUS"]*1e-6, m["GyrX"], m["GyrY"], m["GyrZ"]))
            elif kind == "ISBH":
                batch, chunks = None, []
                if {"N", "type", "instance", "mul", "smp_cnt", "smp_rate"} <= m.keys() and (
                        m["type"] in (0, 1) and m["mul"] > 0 and 128 <= m["smp_cnt"] <= 32768):
                    batch = m
            elif kind == "ISBD" and batch and m.get("N") == batch["N"]:
                if m.get("seqno") != len(chunks) or not all(a in m and len(m[a]) == 32 for a in "xyz"):
                    batch = None
                    continue
                chunks.append(np.column_stack([m[a] for a in "xyz"]))
                if len(chunks)*32 >= batch["smp_cnt"]:
                    signal = np.concatenate(chunks)[:batch["smp_cnt"]]/batch["mul"]
                    sensor = "Accel" if batch["type"] == 0 else "Gyro"
                    key = (sensor, batch["instance"])
                    if key not in imu or len(signal) > len(imu[key][1]):
                        imu[key] = (batch["smp_rate"], signal)
                    batch = None
            elif kind in ("FTN1", "FTN2"):
                ftn.extend(m[k] for k in ("PkAvg", "PkX", "PkY", "PkZ")
                           if k in m and np.isfinite(m[k]) and m[k] > 0)
            elif kind == "ESC" and np.isfinite(m.get("RPM", 0)) and m.get("RPM", 0) > 0:
                esc.append(m["RPM"]/60)
    finally:
        reader.close()
    if not counts:
        raise ValueError("지원되는 DataFlash 메시지가 없습니다.")
    for instance, samples in gyro.items():
        if ("Gyro", instance) in imu:
            continue
        data = np.asarray(samples, dtype=float)
        for idx in sorted(segments(data[:, 0]), key=len, reverse=True):
            data_slice = data[idx[:8192]]
            if len(data_slice) < 128:
                break
            t, dt = data_slice[:, 0], np.diff(data_slice[:, 0])
            median = np.median(dt)
            if np.max(np.abs(dt-median)) > .15*median:
                continue
            grid = np.linspace(t[0], t[-1], len(t))
            imu["Gyro", instance] = ((len(t)-1)/(t[-1]-t[0]), np.column_stack([
                np.interp(grid, t, data_slice[:, i]) for i in range(1, 4)]))
            break
    spectra = {}
    for (sensor, instance), (hz, signal) in imu.items():
        unit = "rad/s" if sensor == "Gyro" else "m/s²"
        for i, axis in enumerate("XYZ"):
            spec = spectrum(signal[:, i], hz)
            if spec:
                spectra[f"{sensor} {instance} {axis} ({unit})"] = spec
    notes = ["Armed 구간의 RATE만 추종 지표에 사용합니다. ATT는 각도이며 각속도 계산에 사용하지 않습니다.",
             "PID 출력 제한 횟수: "+str(dict(limits)), f"ATT 마지막 각도: {attitude}",
             f"단위를 확인하지 못한 RATE 축 샘플: {invalid_rate}개.",
             "일반 IMU 로그는 실제 원시 샘플 주기가 확인되지 않아 FFT에서 제외합니다."]
    if vibes:
        notes.append(f"Armed VIBE 축별 중앙값(m/s²): {np.round(np.median(vibes, axis=0), 2)}")
    return dict(source=path.name, firmware=firmware or "확인 불가", rates=report_rates(rows),
                spectra=spectra, params=params, notes=notes, counts=dict(counts),
                onboard_fft=float(np.median(ftn)) if ftn else None,
                esc_hz=(min(esc), max(esc)) if esc else None)
