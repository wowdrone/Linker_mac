# Codex CLI 프롬프트 — 포뮬러 가이드 드론 장애물 경보 기능 (Linker + Sentinel GCS)

> 사용법
> 1. Linker와 Sentinel GCS가 같은 상위 폴더 아래에 있으면 그 상위 폴더에서 `codex`를 실행하고 아래 [프롬프트 본문]을 통째로 붙여넣습니다.
> 2. 저장소가 따로 떨어져 있으면 각 저장소에서 따로 실행하고, 본문 맨 위 `작업 대상` 줄만 바꿔서 쓰세요.
>    - Linker 폴더 → `작업 대상: LINKER만`
>    - Sentinel 폴더 → `작업 대상: SENTINEL만`
>    - 두 번째 저장소 작업 때는 첫 번째에서 만든 `docs/obstacle_protocol.md`를 복사해 넣어 주세요(메시지 규격을 같게 맞추기 위해).
> 3. Codex가 PHASE 0 보고서를 내면 내용을 확인한 뒤 "진행해"라고 답하세요.

---

## [프롬프트 본문]

작업 대상: LINKER + SENTINEL (둘 다)

### 1. 배경

우리는 포뮬러 경주차 앞을 먼저 날아가며 코스를 정찰하는 "가이드 드론"을 개발 중이다.

- 드론: ArduPilot 기반 FC + AI 미션컴퓨터(Jetson 계열) + AI 카메라, LTE/무선 데이터링크
- 차량 속도: 200~300km/h (약 55~83 m/s)
- 드론은 차량보다 300~800m 앞서 비행한다.
- **Linker**: 드론 쪽 링크/중계 소프트웨어 (FC·AI 카메라·통신을 연결)
- **Sentinel GCS**: 지상/차량 쪽 관제 소프트웨어. 이번 작업에서는 "차량 GCS"(차량 피트 또는 차량 탑재) 역할을 맡는다.

### 2. 목표 기능

드론이 코스 위 장애물(파편, 정지 차량, 사람, 동물, 오일/물 노면 등)을 발견하면:

1. Linker가 탐지 결과를 **지리 좌표 + 코스 위치**로 변환하고
2. 위험도를 판정해 **장애물 경보 메시지**를 차량 GCS로 보낸다.
3. Sentinel GCS는 메시지를 받아 **차량 기준 남은 거리·도달 시간(TTA)**을 다시 계산하고
4. 화면 경고 + 소리 경고 + 회피 권고(감속 / 좌측 회피 / 우측 회피 / 정지)를 표시하며
5. 수신 확인(ACK)을 드론에 돌려보낸다.

중요: 이 기능은 **권고(advisory) 전용**이다. 차량 제어(조향·브레이크)에 직접 명령을 보내는 코드는 절대 만들지 마라.

### 3. PHASE 0 — 먼저 조사하고 보고 (코드 수정 금지)

코드를 고치기 전에 두 저장소를 읽고 아래를 짧게 보고한 뒤 멈춰라.

- 언어, 프레임워크, 빌드 방법, 실행 방법, 테스트 방법
- 현재 통신 방식 (MAVLink / UDP / TCP / WebSocket / ZeroMQ / MQTT 등), 포트, 직렬화 형식
- AI 카메라 탐지 결과가 지금 어디로 어떤 형태로 들어오는지 (없으면 "없음")
- 드론 위치·자세(GPS, attitude, 짐벌 각도)를 어디서 얻는지
- Sentinel의 지도/화면 구조, 경고 표시 방식, 로그 저장 방식
- 기존 코드 스타일, 설정 파일 위치, 테스트 폴더 구조
- 아래 4~7장의 설계를 이 저장소에 맞추려면 **바꿔야 할 점** (예: 이미 MAVLink를 쓰면 MAVLink 커스텀 메시지로, 아니면 JSON/UDP로)
- 수정·추가할 파일 목록 계획

보고 후 내가 "진행해"라고 할 때까지 파일을 수정하지 마라.

### 4. 메시지 규격 (두 저장소 공통)

`docs/obstacle_protocol.md`에 먼저 규격을 쓰고, 두 저장소가 **같은 규격**을 쓰게 하라.
기존 통신이 MAVLink면 커스텀 메시지(dialect XML)로, 아니면 JSON(UTF-8) over UDP로 구현한다.
어느 쪽이든 필드는 아래와 같다.

