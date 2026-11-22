## map_widget.py

import base64
import json
from pathlib import Path
import threading
import requests

from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtWebChannel import QWebChannel
from PyQt5.QtCore import QObject, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QColor

# 로봇랜드 기본 좌표 (main_window.py 와 동일하게 맞춰서 사용)
DEFAULT_LAT = 37.5196976
DEFAULT_LON = 126.6109531


class MapBridge(QObject):
    """JS <-> Python 브리지 (지도 공통)"""

    searchResult = pyqtSignal(float, float, str)
    searchError = pyqtSignal(str)
    mapModeChanged = pyqtSignal(str)
    waypointClicked = pyqtSignal(float, float)
    vehicleClicked = pyqtSignal(str)   # 멀티 기체 마커 클릭 → 기체 key

    @pyqtSlot(str)
    def requestSearch(self, query: str):
        """JS 쪽에서 검색 요청이 들어왔을 때 Nominatim 으로 지오코딩"""
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
    def setMapMode(self, mode: str):
        # map.html 에서 setMapMode(...) 호출할 때 들어오는 모드
        # print(f"[MapBridge] setMapMode: {mode}")
        self.mapModeChanged.emit(mode)

    @pyqtSlot(float, float)
    def addWaypoint(self, lat: float, lon: float):
        self.waypointClicked.emit(lat, lon)

    @pyqtSlot(str)
    def selectVehicle(self, key: str):
        self.vehicleClicked.emit(key)


# ⚠️ {} 때문에 str.format 대신 __LAT__ / __LON__ 만 replace 로 치환
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<title>Linker Map</title>
<style>
html, body {
  margin:0; padding:0; height:100%; overflow:hidden;
}
:root {
  --panel:#202632; --field:#171d27; --border:#353f50;
  --text:#eef3fc; --muted:#a2b1c5; --accent:#63d0ff; --accent-text:#0c2433;
  --radius:7px; color-scheme:dark;
}
body {
  display:flex; flex-direction:column;
  background:var(--field);
}
#toolbar {
  flex:none; display:flex; flex-wrap:wrap; align-items:center;
  gap:4px 8px; padding:5px; background:var(--panel);
  color:var(--muted); font-family:Arial,sans-serif; font-size:12px;
}
.toolbar-group {
  display:flex; flex-wrap:wrap; align-items:center; gap:4px;
  min-width:0; max-width:100%;
}
.toolbar-group label {
  display:flex; align-items:center; gap:3px; white-space:nowrap;
}
.btn {
  background:var(--panel); color:var(--text); border:1px solid var(--border);
  padding:4px 8px; border-radius:var(--radius);
  cursor:pointer; font-weight:bold; white-space:nowrap;
}
.btn.active {
  background:var(--accent); color:var(--accent-text); border-color:var(--accent);
}
#searchBox {
  order:-1; flex:1 1 190px; max-width:260px; flex-wrap:nowrap;
}
#searchInput {
  flex:1; min-width:0; max-width:260px; padding:4px;
  border-radius:var(--radius); border:1px solid var(--border);
  background:var(--field); color:var(--text);
}
#circleRadius,
#circleAlt,
#circleSpeed,
#circleDir {
  box-sizing:border-box; width:56px; padding:2px;
  border-radius:var(--radius); border:1px solid var(--border);
  background:var(--field); color:var(--text);
}
#map {
  position:relative; flex:1; min-height:0; background:var(--field);
}
#map .leaflet-bar a, #map .leaflet-bar a:hover {
  background:var(--panel); color:var(--text); border-color:var(--border);
}
#map .leaflet-control-scale-line {
  background:var(--panel); color:var(--text); border-color:var(--border); text-shadow:none;
}
#basemapSelect {
  background:var(--field);
  color:var(--text);
  border-radius:var(--radius);
  border:1px solid var(--border);
  padding:3px 6px;
}
.wp-label {
  background:rgba(0,0,0,0.6);
  color:#fff;
  border-radius:10px;
  padding:2px 4px;
  font-size:11px;
  font-weight:bold;
}
.drone-icon {
  filter: drop-shadow(0 1px 2px rgba(0,0,0,.8));
}
/* 멀티 기체 마커 (색상은 --c 로 기체별 지정) */
.veh-wrap { background:none; border:none; }
.veh {
  width:34px; height:34px; box-sizing:border-box;
  border-radius:50%; border:3px solid var(--c);
  background:rgba(0,0,0,0.75);
  box-shadow:0 0 6px var(--c);
  position:relative;
}
.veh.active { border-width:4px; box-shadow:0 0 16px 5px var(--c); }
.veh.lost { opacity:0.45; border-style:dashed; }
.veh-arrow { position:absolute; left:0; top:0; width:100%; height:100%; }
.veh-arrow::before {
  content:""; position:absolute; left:50%; top:3px; margin-left:-6px;
  border-left:6px solid transparent; border-right:6px solid transparent;
  border-bottom:13px solid var(--c);
}
</style>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
</head>
<body>
<div id="toolbar">
  <div class="toolbar-group">
    <div id="btn2d" class="btn">2D</div>
    <div id="btnSat" class="btn active">Sat</div>
    <label>Base
      <select id="basemapSelect">
        <option value="esri" selected>Esri Sat</option>
        <option value="osm">OSM 2D</option>
        <option value="google">Google Sat</option>
        <option value="naver">Naver</option>
        <option value="bing">Bing</option>
        <option value="custom">Custom</option>
      </select>
    </label>
  </div>
  <div class="toolbar-group">
    <div id="btnWP" class="btn">WP MODE</div>
    <div id="btnGF" class="btn">Geo Fence</div>
    <div id="btnFinishGF" class="btn">Finish GF</div>
  </div>
  <div class="toolbar-group">
    <label>Circle (m) <input id="circleRadius" type="number" value="50" /></label>
    <label>Alt <input id="circleAlt" type="number" value="60" /></label>
    <label>Spd <input id="circleSpeed" type="number" value="8" /></label>
    <label>Dir
      <select id="circleDir">
        <option value="CW">CW</option>
        <option value="CCW">CCW</option>
      </select>
    </label>
  </div>
  <div class="toolbar-group">
    <div id="btnCircle" class="btn">Circle</div>
    <div id="btnClear" class="btn">Clear</div>
    <div id="btnFinal" class="btn">Final</div>
  </div>
  <div id="searchBox" class="toolbar-group">
    <input id="searchInput" type="text" aria-label="Search address" value="인천 서구 로봇랜드로 155-11" />
    <button id="btnSearch" class="btn">Go</button>
  </div>
