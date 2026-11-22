# map_common.py
# 지도 관련 공통 정의(기본 좌표, MapBridge 등)를 모아둔 모듈

import threading
import requests

from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot

# 로봇랜드 기본 좌표 (공통 사용)
DEFAULT_LAT = 37.5196976
DEFAULT_LON = 126.6109531


class MapBridge(QObject):
    """JS <-> Python 브리지"""

    searchResult = pyqtSignal(float, float, str)
    searchError = pyqtSignal(str)
    mapModeChanged = pyqtSignal(str)
    waypointClicked = pyqtSignal(float, float)

    @pyqtSlot(str)
    def requestSearch(self, query):
        query = query.strip()
        if not query:
            self.searchError.emit("검색어가 비어 있습니다.")
            return

        def worker():
            try:
                url = "https://nominatim.openstreetmap.org/search"
                params = {"q": query, "format": "json", "limit": 1}
                headers = {"User-Agent": "LINKER-GCS/1.0 (wowdrone)"}
                r = requests.get(url, params=params, headers=headers, timeout=10)
                r.raise_for_status()
                data = r.json()
                if not data:
                    self.searchError.emit("검색 결과가 없습니다.")
                    return
                item = data[0]
                lat = float(item["lat"])
                lon = float(item["lon"])
                name = item.get("display_name", query)
                self.searchResult.emit(lat, lon, name)
            except Exception as e:
                self.searchError.emit(f"검색 오류: {e}")

        threading.Thread(target=worker, daemon=True).start()

    @pyqtSlot(str)
    def setMapMode(self, mode):
        self.mapModeChanged.emit(mode)

    @pyqtSlot(float, float)
    def addWaypoint(self, lat, lon):
        self.waypointClicked.emit(lat, lon)
