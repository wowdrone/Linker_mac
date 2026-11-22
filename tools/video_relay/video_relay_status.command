#!/bin/bash
# Linker 로컬 RTSP 중계 상태: 프로세스, 수신 대기 포트, mediamtx API의 경로 상태.
cd "$(dirname "$0")" || exit 1
CFG="$PWD/linker_relay.yml"
PID="$(pgrep -f "mediamtx $CFG" | head -1)"
if [ -z "$PID" ]; then echo "중계 꺼짐"; exit 1; fi
echo "중계 실행 중 · PID $PID"
echo "수신 대기 포트:"; lsof -nP -a -p "$PID" -i 2>/dev/null | awk 'NR>1{print "  " $8, $9}'
JSON="$(curl -s --max-time 2 http://127.0.0.1:9997/v3/paths/list || true)"
if [ -z "$JSON" ]; then echo "API 응답 없음 (127.0.0.1:9997)"; exit 2; fi
printf '%s' "$JSON" | /usr/bin/python3 -c '
import json, sys
data = json.load(sys.stdin)
for item in data.get("items", []):
    ready = item.get("ready")
    readers = item.get("readers")
    state = "카메라 수신 중" if ready else ("카메라 미연결" if ready is False else "ready 필드 없음(버전 확인)")
    name = item.get("name")
    count = len(readers) if isinstance(readers, list) else "—"
    print("  경로 %s · %s · 읽는 쪽 %s" % (name, state, count))
'