</div>
<div id="map"></div>
<script>
let map;

let osmLayer;
let esriSatLayer;
let googleSatLayer;
let naverLayer;
let bingLayer;
let customLayer;
let currentBaseLayer = null;
let currentBase = "esri";

let searchMarker = null;
let pyBridge = null;
let currentMapMode = "NONE";

let wpMarkers = [];
let routePoints = [[__LAT__, __LON__]];
let wpPolyline = null;

// Circle 프리뷰용
let circlePolyline = null;
let circlePointMarkers = [];

// Geo Fence
let gfPoints = [];
let gfLayer = null;
let gfMarkers = [];

let home = null;
let droneMarker = null;

function init() {
  map = L.map('map', {zoomControl:false}).setView([__LAT__, __LON__], 17);
  L.control.zoom({position:'bottomright'}).addTo(map);
  L.control.scale({imperial:false}).addTo(map);

  osmLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    maxZoom: 19,
    attribution: '&copy; OpenStreetMap contributors'
  });

  esriSatLayer = L.tileLayer(
    'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{x}/{y}',
    {
      maxZoom: 19,
      attribution: 'Tiles &copy; Esri'
    }
  ).addTo(map);
  // Google 을 기본으로
  googleSatLayer = L.tileLayer(
      'https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',
      { maxZoom: 19, attribution: 'Google' }
  ).addTo(map);
  currentBaseLayer = googleSatLayer;
  currentBase = "google";
  

  googleSatLayer = L.tileLayer(
    'https://mt1.google.com/vt/lyrs=s&x={x}&y={y}&z={z}',
    {
      maxZoom: 19,
      attribution: 'Google'
    }
  );

  naverLayer = L.tileLayer(
    'https://YOUR_NAVER_TILE_URL/{z}/{x}/{y}.png',
    {
      maxZoom: 19,
      attribution: 'Naver'
    }
  );
  bingLayer = L.tileLayer(
    'https://YOUR_BING_TILE_URL/{z}/{x}/{y}.png',
    {
      maxZoom: 19,
      attribution: 'Bing'
    }
  );
  customLayer = L.tileLayer(
    'https://your.custom.server/tiles/{z}/{x}/{y}.png',
    {
      maxZoom: 19,
      attribution: 'Custom'
    }
  );

  home = L.marker([__LAT__, __LON__]).addTo(map);
  home.bindTooltip("H", {permanent:true, direction:"center", className:"wp-label"});
  home.bindPopup("로봇랜드로 155-11 (Home)");

  pythonUpdateDrone(__LAT__ + 0.00025, __LON__);

  document.getElementById('btn2d').onclick = () => setBaseLayer('osm');
  document.getElementById('btnSat').onclick = () => setBaseLayer('esri');

  const baseSel = document.getElementById('basemapSelect');
  if (baseSel) {
    baseSel.addEventListener('change', (e) => {
      setBaseLayer(e.target.value);
    });
  }

  document.getElementById('btnSearch').onclick = () => sendSearch();
  document.getElementById('searchInput').addEventListener('keypress', (e) => {
    if (e.key === 'Enter') sendSearch();
  });

  document.getElementById('btnWP').onclick = () => setMapMode('WP');
  document.getElementById('btnGF').onclick = () => setMapMode('GF');
  document.getElementById('btnFinishGF').onclick = () => setMapMode('FINISH_GF');
  document.getElementById('btnCircle').onclick = () => setMapMode('CIRCLE');
  document.getElementById('btnClear').onclick = () => setMapMode('CLEAR');
  document.getElementById('btnFinal').onclick = () => setMapMode('FINAL');

  map.on('click', function(e) {
    const lat = e.latlng.lat;
    const lon = e.latlng.lng;

    if (currentMapMode === 'WP') {
      const index = routePoints.length;
      const m = L.marker([lat, lon]).addTo(map);
      m.bindTooltip(String(index), {permanent:true, direction:"center", className:"wp-label"});
      wpMarkers.push(m);

      routePoints.push([lat, lon]);
      if (wpPolyline) map.removeLayer(wpPolyline);
      wpPolyline = L.polyline(routePoints, {color:'lime', weight:2}).addTo(map);

      if (pyBridge && pyBridge.addWaypoint) {
        pyBridge.addWaypoint(lat, lon);
      }

    } else if (currentMapMode === 'CIRCLE') {
      // ----- Circle 모드: 포인트+폴리라인으로 경로 표시 -----
      const rInput = document.getElementById('circleRadius').value;
      const aInput = document.getElementById('circleAlt').value;
      const sInput = document.getElementById('circleSpeed').value;
      const dirSel = document.getElementById('circleDir').value || 'CW';

      let r = parseFloat(rInput);
      if (!isFinite(r) || r <= 0) r = 50;
      let alt = parseFloat(aInput);
      if (!isFinite(alt)) alt = 60;
      let spd = parseFloat(sInput);
      if (!isFinite(spd)) spd = 8;

      // 이전 경로/포인트 삭제
      if (circlePolyline) {
        map.removeLayer(circlePolyline);
        circlePolyline = null;
      }
      circlePointMarkers.forEach(m => map.removeLayer(m));
      circlePointMarkers = [];

      const pts = createCirclePoints({lat:lat, lon:lon}, r, 48, dirSel);

      circlePolyline = L.polyline(pts, {
        color:'yellow',
        weight:2
      }).addTo(map);

      pts.forEach((p, idx) => {
        // 너무 많지 않게 간격을 두고 포인트 표시
        if (idx % 4 !== 0) return;
        const m = L.circleMarker(p, {
          radius:3,
          color:'#ffffff',
          fillColor:'#ff8800',
          fillOpacity:0.9
        }).addTo(map);
        const label = `Alt:${alt} Spd:${spd} Dir:${dirSel}`;
        m.bindTooltip(label, {
          permanent:false,
          direction:"top",
          className:"wp-label"
        });
        circlePointMarkers.push(m);
      });

    } else if (currentMapMode === 'GF') {
      gfPoints.push([lat, lon]);
      const m = L.circleMarker([lat, lon], {
        radius:4,
        color:'orange',
        weight:2,
        fill:true,
        fillOpacity:0.9
      }).addTo(map);
      gfMarkers.push(m);
      if (gfLayer) map.removeLayer(gfLayer);
      gfLayer = L.polyline(gfPoints, {color:'orange', weight:2, dashArray:'4,4'}).addTo(map);
    }
  });
}

