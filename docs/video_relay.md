# HM30 영상 단일 수신 구조 — 로컬 RTSP 중계 (2026-11-22)

## 왜 필요한가

- Linker 안에서는 Fly / Video 4CH / Tracking이 같은 주소면 `_SharedCapture` 디코더 1개를 공유한다. 브라우저(Tracking 대시보드, Sentinel Legacy drive 대시보드)는 카메라에 붙지 않고 Linker가 만든 JPEG를 127.0.0.1 HTTP로 받는다.
- WOW Sentinel(`/Users/kimmunju/workspace/sentinel/track.py`)은 같은 카메라에 자기 RTSP 연결을 따로 연다(`rtsp_transport;tcp|fflags;nobuffer`). 따라서 Linker와 Sentinel을 함께 쓰면 HM30 무선 링크로 같은 영상이 최소 2번 흐른다.
- HM30 사용자·제조사 포럼에는 카메라 RTSP를 여러 클라이언트가 직접 열면 무선 부하가 늘고 안정 스트림이 1개로 줄며 통신 거리가 감소한다는 보고가 있다. **이 저장소에서 실측하지는 않았다.**
- 목표 구조:

```
카메라 ─(HM30, RTSP 1개, TCP)─▶ GCS Mac 로컬 중계 (mediamtx, 127.0.0.1:8554/cam)
                                 ├─▶ Linker (공유 디코더 1개) ─▶ 브라우저들 (JPEG, 관찰용)
                                 └─▶ Sentinel (객체 추적·추적 비행)
```

## 설치 상태와 설정 근거

- 2026-11-22 이 Mac: **mediamtx 미설치**, ffmpeg 미설치, Homebrew 있음. 설치는 자동으로 하지 않았다. 설치 명령: `brew install mediamtx`
- `tools/video_relay/linker_relay.yml`의 키 이름은 공식 저장소(bluenviron/mediamtx) 최신 릴리스 **v1.21.4** 기준 reference `mediamtx.yml`과 공식 문서(환경 변수 `MTX_<PARAM>`, `MTX_PATHS_<NAME>_SOURCE`, 제어 API `/v3/paths/list`)에서 확인했다. 설치된 바이너리로 확인한 것은 아니므로 설치 후 `mediamtx --version`과 실제 시작 결과로 다시 확인해야 한다.
- 최신 main 브랜치 reference에는 `moq` 서버(기본 켜짐)가 있으나 v1.21.4 포함 여부를 확인하지 못해 설정에 넣지 않았다(알 수 없는 키는 설정 오류가 될 수 있음). 대신 시작 스크립트가 시작 직후 127.0.0.1/::1 이외 주소에서 열린 포트를 찾아 경고한다.

## 구성 파일과 스크립트 (`tools/video_relay/`)

| 파일 | 역할 |
|---|---|
| `linker_relay.yml` | RTSP만 127.0.0.1:8554 TCP로 제공(`rtspTransports: [tcp]`), API는 127.0.0.1:9997, RTMP/HLS/WebRTC/SRT·metrics·pprof·playback 끔, 읽기·API는 127.0.0.1/::1만 허용, 외부 publish 금지, 경로 `cam` 하나(`rtspTransport: tcp`, `sourceOnDemand: no`). 카메라 주소는 파일에 없음. |
| `video_relay_start.command [rtsp://…]` | mediamtx 존재·버전 확인 → 카메라 주소(인자 또는 같은 폴더 `camera.local` 첫 줄, 예시는 `camera.local.example`) → `MTX_PATHS_CAM_SOURCE`로 전달해 백그라운드 실행. 화면·로그(`~/Library/Logs/linker_video_relay.log`)의 URL 자격증명은 `***`로 가린다. 8554 포트 충돌·중복 실행·비루프백 포트를 검사한다. |
| `video_relay_stop.command` | 이 설정으로 실행된 mediamtx만 종료한다. |
| `video_relay_status.command` | 프로세스, 수신 대기 포트, API의 경로별 카메라 수신 여부·읽는 쪽 수를 출력한다(`ready` 필드는 설치 버전에서 확인 필요). |

주의: 비밀번호가 든 카메라 주소는 실행 중 mediamtx 프로세스의 환경 변수에 있으므로 같은 사용자 계정의 프로세스는 볼 수 있다. `camera.local`은 `chmod 600`으로 두고 저장소에 올리지 않는다(.gitignore 제안 참고).

Linker는 중계를 **자동 실행하지 않는다**. 시작 시 자동 연결 금지 원칙을 유지한다.

## Linker 쪽 동작 (구현·시험 완료 범위)

