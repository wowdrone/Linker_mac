# STAGE1 차량·드론 가이드 시뮬레이션

- **Linker**는 차량 위치, 위험 판단, 운전자 배너를 맡는다. `AUTONOMOUS`에서는 로컬 `SimVehicleInterface`로 속도·오프셋을 보내고 `MANNED`에서는 화면·소리로만 표시한다.
- **Sentinel**은 탐지·좌표 투영·경보 송신을 맡는다. 프로필의 `demo: true`는 가짜 좌표 입력과 드론 운동 모델을 활성화한다. `false`는 기체 FC 위치·자세로 목표를 계산해 표시한다. 자동 비행 목표를 실기체에 전송하지 않는다.

## Windows Git Bash 실행

두 폴더 각각에서 기존 Python 환경을 활성화한다. 예제는 `source .venv/Scripts/activate`다.

한 장비의 로컬 데모에서 서명 키를 한 번 생성한다. 네 터미널에서 같은 사용자 홈을 사용한다.

```bash
python -c "from pathlib import Path; import secrets; p=Path.home()/'.wow-guide-key.hex'; p.open('x', encoding='ascii').write(secrets.token_hex(32))"
```

키 파일이 이미 있으면 같은 파일을 계속 사용한다. 별도 위치는 `WOW_GUIDE_SIGNING_KEY_FILE`로 지정한다. 두 링크를 실제 장비 간 연결로 사용할 때는 같은 키를 보안 경로로 배포한다.

| 순서 | 작업 폴더 | 명령 |
| --- | --- | --- |
| 1 | Linker_mac | `python guide_vehicle.py --config config/stage1.json` |
| 2 | sentinel | `python guide_sentinel.py --config config/stage1.json` |
| 3 | Linker_mac | `python tools/sim_car.py --config config/stage1.json --seconds 30` |
| 4 | sentinel | `python tools/fake_detector.py --config config/stage1.json --class DEBRIS --side LEFT` |

화면을 사용하려면 1번 대신 Linker를 실행해 상단 **Guide → 수신 시작**, 2번 대신 Sentinel을 실행해 **드론 경보 → 중계 시작**을 누른다. 같은 포트를 쓰므로 한 역할의 CLI와 화면 수신을 동시에 시작하지 않는다. 3번과 4번을 바로 이어서 실행하거나 `--station`으로 시험 장애물 위치를 더 앞에 놓는다. `--class VEHICLE --side NONE --lateral 0 --size 3`은 정지 시나리오다.

실제 연결 없이 대시보드만 보려면 `dashboard_mac.command`를 더블클릭하거나 `python drive_dashboard.py`를 실행한다. 이때는 시뮬레이션을 표시하며, 같은 주소로 Linker 수신이 시작되면 실데이터로 바뀐다. Linker 화면의 Guide 창 **대시보드** 버튼도 수신 시작 전부터 열린다. 실데이터는 **수신 시작** 후 표시되며, CLI에서 `python guide_vehicle.py --config config/stage1.json --dashboard` 후 `http://127.0.0.1:8765/`를 연다. 주소는 프로필 `linker.dashboard`(기본 `["127.0.0.1", 8765]`)로 바꾸며 루프백만 허용한다. 대시보드는 세션 상태를 읽기만 하고 판단에 관여하지 않는다. RPM·기어·스로틀·브레이크·ERS는 차량 GPS JSON의 선택 필드 `rpm`, `gear`, `throttle_pct`, `brake_pct`, `ers_pct`가 있을 때만 표시한다. `tools/sim_car.py`는 표시용 장난감 구동 모델 값을 함께 보낸다.

로그는 기본값으로 `~/.wow-guide/guide_linker.jsonl`, `~/.wow-guide/guide_sentinel.jsonl`에 남는다. 두 장비에서 실행할 때는 송신 시각 검증 전에 운영체제 UTC를 GPS 또는 동기화된 시계에 맞춘다.

## 검증

Linker 폴더의 Git Bash에서 다음을 실행한다.

```bash
python -m unittest discover -s tests -p 'test_guide_stage1.py' -v
GUIDE_TEST_UDP=1 python -m unittest discover -s tests -p 'test_guide_stage1.py' -v
```

첫 명령은 메모리 소켓을 사용하고, 두 번째는 실제 `127.0.0.1` UDP 포트를 사용한다. 둘 다 같은 MAVLink 서명·직렬화, 이중 링크, 차량·드론 시뮬레이터, 손실·끊김 중계를 사용한다. 10m 정지 시험은 STOP 결정과 정지 여유 부족을 확인한다. 이 속도에서 2m 여유를 확보할 정지 거리는 약 10.34m다.

## 장비 연결 전 확인

- 카메라의 내부 파라미터, 렌즈 보정, 짐벌 각도와 좌표계, FC 시각과 프레임 도착 지연을 확인한다. 시뮬레이터의 지면 평면 결과를 실제 탐지 거리로 사용하지 않는다.
- 코스 파일의 방향, 시작선, 랩 경계, 교차점과 안전 이탈 위치를 확인한다. 카메라 중심선 기하 추정과 별도로 실제 환경에서 차량 기준 최소 탐지 거리를 측정한다.
- 실측 최대 감속도, 횡가속도, 무선 편도 지연, 손실률과 각 경로의 중단을 검증한다. 시뮬레이션의 대략적인 kinematic 모델 결과를 안전 한계로 사용하지 않는다.
- 기존 ArduPilot failsafe를 별도로 점검한다. 실차 제어 인터페이스와 실기체 자동 선행·이탈 연동은 없다.
