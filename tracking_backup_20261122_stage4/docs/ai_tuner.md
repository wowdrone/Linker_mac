# Linker AI Tuner

AI Tuner의 기존 화면에는 가상 RC 스틱과 연결되지 않은 PID 버튼이 있었습니다.
현재 화면은 분석·검토용입니다. 분석, 파라미터 읽기, 승인된 PID 쓰기에서 RC,
Arm, 이륙, 모드 전환 및 자동 비행 명령을 보내지 않습니다. 기체 선택을 바꾸면
진행 중인 대상 의존 작업을 취소하고 다음 쓰기를 중단합니다.

## 분석 입력

- Flight Analysis에서 ArduPilot DataFlash BIN을 엽니다. FMT/FMTU와 펌웨어
  표기를 확인하며, RATE의 목표/실제 각속도만 비교합니다. ATT는 별도의 각도
  참고값입니다. ARM 또는 EV의 명시적 Armed 구간을 사용합니다. 로그가 Armed
  중간부터 기록됐거나 해당 메시지가 없으면 지표를 계산하지 못할 수 있습니다.
  Armed는 비행 중이라는 증거가 아닙니다. 지상 회전 및 이착륙 구간은 해석에 주의하세요.
- RMS는 유효 구간의 시간 가중 오차입니다. Overshoot, settling, oscillation은
  계단 입력의 진폭, 전후 평탄 구간, 초기 추종 및 관측 시간 기준을 통과한 경우에만
  표시합니다. 정착 시간은 목표 변화의 ±5% 범위입니다. 진동 지표는 그 범위 밖에서
  오차가 부호를 바꾼 횟수의 중앙값입니다. 제어 안정성 판정이나 진단 확률이 아닙니다.
- Vibration은 GYR의 SampleUS 또는 연속 ISBH/ISBD 배치의 샘플링 주기로
  스펙트럼을 계산합니다. 일반 IMU의 기록 빈도를 원시 센서 주기로 간주하지 않습니다.
  VIBE, PID 제한 플래그, FTN1/FTN2 피크, ESC RPM을 참고로 표시합니다.
  배치의 필터 전후 구성을 기체의 로깅 설정과 대조하세요.
- MAVLink는 선택 기체에서 관측된 ATTITUDE_TARGET과 ATTITUDE를 부팅 시간으로
  정렬합니다. 목표 각속도 마스크와 100 ms 시간 차를 적용합니다. 스트림을 강제로
  활성화하지 않으므로 기체가 두 메시지를 보내지 않으면 BIN을 사용하세요.
- 데이터가 충분해도 이 구현은 폐루프 RATE만으로 PID의 방향이나 크기를 단정하지
  않습니다. 검증된 동역학 모델이나 반복 시험에서 나온 후보는 사용자가 입력합니다.
  필터 탭의 주파수와 추적 방식은 검토 단서입니다. 필터 파라미터는 읽기만 지원합니다.