// 중심/반지름/방향으로 원 궤적 포인트 계산
function createCirclePoints(center, radiusM, segments, dir) {
  const lat0 = center.lat * Math.PI / 180;
  const lon0 = center.lon * Math.PI / 180;
  const R = 6378137.0;
  const d = radiusM / R;

  const pts = [];
  const sign = (dir === 'CCW') ? 1 : -1;

  for (let i = 0; i <= segments; i++) {
    const theta = sign * 2 * Math.PI * i / segments;

    const lat = Math.asin(
      Math.sin(lat0) * Math.cos(d) +
      Math.cos(lat0) * Math.sin(d) * Math.cos(theta)
    );
    const lon = lon0 + Math.atan2(
      Math.sin(theta) * Math.sin(d) * Math.cos(lat0),
      Math.cos(d) - Math.sin(lat0) * Math.sin(lat)
    );

    pts.push([lat * 180 / Math.PI, lon * 180 / Math.PI]);
  }
  return pts;
}

function setBaseLayer(name) {
  if (currentBaseLayer) {
    map.removeLayer(currentBaseLayer);
  }

  if (name === 'osm') {
    currentBaseLayer = osmLayer.addTo(map);
  } else if (name === 'esri') {
    currentBaseLayer = esriSatLayer.addTo(map);
  } else if (name === 'google') {
    currentBaseLayer = googleSatLayer.addTo(map);
  } else if (name === 'naver') {
    currentBaseLayer = naverLayer.addTo(map);
  } else if (name === 'bing') {
    currentBaseLayer = bingLayer.addTo(map);
  } else if (name === 'custom') {
    currentBaseLayer = customLayer.addTo(map);
  }

  currentBase = name;
  updateBaseButtons();

  const baseSel = document.getElementById('basemapSelect');
  if (baseSel && baseSel.value !== name) {
    baseSel.value = name;
  }
}

