# Linker Tracking 구현·검증 기록 (2026-11-22)

# (1단계 기록은 아래 「1단계」 절부터. 2·3단계 현재 상태를 먼저 적는다.)

## 범위와 사용법

실제 `/Users/kimmunju/workspace/Linker_mac` 소스를 수정했다. 상단 **Tracking**은 새 관찰 화면이며 **Sentinel Legacy**는 기존 차량 장애물 경보 화면이다. `GuideVehiclePanel`, `VehicleSession`, `--guide-vehicle`, 기존 Legacy 브라우저 대시보드와 그 토큰/Host 검증/Qt 요청 큐는 유지한다. 2단계에서 **별도 Tracking 브라우저 대시보드**(`tracking_dashboard.py`)를 추가했고, 사용자 결정(2026-11-22)에 따라 이 대시보드에서 **비행 모드 변경 요청만** 허용한다. 허용/금지 범위와 남은 위험은 아래 「브라우저 비행 모드 변경」을 본다.

시작 시 Tracking은 MONITOR ONLY/IDLE이며 카메라·기체·Guide를 연결하지 않는다. 기존 Fly 또는 CH0 저장 URL을 초기 입력으로 사용한다. 주소/transport를 확인하고 영상 연결 → ROI 선택 → 영상 내부 드래그 순으로 사용한다. 프리셋은 제품별 예시이며 실제 HM30 주소라는 의미가 아니다. 기본 카메라 모드는 **기체 고정·비짐벌**, 별도 선택은 **지상 고정**이다. 두 모드 모두 사용자 ROI 관찰만 수행하며 자동 후보 검출이나 배경 운동 보정은 없다.

추적 정지는 관찰 결과와 모의 권한을 취소한다. LOST는 프레임이 돌아와도 복구되지 않고 새 ROI가 필요하다. 합성 모의 시험은 명시적 버튼으로만 시작하며 실제 입력과 구분해 SIMULATION으로 표시한다. 모의 출력 허용은 별도 조작이다. 로그 저장은 내부 관찰 및 모의 기록 JSON을 저장한다. source는 URL 대신 SHA256 식별자로 기록하며 자격증명 URL을 로그에 쓰지 않는다.


## 2·3단계 현재 구조 (2026-11-22)

- `tracking_service.py` `TrackingService`(Qt 스레드 단일 소유자): TrackingSession 1개, `_SharedCapture` 1개(같은 소스는 Fly·Video4CH와 디코더 공유), 선택 기체(읽기 전용), `FlightModeController`, 브라우저 명령 실행. `main_window.py`가 만들어 `TrackingPanel(self, service)`에 넘긴다. Sentinel Legacy(VehicleSession) 수명주기와 독립이다.
- `tracking_panel.py` PyQt 창 · `tracking_dashboard.py`+`tracking_dashboard.html` 브라우저 화면 · `dashboard_server.py` 공통 loopback 계층(Legacy `drive_dashboard`도 사용) · `flight_mode_controller.py` 모드 변경 트랜잭션 · `tracking_simulation.py` 모의 출력(메모리/파일).
- HTTP 스레드는 Qt 객체·추적기·기체를 만지지 않는다. 검증된 명령은 최대 16개 큐에 들어가고 Qt 스레드 `tick()`에서 실행된다.
- `TrackingService.event()`라는 이름이 `QObject.event(QEvent)`를 덮어써 Qt 이벤트 수신 즉시 TypeError로 종료되던 결함은 `log_event()`로 해결했다. `tests/test_tracking_service.py`가 QObject/QWidget 메서드 이름 재정의를 정적으로 검사하고 postEvent/sendEvent/자식 QTimer를 실제로 통과시킨다(백업본의 `event` 정의를 검출함을 확인).
- 상태 표시 규칙 통일: 합성 영상 또는 모의 출력 허용이면 SIMULATION(칩·배너·영상 표시 모두 `monitor_label()` 하나). 영상 프레임 나이가 추적 timeout 이상이면 STALE(이전에는 1초 기준이라 0.3~1초 구간이 LIVE로 보였음 — 캡처 검증 중 발견해 수정).

## 브라우저 비행 모드 변경 (사용자 결정: 허용, 2026-11-22)

허용: Tracking 대시보드의 `mode_prepare` → (브라우저 확인 패널, 체크박스) → `mode_commit`, `mode_cancel`. 대상은 `flight_mode_controller.OFFERED`에 있고 기체 펌웨어 매핑(ArduPilot: `mode_mapping_byname(mav_type)`, PX4: `px4_map`)에 실제 존재하는 모드뿐이다.

