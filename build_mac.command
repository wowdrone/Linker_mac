#!/bin/bash
# LINKER.app 빌드 (결과: dist/LINKER.app)
set -e
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  echo "먼저 setup_mac.command 를 실행하세요."
  exit 1
fi
if ! .venv/bin/python -m PyInstaller --version >/dev/null 2>&1; then
  .venv/bin/python -m pip install pyinstaller
fi
.venv/bin/python -m PyInstaller --noconfirm --clean "$@" Linker_mac.spec
echo ""
echo "완료: $(pwd)/dist/LINKER.app"