function updateBaseButtons() {
  const btn2d = document.getElementById('btn2d');
  const btnSat = document.getElementById('btnSat');
  if (!btn2d || !btnSat) return;

  btn2d.classList.remove('active');
  btnSat.classList.remove('active');

  if (currentBase === 'osm') {
    btn2d.classList.add('active');
  } else {
    btnSat.classList.add('active');
  }
}

function highlightModeButtons() {
  const ids = ['btnWP','btnGF','btnFinishGF','btnCircle','btnClear','btnFinal'];
  for (let id of ids) {
    const el = document.getElementById(id);
    if (el) el.classList.remove('active');
  }
  if (currentMapMode === 'WP') document.getElementById('btnWP').classList.add('active');
  else if (currentMapMode === 'GF') document.getElementById('btnGF').classList.add('active');
  else if (currentMapMode === 'FINISH_GF') document.getElementById('btnFinishGF').classList.add('active');
  else if (currentMapMode === 'CIRCLE') document.getElementById('btnCircle').classList.add('active');
  else if (currentMapMode === 'CLEAR') document.getElementById('btnClear').classList.add('active');
  else if (currentMapMode === 'FINAL') document.getElementById('btnFinal').classList.add('active');
}

function setMapMode(mode) {
  currentMapMode = mode;
  highlightModeButtons();

  if (mode === 'CLEAR') {
    // WP 지우기
    for (let m of wpMarkers) map.removeLayer(m);
    wpMarkers = [];
    routePoints = [[__LAT__, __LON__]];
    if (wpPolyline) map.removeLayer(wpPolyline);
    wpPolyline = null;

    // Circle 프리뷰 지우기
    if (circlePolyline) {
      map.removeLayer(circlePolyline);
      circlePolyline = null;
    }
    circlePointMarkers.forEach(m => map.removeLayer(m));
    circlePointMarkers = [];

    // GF 지우기
    for (let m of gfMarkers) map.removeLayer(m);
    gfMarkers = [];
    gfPoints = [];
    if (gfLayer) {
      map.removeLayer(gfLayer);
      gfLayer = null;
    }
  }

  if (mode === 'FINISH_GF') {
    if (gfPoints.length >= 3) {
      if (gfLayer) map.removeLayer(gfLayer);
      gfLayer = L.polygon(gfPoints, {color:'red', weight:2, fill:false}).addTo(map);
    }
  }

  if (pyBridge && pyBridge.setMapMode) pyBridge.setMapMode(mode);
}