금지(브라우저·PyQt 모두): ARM/DISARM, TAKEOFF, AUTO/MISSION 시작, OFFBOARD(사전 setpoint 스트림 필요 — 더미 setpoint 미송신), RC·자세·위치·속도 명령, 파라미터 변경, 원시 MAVLink 명령/메시지 ID 요청. HTTP 검증기는 정해진 action과 키만 받으며 알 수 없는 키를 400으로 거부한다. AUTO/TAKEOFF/MISSION은 검증기를 통과해도 컨트롤러에서 거부되고 송신 0건임을 테스트로 확인한다.

안전 흐름(PyQt와 동일): 확인 티켓(TTL 30초) → commit 직전 재검증(같은 Vehicle 객체·link/sysid/compid·연결 epoch, 현재 선택 기체, HEARTBEAT 3초 이내, 티켓 발행 시점과 같은 모드·armed) → `MAV_CMD_DO_SET_MODE` 1회 송신 → 송신 후 도착한 해당 기체 HEARTBEAT의 모드로만 성공 판정. ACK ACCEPTED만으로 성공 처리하지 않으며 ACK 후 HEARTBEAT 3회 불일치는 FAILED, 5초 미확인은 TIMEOUT. 자동 재시도 없음, 기체당 60초 3회 한도.

동시 조작: 유효 티켓은 항상 1개. 새 prepare(앱이든 브라우저 탭이든)는 이전 미확인 티켓을 무효화(SUPERSEDED 기록)하고, 무효 티켓 commit은 거부되며 유효 티켓은 유지된다. 티켓은 만든 화면에서만 확인할 수 있다(app 티켓을 web에서, web 티켓을 app에서 commit 불가). 요청 기체 키가 현재 선택 기체와 다르면 거부·기록한다.

감사 기록: prepare/거부/무효화/송신/ACK/확인/취소/실패 각각을 출처(app/web), 기체 이름·link/sysid/compid·펌웨어·epoch, 이전·목표 모드, armed, 티켓, ACK와 함께 `mode_audit`에 남기고 「로그 저장」 JSON에 포함한다(최근 500건).

표시: 대시보드가 켜져 있는 동안 PyQt 창 상단에 주황색으로 「브라우저 대시보드 URL · 같은 추적 세션 공유 중 · 브라우저에서 비행 모드 변경 가능」을 상시 표시하고 닫기/다시 열기 버튼을 둔다. 브라우저 확인 패널은 ARMED일 때 빨간 대형 경고를 표시한다. 기본 포트 8766이 사용 중이면 임의 포트로 열리며 실제 URL을 창과 이벤트 로그에 표시한다.

**남은 위험**: 서버는 127.0.0.1에만 바인딩하고 Host/Origin/토큰을 검사하지만, 같은 Mac의 다른 로컬 프로세스는 `GET /`으로 토큰이 들어 있는 페이지를 받아 모드 변경 요청을 보낼 수 있다(기존 drive_dashboard와 같은 구조). 사용자 계정이 침해된 경우나 신뢰하지 않는 로컬 프로그램이 있으면 이 경로가 열린다. 완화책: 대시보드는 필요할 때만 열고 사용 후 「대시보드 닫기」로 끈다(서버 종료 시 브라우저 티켓도 취소). 모드 변경은 매번 확인 단계와 commit 직전 재검증을 거치고 감사 기록이 남지만, 이것이 로컬 프로세스 인증을 대신하지는 않는다. 향후 개선 후보: 페이지 로드 후 1회용 세션 쿠키(HttpOnly, SameSite=Strict) 교환, 또는 PyQt 창에서의 최종 승인 요구.

PyQt 창을 닫을 때 대시보드가 열려 있으면 「창만 닫기(대시보드 유지) / 대시보드도 닫기 / 취소」를 묻는다.

## ROI 선택 정책 (3단계)

