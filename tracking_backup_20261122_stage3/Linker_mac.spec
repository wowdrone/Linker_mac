# -*- mode: python ; coding: utf-8 -*-
# macOS .app 빌드:  pyinstaller --noconfirm --clean Linker_mac.spec

import os
import pymavlink

MAVLINK_DEFS = os.path.join(os.path.dirname(pymavlink.__file__), 'message_definitions')

a = Analysis(
    ['main_window.py'],
    pathex=[],
    binaries=[],
    datas=[
        (MAVLINK_DEFS, 'message_definitions'),
        ('logo1.png', '.'),
        ('quad.png', '.'),
        ('multicopter_home.png', '.'),
        ('map.html', '.'),
        ('drive_dashboard.html', '.'),
        ('tracking_dashboard.html', '.'),
        ('LINKER.icns', '.'),
        ('config/stage1.json', 'config'),
        ('config/safety', 'config/safety'),
        ('protocol/demo_course.geojson', 'protocol'),
    ],
    hiddenimports=[
        'serial', 'serial.tools.list_ports', 'PyQt5.QtWebEngineWidgets', 'PyQt5.QtWebChannel',
        'tracking_models', 'tracking_core', 'tracking_worker', 'tracking_session', 'tracking_panel', 'flight_mode_controller', 'dashboard_server', 'tracking_dashboard', 'tracking_service',
        'cv2', 'fleet', 'fleet_panel', 'video_grid', 'platform_mac', 'drive_dashboard',
        'pymavlink.dialects.v10.ardupilotmega', 'pymavlink.dialects.v20.ardupilotmega',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='LINKER',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='LINKER',
)
app = BUNDLE(
    coll,
    name='LINKER.app',
    icon='LINKER.icns',
    bundle_identifier='com.wowdrone.linkergcs',
    info_plist={
        'CFBundleName': 'LINKER',
        'CFBundleDisplayName': 'LINKER',
        'CFBundleVersion': '7',
        'CFBundleShortVersionString': '0.1.0',
        'NSHighResolutionCapable': True,
        'NSCameraUsageDescription': 'USB 캡처 장치 영상을 수신합니다.',
        'NSLocalNetworkUsageDescription': 'MAVLink(UDP/TCP) 및 영상 스트림을 수신합니다.',
    },
)
