#!/bin/bash
# Linker GCS (macOS) 실행
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
  echo "먼저 setup_mac.command 를 실행하세요."
  exit 1
fi
exec .venv/bin/python launch_mac.py "$@"