function sendSearch() {
  const q = document.getElementById('searchInput').value.trim();
  if (!pyBridge) {
    alert('Python bridge not ready.');
    return;
  }
  pyBridge.requestSearch(q);
}

function pythonSearchResult(payload) {
  if (!payload) return;
  const lat = payload.lat;
  const lon = payload.lon;
  const name = payload.name || '';
  const ll = [lat, lon];
  map.setView(ll, 17);
  if (searchMarker) map.removeLayer(searchMarker);
  searchMarker = L.marker(ll).addTo(map);
  searchMarker.bindPopup(name).openPopup();
}

function pythonSearchError(msg) {
  alert(msg || '검색 오류');
}

function pythonUpdateHome(lat, lon) {
  const ll = [lat, lon];
  map.setView(ll, map.getZoom());
  if (home) {
    home.setLatLng(ll);
  } else {
    home = L.marker(ll).addTo(map);
    home.bindTooltip("H", {permanent:true, direction:"center", className:"wp-label"});
  }
  routePoints[0] = ll;
}

function pythonUpdateDrone(lat, lon) {
  const ll = [lat, lon];
  if (droneMarker) {
    droneMarker.setLatLng(ll);
  } else {
    const quadIcon = L.icon({
      iconUrl: 'data:image/png;base64,__QUAD_ICON__',
      className: 'drone-icon',
      iconSize: [42, 42],
      iconAnchor: [21, 21]
    });
    droneMarker = L.marker(ll, {icon: quadIcon, alt: '드론'}).addTo(map);
  }
}

// ---------------- 멀티 기체 ----------------
let vehicleMarkers = {};
let vehicleTracks = {};

function pythonUpdateVehicle(id, lat, lon, heading, color, label, active, lost) {
  if (!map) return;
  const ll = [lat, lon];
  let m = vehicleMarkers[id];
  if (!m) {
    const icon = L.divIcon({
      className: 'veh-wrap',
      html: '<div class="veh" style="--c:' + color + '"><div class="veh-arrow"></div></div>',
      iconSize: [34, 34],
      iconAnchor: [17, 17]
    });
    m = L.marker(ll, {icon: icon}).addTo(map);
    m.bindTooltip(label, {permanent:true, direction:"bottom", offset:[0, 16], className:"wp-label"});
    m.on('click', function () {
      if (pyBridge && pyBridge.selectVehicle) pyBridge.selectVehicle(String(id));
    });
    vehicleMarkers[id] = m;
  } else {
    m.setLatLng(ll);
    m.setTooltipContent(label);
  }
  m.setZIndexOffset(active ? 1000 : 0);
  const el = m.getElement();
  if (el) {
    const body = el.querySelector('.veh');
    const arrow = el.querySelector('.veh-arrow');
    if (arrow) arrow.style.transform = 'rotate(' + heading + 'deg)';
    if (body) {
      body.classList.toggle('active', !!active);
      body.classList.toggle('lost', !!lost);
    }
  }

  let t = vehicleTracks[id];
  if (!t) {
    t = L.polyline([], {color: color, weight: 2, opacity: 0.7}).addTo(map);
    vehicleTracks[id] = t;
  }
  const pts = t.getLatLngs();
  const last = pts.length ? pts[pts.length - 1] : null;
  if (!last || map.distance(last, ll) > 1.0) {
    t.addLatLng(ll);
    if (pts.length > 3000) { pts.shift(); t.setLatLngs(pts); }
  }
}