- 영상 프리셋 「로컬 중계(권장)」 = `rtsp://127.0.0.1:8554/cam`. 실제 HM30 주소라는 뜻이 아니다. 중계가 꺼져 있으면 영상 상태에 「로컬 중계 서버 응답 없음 · 127.0.0.1:8554」를 표시한다.
- 디코더 풀 키 정규화: 스킴·호스트 소문자, 기본 포트(rtsp 554, rtsps 322, http 80, https 443, rtmp 1935) 생략, 끝의 `/` 제거. 경로·쿼리 대소문자는 유지. **자격증명 규칙**: 자격증명이 있는 주소는 없는 주소·다른 자격증명 주소와 디코더를 공유하지 않는다(키에는 user:password의 짧은 해시만 넣고 비밀번호는 넣지 않음).
- Linker 자기 풀에서 같은 카메라 호스트(루프백 제외)로 RTSP 디코더가 2개 이상이면 Tracking 창·브라우저·이벤트 로그에 「같은 카메라에 직접 연결 n개 · 무선 링크 부하 증가 · 로컬 중계 사용 권장」. Sentinel 등 다른 프로그램의 연결은 세지 않는다.
- 디코딩 밀림 감지(`video_health.py`): 최근 5초 평균 수신 FPS가 소스 FPS의 80% 미만이거나, read() 간격이 프레임 주기의 25% 미만으로 8번 이상 연속되면(버스트) 「디코딩 밀림·지연 누적 가능」. QSettings `video/decode_health/{window_s,ratio,burst_fraction,burst_run,hold_s}`로 조정(범위 검증, 잘못된 값은 기본값).
- 영상 상태에 경로(직접 연결 / 로컬 중계 / 장치 / 파일 / 합성)와 재연결 횟수를 표시. 브라우저에는 「브라우저 영상 나이」(앱 디코더 수신 → 브라우저 표시) 숫자를 표시.
- 중계 단절·복구 시험(`tests/test_video_relay.py`): mediamtx가 없어 **실제 중계 서버로는 시험하지 못했다**. 대신 캡처 경계(`VideoWorker._open`)에서 녹화 MP4를 30 fps로 내보내다 40프레임 뒤 끊기는 중계를 흉내 냈다. 결과: 재연결 1회, session_id 변경, 추적은 `source_session_or_resolution_changed`로 LOST·bbox 폐기, 이후 프레임이 계속 와도 자동 재개 없음.

## 비교 측정 절차 (①②③)

같은 카메라·HM30·해상도·코덱·비트레이트·거리에서 차례로 측정한다. 각 구성마다 Linker와 Sentinel을 모두 껐다가 다시 켠다.

| 구성 | Linker 영상 주소 | Sentinel 영상 주소 |
|---|---|---|
| ① 직접 연결 1개 | 카메라 RTSP 직접 | Sentinel 끔 |
| ② 직접 연결 2개 | 카메라 RTSP 직접 | 카메라 RTSP 직접(Sentinel 화면의 소스 입력란) |
| ③ 로컬 중계 경유 | `rtsp://127.0.0.1:8554/cam` | `rtsp://127.0.0.1:8554/cam` |

1. ③에서만 `tools/video_relay/video_relay_start.command rtsp://카메라주소/경로` 실행, `video_relay_status.command`로 「카메라 수신 중」 확인.
2. Linker 링크 지표(각 구성 120초 이상):
   `.venv/bin/python tools/video_link_metrics.py <Linker 주소> --seconds 120 --label ① --out tracking_validation/link_①.csv`
   CSV(초당 수신 FPS·프레임 나이 p50/최대·끊김(기본 500 ms 초과 간격)·재연결·세션 변경·디코딩 경고)와 `.summary.json`이 남는다. 프레임 나이는 이 Mac의 디코더 수신→샘플링 구간이며 카메라·전송 지연은 포함하지 않는다.
3. 표시→수신(glass-to-glass에 가까운) 지연: Sentinel 도구를 **읽기 전용으로** 실행한다. Sentinel 소스는 수정하지 않는다.
   `cd /Users/kimmunju/workspace/sentinel && .venv/bin/python tools/measure_video_latency.py --source <같은 주소> --camera "<카메라>" --path "①/②/③" --count 20`
   - `--save`는 쓰지 않는다(Sentinel 저장소의 `docs/video_latency.md`를 수정하므로). 출력된 중앙값/최소/최대를 아래 표에 옮겨 적는다.
   - ②에서는 Sentinel 본체가 켜진 상태에서 이 도구가 세 번째 연결이 되므로, Sentinel 본체 대신 이 도구를 Sentinel 쪽 연결로 쓰고 Linker는 켜 둔다(측정 조건을 비고에 기록).
4. 끊김·깨짐은 화면 관찰과 링크 지표의 gaps/재연결을 함께 적는다.

## 기록 표

실측 전에는 「미측정」으로 둔다. 추정값을 넣지 않는다.

| 날짜 | 카메라 | 해상도/코덱/비트레이트 | 구성 | 거리 | 지연 중앙값/최소/최대 (ms) | 끊김·깨짐 | 재연결 | 비고 |
|---|---|---|---|---|---|---|---|---|
| 미측정 | 미측정 | 미측정 | ① 직접 1개 | 미측정 | 미측정 | 미측정 | 미측정 | |
| 미측정 | 미측정 | 미측정 | ② 직접 2개 | 미측정 | 미측정 | 미측정 | 미측정 | |
| 미측정 | 미측정 | 미측정 | ③ 로컬 중계 | 미측정 | 미측정 | 미측정 | 미측정 | |

## 결정 (사용자)

- 로컬 중계를 운용 기본값으로 사용: ☐ 예 ☐ 아니오 ☐ 추가 측정 후 결정
- 결정일: ________  결정자: ________  근거(표의 행): ________

중계가 한 단계 더 생기므로 지연이 늘 수도 있고, 무선 링크 부하 감소로 오히려 안정될 수도 있다. 판단은 위 표의 실측으로만 한다.
