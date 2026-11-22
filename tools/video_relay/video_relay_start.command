#!/bin/bash
# Linker 로컬 RTSP 중계 시작 (mediamtx). Linker는 이 스크립트를 자동 실행하지 않는다.
#   video_relay_start.command rtsp://카메라주소:포트/경로
#   (인자가 없으면 같은 폴더 camera.local 의 첫 줄을 사용)
set -u
cd "$(dirname "$0")" || exit 1
CFG="$PWD/linker_relay.yml"
LOCAL="$PWD/camera.local"
LOG="$HOME/Library/Logs/linker_video_relay.log"
mask() { sed -E 's#([A-Za-z][A-Za-z0-9+.-]*://)[^/@[:space:]]*@#\1***@#g'; }

BIN="$(command -v mediamtx || true)"
if [ -z "$BIN" ]; then
  echo "mediamtx 미설치 · 설치 명령(자동 실행하지 않음): brew install mediamtx"
  exit 2
fi
echo "mediamtx $("$BIN" --version 2>&1 | head -1) · 설정 키 기준: reference mediamtx.yml v1.21.4"

URL="${1:-}"
if [ -z "$URL" ] && [ -f "$LOCAL" ]; then URL="$(head -1 "$LOCAL" | tr -d '\r')"; fi
if [ -z "$URL" ]; then
  echo "카메라 주소 없음 · 인자 또는 camera.local 첫 줄에 rtsp://… 주소"
  exit 2
fi
case "$URL" in rtsp://*|rtsps://*) ;; *) echo "RTSP 주소만 지원"; exit 2 ;; esac

if pgrep -f "mediamtx $CFG" >/dev/null 2>&1; then
  echo "이미 실행 중 · 상태: video_relay_status.command"
  exit 0
fi
if lsof -nP -iTCP:8554 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "127.0.0.1:8554 포트 사용 중 · 다른 RTSP 서버 확인"
  exit 3
fi

echo "카메라 $(printf '%s' "$URL" | mask) → rtsp://127.0.0.1:8554/cam (TCP)"
mkdir -p "$(dirname "$LOG")"
( MTX_PATHS_CAM_SOURCE="$URL" exec "$BIN" "$CFG" ) 2>&1 | mask >> "$LOG" &
sleep 1.5
if ! pgrep -f "mediamtx $CFG" >/dev/null 2>&1; then
  echo "시작 실패 · 로그: $LOG"
  tail -5 "$LOG"
  exit 4
fi
# The relay must listen on loopback only. Older/newer versions may add servers this file
# does not switch off (e.g. moq in recent main): list anything not bound to 127.0.0.1 / ::1.
OPEN="$(lsof -nP -a -c mediamtx -i -sTCP:LISTEN 2>/dev/null | awk 'NR>1{print $9}' | grep -v -E '^(127\.0\.0\.1|\[::1\]):' || true)"
OPEN_UDP="$(lsof -nP -a -c mediamtx -iUDP 2>/dev/null | awk 'NR>1{print $9}' | grep -v -E '^(127\.0\.0\.1|\[::1\]):' || true)"
if [ -n "$OPEN$OPEN_UDP" ]; then
  echo "경고 · 127.0.0.1 이외 주소에서 열린 포트:"
  printf '%s\n%s\n' "$OPEN" "$OPEN_UDP" | sed '/^$/d'
  echo "설치 버전의 설정 키를 확인하고 해당 서버를 끄세요."
fi
echo "중계 주소 rtsp://127.0.0.1:8554/cam · 로그 $LOG"
echo "상태 video_relay_status.command · 정지 video_relay_stop.command"
