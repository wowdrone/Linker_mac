#!/bin/bash
# Linker 로컬 RTSP 중계 정지.
cd "$(dirname "$0")" || exit 1
CFG="$PWD/linker_relay.yml"
if pkill -f "mediamtx $CFG"; then
  echo "중계 정지 요청 · Linker/Sentinel의 중계 연결은 끊기며 재연결을 시도합니다."
else
  echo "실행 중인 Linker 중계 없음"
fi