참조: [ArduPilot 로그 스키마](https://ardupilot.org/copter/docs/logmessages.html),
[센서 배치 샘플러](https://github.com/ArduPilot/ardupilot/blob/master/libraries/AP_InertialSensor/BatchSampler.cpp),
[MAVLink 메시지](https://mavlink.io/en/messages/common.html).

## 파라미터 적용과 복원

1. Dashboard에서 기체 식별을 읽고 PID Tuning에서 현재 값을 읽습니다. 검증할 값을
   후보로 등록합니다. 연결된 정확한 ArduCopter 빌드의 파라미터 정의가 필요합니다.
2. Apply & Restore에서 변경 목록과 SYS/COMP/고유 ID를 확인합니다. 세션 쓰기를
   켜고 Apply를 누릅니다. 메타데이터 파일을 선택한 뒤 경고창에서 승인합니다.
3. 앱은 heartbeat, 펌웨어 빌드, 고유 ID, Disarmed, 지상 상태, 현재값을 확인합니다.
   변경 대상 파라미터의 기존값을 고유 ID 별 폴더에 백업한 뒤 각 항목의 응답과
   별도의 readback을 확인합니다. 읽기는 제한적으로 재시도합니다. 쓰기 응답이
   없으면 같은 쓰기를 반복하지 않고 중단합니다. 쓰기가 적용됐지만 응답이 사라졌을
   수 있으므로 재연결 후 해당 값부터 확인하세요.
4. 복원 시 백업에 있는 대상의 현재값을 먼저 읽고 동일 ID와 빌드의 파일을 선택합니다.
   같은 검증 및 승인 절차로 복원하며 복원 직전 값도 다시 백업합니다.

메타데이터 입력 형식은 다음 키로 구성됩니다. 값은 예제 수치를 복사하지 말고
실제 보드의 해당 펌웨어 태그/커밋에 해당하는 파라미터 정의에서 가져오세요.

- `firmware.version`: AUTOPILOT_VERSION의 원본 flight_sw_version 정수.
- `firmware.build`: flight_custom_version 바이트의 hex 문자열.
- `firmware.board`: board_version 정수.
- `firmware.autopilot`, `firmware.vehicle_type`: 대상 heartbeat의 enum 정수.
- `parameters.<name>`: `type: "REAL32"`, `writable: true`, `min`, `max`.

예시 범위를 내장하지 않습니다. 업스트림 메타데이터 자동 검색이나 서명 검증은
구현되어 있지 않으므로 메타데이터 출처를 직접 검증해야 합니다. 데이터 경로는
Qt AppLocalDataLocation 아래 `tuner/<uid>/`입니다. 응답에 UID2 또는 UID가
없으면 쓰기와 기체별 저장을 막습니다. 쓰기는 단일 대상 Serial/Bluetooth/TCP
링크에 한정합니다. 같은 SYS/COMP를 가진 여러 기체를 중계하는 TCP 링크나
인증되지 않은 원격 링크에 이 기능을 사용하지 마세요. MAVLink 파라미터 응답에는
요청 번호가 없고 heartbeat/landed 상태 또한 수신 시점에 의존합니다. 지상 시험 시
추진부를 물리적으로 비활성화하고 다른 GCS의 설정·Arm 작업을 중단하세요.

참조: [MAVLink 파라미터 프로토콜](https://mavlink.io/en/services/parameter.html).

## ArduCopter SITL 검증

1. [ArduPilot SITL](https://ardupilot.org/dev/docs/using-sitl-for-ardupilot-testing.html)
   환경에서 실기체에 쓸 후보의 펌웨어와 맞는 커밋·프레임을 실행합니다.
   예를 들어 ArduPilot 체크아웃에서
   `Tools/autotest/sim_vehicle.py -v ArduCopter -f quad --no-mavproxy`를 실행합니다.
2. UI의 TCP 대상은 SITL 프로세스가 열어 둔 주소를 사용합니다. 기본 인스턴스와
   같은 호스트라면 보통 `127.0.0.1:5760`입니다. 앱에서 기체 식별을 먼저 읽습니다.
   SITL 빌드가 고유 ID를 제공하지 않으면 Linker 쓰기는 차단됩니다. 이때는 SITL
   콘솔에서만 후보를 적용한 뒤 BIN 두 개를 비교하세요.
3. 같은 입력, 프레임, 부하, 시뮬레이터 설정으로 기준과 후보를 각각 실행합니다.
   조종 입력과 시뮬레이션 실행은 운영자가 통제합니다. 두 BIN을 Linker의 History에서
   비교하거나 터미널에서 아래 명령을 실행합니다.

```bash
.venv/bin/python tools/tuner_sitl.py /path/before.BIN /path/after.BIN > sitl-comparison.json
```

PID Tuning에서 생성된 JSON을 불러와 후보의 기존값과 변경값을 대조할 수 있습니다.
비교 도구는 두 파일의 펌웨어 표기, 로그에 기록된 설정 차이, 축별 추종 지표와
유효 계단 입력을 표시합니다. 명령 전송이나 자동 합격 판정은 수행하지 않습니다.
입력과 환경이 일치했는지, 과도응답 개선이 일관적인지 운영자가 검토해야 합니다.
SITL 결과는 실제 기체의 진동과 안정성 검증을 대체할 수 없습니다.