**OBSTACLE_ALERT** (드론 → 차량 GCS)

| 필드 | 형식 | 설명 |
|---|---|---|
| proto_ver | int | 규격 버전, 1부터 |
| msg_id | uint32 | 메시지 고유 번호 (재전송 시 같은 값) |
| obstacle_id | uint32 | 추적 ID (같은 장애물은 같은 ID 유지) |
| seq | uint32 | 송신 순번 |
| t_detect_utc_ms | uint64 | 탐지 시각 (GPS/UTC ms) |
| t_send_utc_ms | uint64 | 송신 시각 |
| type | enum | DEBRIS, VEHICLE, PERSON, ANIMAL, SURFACE_HAZARD, UNKNOWN |
| confidence | float 0~1 | AI 신뢰도 |
| lat, lon | double | 장애물 위치 (WGS84) |
| pos_err_m | float | 위치 추정 오차 반경(m) |
| size_m | float | 추정 크기(m) |
| track_s_m | float | 코스 중심선 기준 진행 거리(m), 코스 파일 없으면 -1 |
| lateral_m | float | 중심선 기준 좌(-)/우(+) 오프셋(m) |
| free_side | enum | LEFT, RIGHT, BOTH, NONE, UNKNOWN |
| severity | enum | INFO, CAUTION, WARNING, CRITICAL |
| advice | enum | NONE, SLOW, AVOID_LEFT, AVOID_RIGHT, STOP |
| ttl_ms | uint32 | 이 경보의 유효 시간 |
| state | enum | NEW, UPDATE, CLEARED |
| drone_lat, drone_lon, drone_alt_m | double/float | 송신 시점 드론 위치 |

**OBSTACLE_ACK** (차량 GCS → 드론): msg_id, obstacle_id, t_recv_utc_ms, t_ack_utc_ms

**LINK_HEARTBEAT** (양방향, 1Hz): node_id, t_utc_ms, status

### 5. Linker (드론 쪽) 구현

모듈은 기존 구조에 맞게 나누되, 대략 다음 역할로 만든다.

1. **detection_input**: AI 카메라 탐지 결과 수신 어댑터.
   기존 입력이 없으면 UDP JSON 입력(`class, confidence, bbox, depth_m(optional)`)을 받는 어댑터를 만들고, 인터페이스로 분리해 나중에 교체 가능하게 하라.
2. **geo_projector**: 픽셀 좌표 → 지상 좌표 변환.
   드론 GPS·고도·자세·짐벌 각도·카메라 내부 파라미터(설정 파일)를 사용한다.
   depth가 있으면 depth 우선, 없으면 평지 가정(ground plane intersection)으로 계산하고 `pos_err_m`을 함께 추정한다.
3. **track_mapper**: 코스 중심선 파일(GeoJSON LineString 또는 CSV lat,lon)을 읽어 `track_s_m`, `lateral_m`, `free_side` 계산. 코스 폭은 설정값.
4. **obstacle_tracker**: 같은 장애물 중복 제거 (거리 임계값 + 시간창), obstacle_id 유지, 일정 시간 안 보이면 CLEARED 송신.
5. **risk_evaluator**: 위험도·권고 판정. 규칙은 설정 파일로 빼고 기본값은:
   - 코스 폭 밖(lateral 기준) → INFO
   - 코스 안 + confidence < 0.5 → CAUTION
   - 코스 안 + confidence ≥ 0.5 → WARNING
   - 코스 안 + 크기 ≥ 0.3m 또는 VEHICLE/PERSON → CRITICAL
   - advice: free_side가 LEFT면 AVOID_LEFT, RIGHT면 AVOID_RIGHT, NONE이면 STOP, BOTH/UNKNOWN이면 SLOW
6. **alert_sender**: 송신 + 신뢰성
   - CRITICAL/WARNING은 ACK 올 때까지 100ms 간격 재전송, 최대 10회
   - 같은 obstacle_id UPDATE는 최대 5Hz로 제한
   - 링크 끊김(heartbeat 3초 무응답) 시 로그 + FC/GCS 기존 경고 채널로 알림
