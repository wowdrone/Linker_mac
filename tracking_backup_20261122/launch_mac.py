"""Run Linker with its own macOS application identity."""
from hashlib import sha256
from pathlib import Path
import os
import plistlib
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory


def linker_executable(python_app, environment_dir):
    icon = Path(__file__).with_name("LINKER.icns")
    icon_data = icon.read_bytes()
    info = plistlib.dumps({
        "CFBundleName": "LINKER",
        "CFBundleDisplayName": "LINKER",
        "CFBundleVersion": "2",
        "CFBundleExecutable": "LINKER",
        "CFBundleIdentifier": "com.wowdrone.linkergcs",
        "CFBundlePackageType": "APPL",
        "CFBundleIconFile": icon.name,
        "NSHighResolutionCapable": True,
        "NSCameraUsageDescription": "USB 캡처 장치 영상을 수신합니다.",
        "NSLocalNetworkUsageDescription": "MAVLink(UDP/TCP) 및 영상 스트림을 수신합니다.",
    })
    # Rebuild when the interpreter or bundle metadata changes.
    version = sha256(python_app.read_bytes() + info + icon_data).hexdigest()[:16]
    cache = environment_dir / "linker-launcher" / version
    bundle = cache / "LINKER.app"
    executable_path = Path("Contents/MacOS/LINKER")
    executable = bundle / executable_path
    if executable.is_file():
        return executable

    cache.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=cache) as directory:
        staging = Path(directory) / bundle.name
        staged_executable = staging / executable_path
        staged_executable.parent.mkdir(parents=True)
        shutil.copyfile(python_app, staged_executable)
        staged_executable.chmod(0o755)
        (staging / "Contents" / "Info.plist").write_bytes(info)
        resources = staging / "Contents" / "Resources"
        resources.mkdir()
        (resources / icon.name).write_bytes(icon_data)
        subprocess.run(
            ["/usr/bin/codesign", "--force", "--sign", "-", str(staging)],
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            staging.rename(bundle)
        except OSError:
            # Another launch may have published the same signed bundle.
            if not executable.is_file():
                raise
    return executable


def main():
    entrypoint = Path(__file__).resolve().with_name("main_window.py")
    python_app = Path(sys.base_prefix) / "Resources/Python.app/Contents/MacOS/Python"
    executable = sys.executable
    env = os.environ.copy()
    if sys.platform == "darwin" and python_app.is_file():
        executable = str(linker_executable(python_app, Path(sys.prefix)))
        # CPython uses this path to keep loading the project's existing virtual environment.
        env["__PYVENV_LAUNCHER__"] = sys.executable
    os.execve(executable, [executable, str(entrypoint), *sys.argv[1:]], env)


if __name__ == "__main__":
    main()