- 「ROI 선택」은 현재 프레임을 고정한다. PyQt와 브라우저 모두 같은 고정 프레임을 표시하고, 요청은 그 프레임의 source/session/frame_seq/width/height를 정확히 지정해야 한다.
- 선택 시간 기본 8초(2~20초, 유한값 검증, PyQt에서 조정). 남은 시간을 두 화면에 크게 표시한다.
- 따라잡기(catch-up): 고정 프레임에서 초기화한 뒤 고정 이후 보관한 프레임(최대 12개, 넘치면 솎아냄)을 순서대로 추적해 최신 프레임까지 따라간다. 보관 간격은 경과 시간이 길수록 커져 광류가 불안정해지므로 **고정 후 4초(`CATCHUP_SPAN_S`) 이내**에만 따라잡기를 한다.
- 4초 초과~선택 시간 이내: ROI를 적용하지 않고 최신 프레임을 새로 고정해 「최신 프레임에서 다시 그리세요」를 안내한다. 선택 시간 초과도 같다. 따라잡기 중 특징점을 잃으면 LOST 대신 같은 안내로 재선택 상태가 된다. 어느 경우에도 다른 객체로 자동 전환하거나 이전 ROI를 새 프레임에 그대로 옮기지 않는다.
- 민무늬 거부(insufficient_texture) 시 「무늬가 뚜렷한 곳(모서리·글자·경계)을 선택하세요」를 표시한다.

## UI 스레드 성능 (3단계 측정, `tools/measure_tracking_ui.py`)

`tracking_validation/perf_ui_thread.md`에 결과가 있다. 720p/1080p/4K 모두 Qt 스레드 p95가 33 ms 미만이어서 JPEG 인코딩(≤10 Hz, 1280 폭 축소)과 표시 변환을 별도 워커로 옮기지 않았다. 추적 입력 해상도는 바꾸지 않았다. 단, 4K에서는 광류 워커 처리 속도가 약 11 fps로 떨어져 처리 프레임 사이 이동량이 커지고 합성 시험에서도 LOST(incoherent_motion)가 났다. HM30 실제 해상도에서 재측정이 필요하다.

## 녹화 재생 측정 (`tools/tracking_replay_metrics.py`)

로컬 녹화 파일만 받는다(RTSP/네트워크 주소는 거부해 HM30에 자동 연결하지 않음). 지정 ROI를 한 번 적용하고 파일 FPS로 재생하며 프레임별 상태·사유·품질·처리 FPS·프레임 나이를 CSV로, 프레임 나이 p50/p95/최대·timeout 초과 수·LOST 사유 분포·추적 유지 시간을 `.summary.json`으로 남긴다. timeout 0.3초 적정성은 실제 HM30 녹화로 이 도구를 돌린 뒤 판단한다.

# 1단계 기록

## 변경 전 보호

적용 경로 및 하위에서 AGENTS.md를 찾지 못했다. `.git`도 없었다. 시스템 git은 Xcode `_XPCTypeBool`/`libxcodebuildLoader.dylib` 오류로 실패했다. git 초기화/리셋을 하지 않았다. 기존 Python/spec/tests/XML 원본은 `tracking_backup_20261122/`에 복사했고 `sha256.json`으로 기록했다. 변경 전 테스트 로그는 그 아래 `baseline/`, 최종 전후 SHA256 및 diff는 `tracking_validation/changes.json`, `changes.diff`에 있다.

인접 Sentinel의 protocol/session/XML 및 의존 모듈은 읽기만 했다. 서명 키 파일이나 자격증명 내용은 열거나 출력하지 않았다. 테스트는 임시 시험 키만 생성한다. 외부 Sentinel 소스 변경은 없다.

## 이벤트·스레드·데이터 흐름