7. **log**: 모든 탐지·송신·ACK를 CSV 또는 JSON Lines로 저장 (지연 분석용)

### 6. Sentinel GCS (차량 쪽) 구현

1. **수신기**: 규격 검증(proto_ver, 필수 필드, 시간 역행), 중복 msg_id 무시, ACK 즉시 회신.
2. **차량 기준 재계산**: 차량 GPS(기존 데이터 소스 사용, 없으면 설정 가능한 입력 어댑터)로
   - 남은 거리 = 장애물 track_s − 차량 track_s (코스 파일 없으면 직선거리)
   - TTA(도달 시간) = 남은 거리 / 차량 속도
   - 이미 지나친 장애물(남은 거리 < 0)은 자동 해제
3. **경고 표시** (시인성 최우선, 운전자·피트 크루가 한눈에 볼 수 있게)
   - 화면 상단 큰 경고 배너: 위험도 색상(INFO 회색, CAUTION 노랑, WARNING 주황, CRITICAL 빨강) + 큰 글씨로 `남은 거리 m / TTA 초 / 권고`
   - 권고는 큰 화살표 아이콘(←, →, ↓감속, ■정지)
   - 지도/코스 화면에 장애물 마커 + 오차 원
   - 글꼴은 크고 굵게, 고대비 색상 사용 (작은 글씨·얇은 선 금지)
4. **소리 경고**: WARNING 이상은 경고음, CRITICAL은 반복음. TTA가 짧을수록 반복 빨라지게.
5. **경보 목록 패널**: 활성 장애물 목록, 수동 해제 버튼, ttl 만료 자동 제거.
6. **로그**: 수신 시각, 지연(t_recv − t_detect), ACK 시각 저장.

### 7. 설정 파일

두 저장소 모두 하드코딩 금지. 기존 설정 방식에 맞춰 아래 항목을 추가:
포트/주소, 카메라 파라미터, 코스 파일 경로, 코스 폭, 위험도 임계값, 재전송 횟수·간격, heartbeat 주기, TTL 기본값, 소리 on/off.

### 8. 테스트와 시뮬레이터

1. **단위 테스트**: geo_projector(알려진 자세·고도에서 좌표 검증), track_mapper, risk_evaluator, 중복 제거, 메시지 직렬화/역직렬화 왕복.
2. **가짜 탐지 주입기** `tools/fake_detector`: 지정 좌표·종류의 장애물을 Linker 입력으로 보냄.
3. **가짜 차량 주행기** `tools/fake_car`: 코스 파일을 따라 250km/h로 이동하는 차량 GPS를 Sentinel에 공급.
4. **통합 테스트**: 로컬(127.0.0.1)에서 Linker ↔ Sentinel 연결, 장애물 1개 주입 → 경고 표시 → ACK → 차량 통과 후 해제까지 자동 확인.
5. 패킷 손실 20% 상황에서 CRITICAL 경보가 ACK까지 도달하는지 테스트.

### 9. 합격 기준

- 탐지 → Sentinel 화면 표시까지 로컬 지연 150ms 이하 (로그로 측정해 보고)
- 손실 20%에서도 CRITICAL 경보 100% 도달
- 같은 장애물이 경고 목록에 중복으로 뜨지 않음
- 차량이 지나간 장애물은 자동 해제
- 기존 기능·기존 테스트가 깨지지 않음
- 차량 제어 명령을 보내는 코드가 없음

### 10. 작업 규칙

- 작은 단위로 나눠 커밋 가능한 상태로 진행하고, 각 단계 끝에 테스트를 돌려라.
- 기존 코드 스타일과 폴더 구조를 따르고, 새 외부 라이브러리는 꼭 필요할 때만, 추가 이유를 적어라.
- 개발 PC는 Windows 10 + Git Bash다. 실행 명령은 Git Bash 기준으로 써라.
- 나는 Python 초급이다. 주요 함수에는 한국어 주석을 달아라.
- 끝나면 다음을 보고하라:
  1. 변경·추가 파일 목록
  2. 실행 방법 (Linker, Sentinel, fake_detector, fake_car 순서)
  3. 테스트 결과와 측정 지연
  4. 실제 비행 전에 내가 직접 확인·보정해야 할 항목 (카메라 캘리브레이션, 코스 파일, 임계값 등)
