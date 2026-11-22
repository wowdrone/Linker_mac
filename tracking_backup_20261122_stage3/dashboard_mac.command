#!/bin/bash
# Linker 주행 대시보드만 실행 (실제 연결 없이 시뮬레이션, Ctrl+C로 종료)
cd "$(dirname "$0")" || exit 1
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
exec "$PY" drive_dashboard.py "$@"