1. `_SharedCapture` Python 디코더 스레드 하나가 정규화한 소스별 pool을 공유한다. 첫 **실제 acquire**가 RTSP transport를 선택한다. Fly/Video4CH/Tracking이 같은 소스를 열어도 하나다.
2. 디코더 `read()` 반환 직후 monotonic 수신 시각을 기록한다. `FrameEnvelope`는 source_id/session_id/frame_seq/width/height/decoder_received_monotonic과 pixels를 갖는다. 원본 촬영 시각은 알 수 없으며 수신 시각을 촬영 시각이라고 표시하지 않는다.
3. pixels는 bytes가 소유하는 불변 NumPy view다. 소비자는 WRITEABLE을 다시 켤 수 없다. 직접 생성하는 envelope도 유효성 검사와 불변 소유권을 적용한다. pool은 최신 envelope 하나만 보관하고 소비자가 참조하는 이전 프레임만 수명이 연장된다.
4. Tracking UI는 33ms 타이머로 최신 프레임만 읽고 worker에는 최신 작업 슬롯 하나만 제출한다. 처리 중 작업과 pending 슬롯 외에 프레임 큐가 쌓이지 않는다. ROI 선택 작업은 다음 일반 프레임으로 덮어쓰지 않는다. Fly/Video4CH GUI도 최신 QImage 슬롯을 polling한다. 녹화·스냅샷은 기존 구독 worker에 남겨 UI polling과 분리했다.
5. `TrackingWorker` Python 스레드만 순수 `RoiTracker`를 소유한다. 코어는 goodFeaturesToTrack, pyramidal LK 전후방 오차, 밝기 잔차, 점 이동 일관성, 최초 점 수 대비 누적 잔존 비율, 보수적인 partial-affine scale/rotation 검사를 사용한다. 모델/온라인 분류기/YOLO/PyTorch/MIL/CSRT/KCF는 추가하지 않았다.
6. `TrackingSession`의 별도 watchdog은 UI/프레임 콜백/광류 처리와 무관하게 20ms마다 deadline을 검사한다. 취소 시 generation UUID를 바꿔 진행 중 늦은 결과를 버린다. 카메라 재연결·해상도 변경·파일 루프 재시작은 capture session을 바꾼다. 소스 해제는 이전 frame 참조도 지운다.
7. 상태는 IDLE/SELECTING/TRACKING/LOST/STOPPED/ERROR다. 결과에는 유효 bbox/중심/정규화 영상 오차(오른쪽·아래 양수)/최대 120점 trail/잔존 특징 품질/seq/수신·처리 시각/처리 FPS/이유가 있다. 클래스는 항상 UNKNOWN이다. quality는 클래스 확률이 아니다. LOST/ERROR/STOPPED에서는 bbox·중심·오차·trail과 모의 출력을 폐기한다.
8. 기본 timeout 0.3초는 시험값이며 UI에서 0.05~10초의 유한값으로 조정 가능하다. 변경은 세션을 교체한다. HM30 지연 측정 후 조정해야 한다.
9. 종료는 pool 참조 해제, 취소 generation 갱신, pending 삭제, worker/watchdog 중지 요청을 수행한다. native read는 QObject/QThread가 아닌 self-owned daemon capture에서 끝날 때까지 생존한다. UI가 native read를 기다리지 않는다. Python tracking worker도 native LK가 반환되면 종료한다. 무한히 멎은 native 호출을 강제 종료한다고 보장하지 않는다. 테스트는 지연 read와 정상/예외 worker 종료를 확인한다.

## Sentinel 호환성

wire 의미와 Python import를 분리했다. Linker 검증기에 합의된 health bit 2~6만 추가 수용하고 bit7은 거부한다. 구형 bit0/1도 수용한다. 메시지 ID 52200~52206, 필드/CRC/서명/순번/세션/UTC/TTL/ACK/스냅샷 의미는 바꾸지 않았다. 양쪽 XML이 동일하므로 XML과 생성된 `wow_guide_mavlink.py`는 수정하지 않았다. Guide의 GUIDE_HEARTBEAT/MODE_ACK는 Pixhawk의 HEARTBEAT/COMMAND_ACK와 다른 채널·의미다. Tracking은 Guide wire 이벤트를 만들지 않으며 bbox에 가짜 위경도·거리도 붙이지 않는다.

테스트 전용 `SentinelNamespace`는 외부 모듈별 import resolver를 사용한다. Sentinel 코드가 Sentinel의 guide_protocol/obstacle_geo를 참조하며 Linker의 전역 모듈 alias를 교체하지 않는다. sys.path에 Sentinel을 append하던 의존을 제거했다.

`tests/fixtures/sentinel_legacy.json`은 과거 Linker 프로필 계약, `sentinel_current.json`은 현재 Sentinel 필수 설정 계약이다. 구형 프로필이 현재 Sentinel에서 추가 필수 키 부족으로 거부되는 것, 현재 프로필 및 extends/STAGE2/3 처리, 순환 상속 거부, Linker Legacy가 STAGE1에 한정되는 것을 명시적으로 시험한다. **과거 Sentinel 바이너리 전체를 실행한 시험은 아니다.** 구형 wire health를 현재 Sentinel 전용 GuideLink로 생성해 실제 서명 직렬화/Linker 수신으로 확인했다.

현재 Sentinel은 demo의 센서를 정상으로 가장하지 않는다. 따라서 기존 통합 시험의 무경보 NORMAL 기대를 CAUTION으로 수정했고, VEHICLE 입력은 현재 클래스 매핑에 맞는 `car`로 제공했다. 탐지는 현재 인증 경계에 맞춘 in-process fixture로 제공한다. 20% 패킷 손실/ACK 재시도/중복/순서/오래된 시각/잘못된 서명/링크 단절 및 스냅샷 재동기화 시나리오는 유지한다. 데모 건강 상태를 강제로 정상으로 패치하지 않았다.

