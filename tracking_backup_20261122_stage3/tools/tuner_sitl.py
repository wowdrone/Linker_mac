"""Compare two DataFlash logs from an operator-run ArduCopter SITL replay."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tuner_analysis import from_bin
from tuner_parameters import PID


def compare(before: Path, after: Path) -> dict:
    baseline, candidate = from_bin(before), from_bin(after)
    if baseline["firmware"] != candidate["firmware"] or not baseline["firmware"].startswith("ArduCopter"):
        raise ValueError("두 로그의 ArduCopter 펌웨어 표기가 일치해야 합니다.")
    gains = {name: [baseline["params"][name], candidate["params"][name]]
             for name in sorted(PID) if name in baseline["params"] and name in candidate["params"]
             and baseline["params"][name] != candidate["params"][name]}
    axes = {axis: dict(before=baseline["rates"][axis][1], after=candidate["rates"][axis][1])
            for axis in baseline["rates"]}
    combined = [axis for axis, data in axes.items() if all(
        data[phase]["rms"] is not None and data[phase]["steps"] > 0 for phase in ("before", "after"))]
    return dict(firmware=baseline["firmware"], before=str(before), after=str(after),
                logged_gain_changes=gains, comparable_axes=combined, metrics=axes,
                status="operator_review_required" if combined and gains else "insufficient_evidence")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(compare(args.before, args.after), ensure_ascii=False, indent=2, allow_nan=False))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
