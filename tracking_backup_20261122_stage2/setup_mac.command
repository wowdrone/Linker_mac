#!/bin/bash
# Linker GCS (macOS) - 최초 1회 설치
# 사용: 터미널에서  bash setup_mac.command
set -e
cd "$(dirname "$0")"

PY=""
for c in python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "python3 가 없습니다. https://www.python.org/downloads/macos/ 에서 Python 3.11 을 설치하세요."
  exit 1
fi
echo "사용할 파이썬: $($PY --version) ($(command -v $PY))"

if [ ! -d .venv ]; then
  "$PY" -m venv .venv
fi
source .venv/bin/activate
python -m pip install --upgrade pip wheel
python -m pip install -r requirements.txt

# 실행 스크립트에 실행 권한 부여
chmod +x run_mac.command run_sim.command build_mac.command 2>/dev/null || true

python - <<'EOF'
from PyQt5.QtWidgets import QApplication
from PyQt5 import QtWebEngineWidgets
import cv2, pymavlink, serial
import main_window
print("설치 확인 OK  /  OpenCV", cv2.__version__)
EOF
echo ""
echo "설치 완료. 실행: ./run_mac.command  (또는 Finder 에서 더블클릭)"