## Pixhawk 및 모의 출력 경계

Fleet의 기존 Vehicle에 메시지 종류별 monotonic 수신 시각과 읽기 전용 표시 캐시를 추가했다. Tracking 화면만 이를 읽으며 별도 MAVLink 연결/recv_match는 없다. link/sysid/compid, autopilot/firmware, mode/armed/heartbeat age, 자세, GPS fix, 배터리와 고도 기준을 표시한다. 미수신 값은 —, 3초 초과 표본은 stale이다. GLOBAL_POSITION_INT는 mm MSL/home-relative, VFR_HUD는 m MSL로 표시하며 초기 상대값을 AGL이라고 부르지 않는다.

기체 선택 변경은 즉시 세션/모의 권한을 취소한다. Pixhawk stale, 모드·armed·연결·COMMAND_ACK 상태 이벤트, 기존 RC override 활성 감지는 모의 권한을 취소한다. 영상 관찰은 Pixhawk stale과 별개로 유지된다. 별도 조종자 개입 버튼은 관찰까지 정지한다. 외부 RC 조종자의 모든 개입 의도를 추론하는 시스템은 구현하지 않았다.

SimulationSink 생성자는 Vehicle/master/mav를 받지 않는다. 기록은 정확한 TrackingResult 타입과 유한 expiry만 받으며 메모리/파일 데이터만 생성한다. 출력은 영상 오차 기록이지 비행 명령이 아니다. 실제 송신 구현이나 활성화 플래그는 없다. 실기 메서드 spy와 AST 검사에서 Tracking 관찰/모의 경로의 송신 호출은 0이다. 기존 `Linker._on_rc_override_timer`의 반복 송신은 변경하지 않았으며 향후에는 worker 정지만으로 이를 안전하게 해제할 수 있다고 가정하면 안 된다.

## 실행 명령과 검증 증거

기존 `.venv`만 사용했으며 패키지를 설치/교체하지 않았다.

```sh
.venv/bin/python tools/run_tracking_validation.py
# 내부적으로 각 모듈을 별도 프로세스로 실행:
QT_QPA_PLATFORM=offscreen .venv/bin/python -m unittest discover -s tests -p test_tracking_core.py
QT_QPA_PLATFORM=offscreen QT_SCALE_FACTOR=2 .venv/bin/python -m unittest discover -s tests -p test_tracking_ui.py
/usr/bin/env PATH=/usr/bin:/bin:/usr/sbin:/sbin .venv/bin/pyinstaller --noconfirm --workpath /tmp/linker_tracking_build --distpath tracking_validation/dist Linker_mac.spec
```

`tracking_validation/results.json`, `summary.log`, 각 `test_*.log`가 최종 결과다. 전체 discover는 실행하지 않았다. baseline에는 Guide/대시보드 import 실패, health UDP bind 권한 실패가 기록돼 있다. 최종에서도 환경이 localhost TCP/UDP bind를 거부하므로 HTTP 서버 5개와 health 실 UDP 1개 시험은 PermissionError로 남는다. 이를 skip이나 통과로 바꾸지 않았다. 메모리 네트워크 Guide 통합과 대시보드 snapshot은 실행한다.

오프스크린 화면: `tracking_offscreen.png`, `tracking_themed_offscreen.png`, `main_window_offscreen.png`. 실제 Qt 이벤트로 letterbox 바깥 ROI 거부, 3840×2160 원본/줌/resize 좌표 변환, QT_SCALE_FACTOR=2를 시험했다. 메인 창 시험은 실제 레이아웃/테마를 사용하되 지도 WebEngine 내용만 inert QWidget으로 대체하고 연결 함수를 spy로 막았다. OS 화면에 실제 창을 띄운 실사용 검증은 아니다.

합성 평행이동 bbox 오차, 작은 회전/scale, 가림, 무텍스처, dtype/shape/ROI/시간/seq 오류, timeout, worker 지연·예외·늦은 결과, 소스/세션/해상도 변경, 사용자 정지/종료, 기체 변경/stale 모의 취소, 동일 소스 6구독자, 녹화·스냅샷, blocking native read와 worker 종료를 시험했다. MP4는 합성 프레임을 인코딩한 파일 재생 시험이며 실제 비행 녹화물은 아니다.