function pythonRemoveVehicle(id) {
  if (vehicleMarkers[id]) { map.removeLayer(vehicleMarkers[id]); delete vehicleMarkers[id]; }
  if (vehicleTracks[id]) { map.removeLayer(vehicleTracks[id]); delete vehicleTracks[id]; }
}

function pythonFocusVehicle(id) {
  const m = vehicleMarkers[id];
  if (m && map) map.panTo(m.getLatLng());
}

function pythonSetWaypoints(points) {
  for (let m of wpMarkers) map.removeLayer(m);
  wpMarkers = [];

  routePoints = [];
  if (home) {
    const h = home.getLatLng();
    routePoints.push([h.lat, h.lng]);
  } else {
    routePoints.push([__LAT__, __LON__]);
  }

  if (!Array.isArray(points)) {
    points = [];
  }

  let idx = 1;
  for (const p of points) {
    if (!Array.isArray(p) || p.length < 2) continue;
    const lat = p[0];
    const lon = p[1];
    const ll = [lat, lon];

    const m = L.marker(ll).addTo(map);
    m.bindTooltip(String(idx), {permanent:true, direction:"center", className:"wp-label"});
    wpMarkers.push(m);
    routePoints.push(ll);
    idx += 1;
  }

  if (wpPolyline) map.removeLayer(wpPolyline);
  if (routePoints.length > 1) {
    wpPolyline = L.polyline(routePoints, {color:'lime', weight:2}).addTo(map);
  } else {
    wpPolyline = null;
  }
}

new QWebChannel(qt.webChannelTransport, function(channel) {
  pyBridge = channel.objects.pyBridge;
});

window.onload = init;
</script>
</body>
</html>
"""


def create_map_widget(default_lat: float = DEFAULT_LAT,
                      default_lon: float = DEFAULT_LON):
    """
    Leaflet 지도를 담은 QWebEngineView 와 MapBridge 를 생성해서 반환.
    초기 좌표와 앱의 드론 아이콘을 HTML에 넣는다.
    """
    quad_icon = base64.b64encode(Path(__file__).with_name("quad.png").read_bytes()).decode("ascii")
    html = (HTML_TEMPLATE
            .replace("__LAT__", str(default_lat))
            .replace("__LON__", str(default_lon))
            .replace("__QUAD_ICON__", quad_icon))

    view = QWebEngineView()
    view.loadFinished.connect(lambda ok: _apply_map_colors(view) if ok else None)
    view.setHtml(html)

    bridge = MapBridge()
    channel = QWebChannel(view.page())
    channel.registerObject("pyBridge", bridge)
    view.page().setWebChannel(channel)

    return view, bridge


def set_map_theme(view, theme):
    colors = {
        "panel": theme.panel, "field": theme.field, "border": theme.border,
        "text": theme.text, "muted": theme.muted, "accent": theme.accent,
        "accent-text": theme.accent_text, "radius": f"{theme.button_radius}px",
    }
    view.setProperty("mapTheme", {f"--{key}": value for key, value in colors.items()})
    view.setProperty("mapColorScheme", "light" if theme.light else "dark")
    view.page().setBackgroundColor(QColor(theme.field))
    _apply_map_colors(view)


def _apply_map_colors(view):
    colors = view.property("mapTheme")
    if colors:
        colors["color-scheme"] = view.property("mapColorScheme")
        view.page().runJavaScript(
            "for (const [name, value] of Object.entries(" + json.dumps(colors)
            + ")) document.documentElement.style.setProperty(name, value);"
        )
