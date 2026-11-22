# Linker GCS — macOS 버전

멀티 기체 동시 연결 + 4채널 영상 수신 GCS (Apple Silicon / Intel 공용)

## 1. 설치 (최초 1회)

1. Python 3.11 설치: https://www.python.org/downloads/macos/
2. 이 폴더(`Linker_mac`)를 맥으로 복사
3. 터미널을 열고:
   ```bash
   cd ~/Desktop/Linker_mac        # 복사한 위치
   bash setup_mac.command
   ```
   `.venv` 가상환경을 만들고 PyQt5 / PyQtWebEngine / pymavlink / OpenCV 를 설치합니다.
   마지막에 `설치 확인 OK` 가 나오면 성공입니다.

> PyQtWebEngine 설치가 실패하면 (구형 macOS 등) conda 를 쓰세요:
> ```bash
> conda create -n linker -c conda-forge python=3.11 pyqt pyqtwebengine pymavlink pyserial opencv
> conda activate linker && python main_window.py
> ```

## 2. 실행

```bash
./run_mac.command
```
Finder 에서 `run_mac.command` 더블클릭도 됩니다.
(처음 더블클릭 시 "확인되지 않은 개발자" 경고 → 우클릭 → 열기)

## 3. 드론 없이 시험

터미널 창 2개:
```bash
./run_sim.command          # 창 1: 가상 기체 4대 (UDP 14550~14580)
./run_mac.command          # 창 2: GCS
```
GCS 에서 **Fleet → UDP ×4** 클릭 → SYS 1~4 표시.
처음 실행 시 "들어오는 네트워크 연결 허용?" 창이 뜨면 **허용**.

## 4. 실제 드론 연결

| 방식 | 설정 |
|---|---|
| USB / 텔레메트리 모뎀 | **Serial**, 포트 `/dev/cu.usbmodem…` 또는 `/dev/cu.usbserial…`, 보레이트 115200 / 57600 |
| Bluetooth Classic SPP | macOS Bluetooth 설정에서 먼저 페어링한 뒤 **Links** 또는 **Fleet**에서 **Bluetooth**, 장치 포트, 모뎀 보레이트를 선택하고 **Add Link** |
| Wi-Fi / 컴패니언 PC | **UDP**, `0.0.0.0:14550` |
| TCP (SITL 등) | **TCP**, `IP:포트` |

- 맥에서는 `COM5` 대신 `/dev/cu.*` 이름을 씁니다. 페어링 후 목록에 안 보이면 ⟳ 버튼으로 갱신하세요. BLE 전용 장치는 SPP 시리얼 포트가 표시되지 않습니다.
- CP210x / CH340 모뎀은 해당 칩 드라이버가 필요할 수 있습니다 (최신 macOS 는 대부분 내장).
- 기체마다 `SYSID_THISMAV` 를 다르게 설정하세요.
- 다중 선택은 `Ctrl` 대신 **⌘(Command)** + 클릭.

## 5. 영상 4채널

Fly 화면의 **실시간 영상**에서 소스 종류를 선택하고 주소, 포트, 카메라 번호 또는 파일 경로를 입력한 뒤 **열기**를 누르세요. **GStreamer**를 사용하려면 실행 중인 Linker와 같은 아키텍처의 [GStreamer 런타임](https://gstreamer.freedesktop.org/download/)이 필요합니다. 파이프라인은 디코딩된 영상까지 입력하면 됩니다. 연결을 확인할 때는 `videotestsrc is-live=true ! video/x-raw`를 사용할 수 있습니다.

Video 4CH 화면에서 각 채널의 소스 목록을 선택하거나 주소를 직접 입력한 뒤 ▶를 누릅니다. SIYI 프리셋은 기본 주소이므로 카메라의 설정이 다르면 주소를 수정하세요. 카메라 장치 번호는 macOS가 정하며 연결 상태에 따라 달라질 수 있습니다.

| 소스 | 예 |
|---|---|
| RTSP | `rtsp://192.168.0.10:8554/live` |
| UDP / RTP | `udp://@:5600`, `rtp-h264://5600`, `rtp-h265://5600` |
| USB 캡처 / 웹캠 | `0`, `1` (터미널/앱에 **카메라 권한** 필요: 시스템 설정 → 개인정보 보호 및 보안 → 카메라) |
| 파일 | `~/Movies/test.mp4` 는 안 되고 전체 경로 `/Users/이름/Movies/test.mp4` |

스냅샷 / 녹화 저장 위치: `~/Movies/LinkerGCS`

## 6. .app 만들기 (선택)

```bash
./build_mac.command
```
빌드가 끝나면 Finder에서 `dist/LINKER.app`을 더블클릭해 실행합니다.

## Windows 버전과 다른 점

| 항목 | Windows | macOS |
|---|---|---|
| 폰트 | 맑은 고딕 / Consolas | Apple SD Gothic Neo / Menlo |
| 시리얼 포트 | COMx | /dev/cu.* (Bluetooth 모드에는 페어링된 SPP 포트 표시) |
| 녹화 폴더 | ~/Videos/LinkerGCS | ~/Movies/LinkerGCS |
| 빌드 | Linker.spec → exe | Linker_mac.spec → LINKER.app |

맥 전용 처리는 `platform_mac.py` 에 모여 있습니다.

## 비행 전 Safety 점검

상단 **Safety** 탭에서 기체를 선택하고 **새로 읽기**를 누릅니다. 전체 파라미터 목록 수신이 끝난 뒤 점검 결과가 표시됩니다. 기준값은 현장에 맞게 JSON 열에 `{"min": 10, "max": 100}`, `{"equal": 1}`, `{"allowed": [1, 2]}`처럼 입력하고 **기준 적용**을 누르세요. 기본 제공 프로파일의 기준값은 미설정이므로 현장 기준을 직접 정해야 합니다. 프로파일 저장/열기/복제와 CSV 내보내기를 사용할 수 있습니다. **모의 데이터**는 기체 없이 표시 기능을 살펴보기 위한 값이며 기체에 쓰지 않습니다.

수정 모드를 켠 후 변경할 값 열에 숫자를 입력하고 **변경 검토 및 적용**에서 확인해야 전송됩니다. 현재 쓰기는 REAL32 형식만 지원합니다. PX4 정수형 파라미터는 인코딩 협상이 구현되지 않아 판정 및 쓰기를 지원하지 않습니다. 송신 뒤 재읽기에 실패한 경우 해당 값을 기체에서 다시 확인해야 합니다. 점검 결과는 앱 데이터 폴더의 `safety` 디렉터리에 JSON으로 저장됩니다.

ArduPilot SITL을 별도로 실행해 Linker에서 SITL의 MAVLink TCP 또는 UDP 포트에 연결한 뒤 Safety에서 새로 읽기를 누릅니다. 안전 파라미터 쓰기 시험은 실제 기체가 아닌 SITL에서 수행하세요. 단위 검사는 `./.venv/bin/python -m unittest tests.test_safety_validation tests.test_safety_parameters`입니다.

`config/stage1.json`의 Linker 수신 포트 14611과 14615는 A1 Gear의 기본 수신 포트와 같습니다. 같은 주소에서 동시에 사용하면 포트 충돌이 날 수 있으므로 한쪽 포트를 변경하세요.