Python 3.12.1 arm64, Qt 5.15.14 arm64, OpenCV 5.1.0 arm64/CSRT API 없음. 설치 GStreamer dylib는 x86_64여서 arm64 런타임 로드 실패. 빌드는 Analysis/PYZ 후 시스템 lipo/Xcode 오류로 실패했으며 `build.log`에 보존했다. 기존 dist/build는 덮어쓰지 않았다. 새 모듈은 Linker_mac.spec hiddenimports에 반영했다. 배포 가능한 새 .app 생성은 완료하지 못했다.

## 남은 실영상 시험과 향후 승인 기준

HM30의 실제 주소·transport 확인, 실측 영상 지연/프레임 간격 및 300ms 정책 조정, 기체 진동·롤링셔터·급격한 자세 변화·역광·저조도·가림·다른 객체 교차·재연결·녹화 병행 장시간 운용은 미검증이다. partial affine는 제한된 영상 움직임만 다루며 움직이는 기체의 ego-motion/3D 거리/장면 의미를 해석하지 않는다. 자동 재식별·다른 객체 전환을 하지 않는다.

실기 제어는 미구현이다. 합성/MP4/메모리 차량 시험은 비행동역학 SITL 검증이 아니며 `sim_multi_vehicle.py`도 autopilot SITL과 동일하지 않다. 비행 준비 완료를 선언하지 않는다.

향후 별도 제어 작업 승인 전에 최소 다음을 충족해야 한다.

- ArduPilot: 실제 ArduCopter SITL에서 승인된 GUIDED 입력 종류, 모드/arming 조건, COMMAND_ACK와 실제 상태 확인, 명시적 handover, RC 수동 개입, GUIDED timeout 및 기존 RC override 해제/타이머 중단 계약을 버전별 확인한다.
- PX4: 실제 PX4 SITL에서 Offboard 진입 조건/주기, offboard-loss 동작, RC 모드 전환, 위치 추정 상실 및 COMMAND_ACK/실제 상태 확인을 버전별 검증한다. ArduPilot 계약을 그대로 사용하지 않는다.
- 양쪽 모두 영상/텔레메트리/worker/프로세스 단절, 지연 결과, 선택 기체 변경, 재부팅·재접속·sysid/compid 충돌, 명령 expiry 후 재전송 금지, 수동 takeover 우선권을 독립 watchdog과 실제 autopilot 수신 로그로 증명한다.
- 비행 파라미터/failsafe 변경은 별도 검토·승인·원복 계획 후 수행한다. 기존 설정을 자동으로 바꾸지 않는다. 자동 추격·접근·충돌 유도는 이 설계의 범위가 아니다.
- SITL/HIL 결과와 handover/failsafe 검토를 문서로 승인한 뒤에도 실기 시험은 별도 제한 구역·안전조종자·중단 절차·명시적 승인 아래 진행한다.

## 1단계 최종 결과 집계 (2단계 이후 결과는 tracking_validation/results.json)

모듈별 독립 프로세스: **93개 실행, 87개 통과, 6개 오류, 스킵 0개**. 별도 HiDPI 실행은 Tracking UI 5개 통과이며 93개 집계에 중복 포함하지 않았다.

| 모듈 | 실행 | 결과 |
|---|---:|---|
| test_ai_tuner | 11 | 통과 |
| test_drive_dashboard | 6 | snapshot 1 통과, HTTP bind 5 오류 |
| test_flight_actions | 3 | 통과 |
| test_guide_stage1 | 12 | 통과 |
| test_health_data | 6 | 5 통과, 실제 UDP bind 1 오류 |
| test_safety_parameters | 5 | 통과 |
| test_safety_validation | 5 | 통과 |
| test_sentinel_compat | 6 | 통과 |
| test_shared_video | 8 | 통과 |
| test_tracking_core | 14 | 통과 |
| test_tracking_main_window | 1 | 통과 |
| test_tracking_replay | 1 | 통과 |
| test_tracking_session | 10 | 통과 |
| test_tracking_ui | 5 | 통과 |

실제 변경 파일은 `tracking_validation/changes.json`에 전후 해시와 함께 모두 열거했다. 주요 제품 파일은 `tracking_core.py`, 새 `tracking_models.py`/`tracking_worker.py`/`tracking_session.py`/`tracking_panel.py`, `video_grid.py`, `fleet.py`, `main_window.py`, `guide_protocol.py`, `guide_vehicle_panel.py`, `Linker_mac.spec`이다. 테스트/fixture/실행 스크립트/이 문서도 변경 목록에 포함한다.
