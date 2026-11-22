"""Pure comparison of received MAVLink parameters against a site profile."""
import math


def evaluate_item(item, values, *, complete=True):
    name = item['parameter']
    if name not in values:
        return {'status': 'na' if complete else 'pending', 'value': None}
    value = values[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return {'status': 'error', 'value': value, 'reason': '타입 또는 값 오류'}
    rule = item.get('criteria')
    if not isinstance(rule, dict) or not rule:
        return {'status': 'unconfigured', 'value': value}
    try:
        checks = []
        if 'equal' in rule:
            checks.append(value == rule['equal'])
        if 'min' in rule:
            checks.append(value >= rule['min'])
        if 'max' in rule:
            checks.append(value <= rule['max'])
        if 'allowed' in rule:
            checks.append(value in rule['allowed'])
        if not checks:
            return {'status': 'unconfigured', 'value': value}
    except (TypeError, ValueError):
        return {'status': 'error', 'value': value, 'reason': '기준값 타입 오류'}
    return {'status': 'ok' if all(checks) else item.get('severity', 'warn'), 'value': value}


def evaluate_profile(profile, values, *, complete=True):
    return [dict(item, **evaluate_item(item, values, complete=complete)) for item in profile['items']]
