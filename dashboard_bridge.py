"""Qt-thread bridge between the browser dashboard and existing Linker connections."""
import time
from PyQt5.QtCore import QByteArray, QBuffer, QIODevice
from pymavlink import mavutil


class DashboardBridge:
    def __init__(self, window):
        self.window = window
        self.video_source = "fly"
        self.result = "연결된 기체를 선택하세요."

    def execute(self, command):
        action = command.get("action")
        fleet = self.window.fleet
        if action == "connect":
            kind = command.get("kind")
            address = str(command.get("address", "")).strip()
            baud = int(command.get("baud", 115200))
            if kind not in ("udp", "udpout", "tcp", "serial", "bluetooth") or not address or len(address) > 256:
                raise ValueError("연결 종류와 주소를 확인하세요.")
            if not 1200 <= baud <= 4000000:
                raise ValueError("전송 속도를 확인하세요.")
            fleet.add_link(kind, address, baud)
            self.result = "링크 연결 요청을 보냈습니다. 연결 상태를 확인하세요."
        elif action == "select":
            vehicle = fleet.vehicle_by_str(command.get("vehicle"))
            if vehicle is None:
                raise ValueError("선택 기체를 찾을 수 없습니다.")
            self.window.set_active_vehicle(vehicle)
            self.result = f"{vehicle.name} 선택"
        elif action in ("streams", "request"):
            vehicle = fleet.vehicle_by_str(command.get("vehicle"))
            if vehicle is None or not vehicle.connected:
                raise ValueError("연결된 기체를 선택하세요.")
            if action == "streams":
                vehicle.request_health_streams()
                self.result = f"{vehicle.name} 텔레메트리 스트림 요청 송신"
            else:
                message_id = int(command.get("message_id", -1))
                if not 0 <= message_id <= 16777215:
                    raise ValueError("MAVLink 메시지 ID를 확인하세요.")
                vehicle.master.mav.command_long_send(
                    vehicle.sysid, vehicle.compid, mavutil.mavlink.MAV_CMD_REQUEST_MESSAGE,
                    0, message_id, 0, 0, 0, 0, 0, 0)
                self.result = f"{vehicle.name} 메시지 {message_id} 요청 송신 · 수신 상태를 확인하세요."
        elif action == "video":
            source = command.get("source")
            if source not in ("fly", "ch1", "ch2", "ch3", "ch4", "off"):
                raise ValueError("영상 채널을 확인하세요.")
            self.video_source = source
            self.result = "영상 공유 소스를 변경했습니다."
        else:
            raise ValueError("지원하지 않는 요청입니다.")

    def snapshot(self):
        vehicles = []
        for vehicle in self.window.fleet.vehicle_list():
            vehicles.append(dict(key=vehicle.key_str, name=vehicle.name, **vehicle.summary()))
        return dict(
            vehicles=vehicles,
            active=self.window.linker.vehicle.key_str if self.window.linker.vehicle else None,
            links=[dict(id=link.link_id, name=link.human,
                        state=self.window.fleet.link_states.get(link.link_id, "closed"),
                        rx=link.rx_count) for link in self.window.fleet.links.values()],
            result=self.result, video_source=self.video_source)

    def frame(self):
        if self.video_source == "off":
            return None
        if self.video_source == "fly":
            canvas = self.window.video_view
        else:
            canvas = self.window.page_video.tiles[int(self.video_source[-1]) - 1].canvas
        image = canvas.image
        if image is None or image.isNull() or time.monotonic() - canvas.frame_at > 2:
            return None
        self.frame_at = canvas.frame_at
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.WriteOnly)
        image.scaledToWidth(min(960, image.width())).save(buffer, "JPEG", 75)
        return bytes(data)
