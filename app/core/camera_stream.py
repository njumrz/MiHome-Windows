# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""本机 go2rtc 服务与摄像机直播地址。"""

import json
import os
import socket
import subprocess
import time
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from urllib.parse import quote, urlencode

_HOST = "127.0.0.1"
_API_PORT = 1984
_RTSP_PORT = 8554
_CAMERA_TOKENS = (".camera.", ".cateye.", ".feeder.")
_DUAL_CAMERA_MODELS = {"xiaomi.camera.082ac1", "isa.camera.cw501d"}


def is_camera(model: str) -> bool:
    model = (model or "").lower()
    return any(token in model for token in _CAMERA_TOKENS)


def has_second_lens(model: str) -> bool:
    return (model or "").lower() in _DUAL_CAMERA_MODELS


def stream_name(did: str, lens: int) -> str:
    return f"{did}-2" if lens == 2 else did


def live_url(stream_name: str) -> str:
    return f"http://{_HOST}:{_API_PORT}/stream.html?{urlencode({'src': stream_name})}"


def rtsp_url(stream_name: str) -> str:
    return f"rtsp://{_HOST}:{_RTSP_PORT}/{quote(stream_name, safe='')}"


def probe_local_video(stream_name: str) -> bool:
    """通过 go2rtc 请求一路关键帧，确认局域网取流可用。须在后台调用。"""
    if not ensure_service():
        return False
    connection = HTTPConnection(_HOST, _API_PORT, timeout=3.0)
    try:
        connection.request("GET", f"/api/frame.mp4?{urlencode({'src': stream_name})}")
        response = connection.getresponse()
        # go2rtc 等到真实视频帧才会生成 MP4。服务端口能连通只说明进程在运行。
        return response.status == 200 and response.read(8)[4:] == b"ftyp"
    except (OSError, HTTPException):
        return False
    finally:
        connection.close()


def received_bytes(stream_name: str) -> int | None:
    connection = HTTPConnection(_HOST, _API_PORT, timeout=1.0)
    try:
        connection.request("GET", f"/api/streams?{urlencode({'src': stream_name})}")
        response = connection.getresponse()
        if response.status != 200:
            return None
        data = json.loads(response.read(256 * 1024))
        producers = data.get("producers") if isinstance(data, dict) else None
        counters = [item.get("bytes_recv") for item in (producers or []) if isinstance(item, dict)]
        valid = [value for value in counters if type(value) is int and value >= 0]
        return sum(valid) if valid else None
    except (OSError, HTTPException, ValueError, TypeError):
        return None
    finally:
        connection.close()


def _service_alive() -> bool:
    try:
        with socket.create_connection((_HOST, _API_PORT), timeout=0.25):
            return True
    except OSError:
        return False


def ensure_service() -> bool:
    """检查本地服务，必要时使用已配置的 go2rtc 启动。须在后台调用。"""
    if _service_alive():
        return True

    override = os.environ.get("MIHOME_CAMERA_DIR")
    appdata = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    base = Path(override) if override else appdata / "MiHome-Camera"
    exe = base / "go2rtc.exe"
    config = base / "go2rtc.yaml"
    if not exe.is_file() or not config.is_file():
        return False

    try:
        process = subprocess.Popen(
            [str(exe.resolve()), "-config", str(config.resolve())],
            cwd=base,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False

    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline and process.poll() is None:
        if _service_alive():
            return True
        time.sleep(0.15)
    return _service_alive()
