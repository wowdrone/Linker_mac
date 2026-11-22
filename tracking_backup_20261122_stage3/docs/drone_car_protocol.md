# Sentinel–Linker 주행 정보 프로토콜 v2

Sentinel(시스템 ID 1)은 드론, Linker(시스템 ID 2)는 차량이다. `protocol/wow_guide.xml`의 독립 MAVLink 2 dialect를 기존 FC 링크와 분리된 **서명된 UDP 데이터그램**으로 보낸다. 피어 주소는 주·보조 링크별로 설정한다. 두 링크는 같은 논리 메시지를 전송하되 각각 서명 스트림을 사용한다. 서명 키는 외부 파일 또는 `WOW_GUIDE_SIGNING_KEY_FILE`에서 읽으며 코드에 포함하지 않는다.

| 메시지 | ID | 송신 | 기본 주기 |
| --- | --- | --- | --- |
| GUIDE_HEARTBEAT | 52200 | 양방향 | 10 Hz, 상태와 동기화 정보 |
| CAR_STATE | 52201 | Linker | 20 Hz |
| DRONE_STATE | 52202 | Sentinel | 10 Hz |
| OBSTACLE_ALERT | 52203 | Sentinel | 신규/갱신, 장애물별 최대 5 Hz |
| HAZARD_CLEAR | 52204 | Sentinel | 해제 사건 |
| DRIVE_ADVICE | 52205 | Sentinel | 경보별 권고 사건 |
| MODE_ACK | 52206 | Linker | 수신 검증 결과와 현재 판단 모드 |

모든 프레임에는 규격 번호 2, 논리 메시지 ID, 메시지 종류별 순번, 세션 ID, 송신 시각이 있다. 방향, 서명, 시각과 범위를 검증하고 같은 세션·메시지 ID는 한 번만 적용한다. 선행 순번이 이미 적용된 스트림에서 뒤늦은 값은 폐기한다. 경보와 해제는 **장애물 ID별** 변화 순서를 비교한다. 논리 메시지 ID와 절대 만료 시각은 재전송 중 유지한다. 송신 시각·프레임 서명은 매 시도 갱신한다. 수신 시각 허용 범위는 프로필 기준이며 STAGE1의 과거 허용은 300 ms다. 장애물 TTL은 최초 탐지 UTC부터 잰다. ACK는 수신 검증 및 차량의 현재 계산 모드이며 액추에이터 실행 확인이 아니다.

WGS84 위도·경도는 도, 고도와 거리·오프셋은 m, 속도는 m/s, 시각은 UTC Unix ms다. 장애물의 `track_s_m`과 크기 −1은 미상이다. `lateral_m` 음수는 코스 좌측이다. 장애물 유형 값은 UNKNOWN 0, DEBRIS 1, VEHICLE 2, PERSON 3, ANIMAL 4, SURFACE_HAZARD 5이며, 여유 방향은 UNKNOWN 0, LEFT 1, RIGHT 2, BOTH 3, NONE 4다. 위험도는 INFO 0, CAUTION 1, WARNING 2, CRITICAL 3이고, 수명 상태는 NEW 0, UPDATE 1, CLEARED 2다. 주행 모드는 NORMAL 0, CAUTION 1, AVOID 2, STOP 3, MANUAL 4, LINK_DEGRADED 5다.

Heartbeat health bit 0은 센서 준비, bit 1은 Linker의 재동기화 요청이다. Sentinel은 요청을 받으면 현재 유효한 장애물의 스냅샷을 보내고 epoch와 개수를 heartbeat에 실어 알린다. Linker는 해당 epoch의 장애물 수가 일치하면 동기화 완료로 처리한다. 개수가 0이면 즉시 완료한다. 동기화 중과 통신 상실 중에는 정상 자동 주행으로 복귀하지 않는다. Linker와 Sentinel 모두 도착 UTC와 송신 UTC의 차이를 기록한다. 이 값에는 편도 지연과 시계 차이가 함께 포함된다.

보내는 값은 판단·권고, 위치와 상태다. Linker의 현재 차량 출력 어댑터는 로컬 시뮬레이터 전용이다. Sentinel의 선행 비행·이탈 출력도 시뮬레이터 전용이며 기존 ArduPilot failsafe를 변경하지 않는다.
