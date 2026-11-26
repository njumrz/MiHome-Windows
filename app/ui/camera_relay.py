# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 MiHome-Windows contributors
"""本机可选的 MijiaDeck 公网中继进程。"""

from base64 import b64decode, b64encode
from binascii import Error as Base64Error
import logging
import os
import socket
import uuid
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QProcess, QTimer, QUrl, Signal

from app.core.models import CameraRelayAuthorization

logger = logging.getLogger(__name__)
_SECOND_LENS_JAR = "camera-relay-second-lens.jar"
_TALK_JAR = "camera-relay-dual-lens-v6.jar"
_QUALITY_JARS = (_TALK_JAR, "camera-relay-dual-lens-v5.jar", "camera-relay-dual-lens-v4.jar")
_DUAL_LENS_JARS = (*_QUALITY_JARS, "camera-relay-dual-lens-v3.jar", "camera-relay-dual-lens-v2.jar", "camera-relay-dual-lens.jar")
_RELAY_TIMEOUT_MS = 15_000
_RELAY_RETRY_TIMEOUT_MS = 30_000
_RELAY_STAGES = {
    b"RELAY DISCOVERY": "正在寻找中继节点",
    b"RELAY REGISTER": "正在等待中继握手应答",
    b"RELAY PORT": "正在等待中继分配会话端口",
    b"RELAY CAMERA": "正在等待摄像机加入中继",
}
_PROGRESS = {
    b"PHASE AUTH": (18_000, "正在验证摄像机授权…"),
    b"PHASE MEDIA": (23_000, "正在等待摄像机画面…"),
}
_ERRORS = {
    b"ERROR CONFIG": "公网中继组件配置无效。",
    b"ERROR AUTH": "摄像机未接受公网视频授权。",
    b"ERROR SWITCH": "切换镜头后未收到画面，请再次点击「悬浮窗」重试。",
    b"ERROR QUALITY": "切换清晰度后未收到画面，请重试。",
    b"ERROR STREAM": "公网视频已断开，请再次点击「悬浮窗」重试。",
}
_STARTUP_FAILURES = {
    b"ERROR RELAY": ("小米公网中继未接通，正在重新连接…", "重试后仍无法连接小米公网中继。"),
    b"ERROR MEDIA": ("公网中继未收到画面，正在更换画质重试…", "公网中继已接通，但重试后仍未收到画面。"),
}


def configured_relay() -> Path | None:
    path = os.environ.get("MIHOME_CAMERA_RELAY_DIR")
    if path:
        base = Path(path).resolve()
        if (base / "camera-relay.jar").is_file() and (base / "runtime/bin/java.exe").is_file():
            return base
    return None


def supports_second_lens(base: Path) -> bool:
    return _dual_lens_jar(base) is not None or (base / _SECOND_LENS_JAR).is_file()


def _dual_lens_jar(base: Path) -> Path | None:
    return next((base / name for name in _DUAL_LENS_JARS if (base / name).is_file()), None)


class CameraRelay(QObject):
    progress = Signal(str)
    ready = Signal(QUrl)
    failed = Signal(str)
    retry = Signal()
    received_bytes = Signal(object)
    quality_changed = Signal(int, bool)
    audio_frame = Signal(str, int, bytes)
    audio_failed = Signal(str)
    talk_ready = Signal(str)
    talk_failed = Signal(str)

    def __init__(self, base: Path, *, lens: int = 1, dual_lens: bool = False):
        # 悬浮窗关闭后仍有机会让中继正常释放摄像机会话。
        super().__init__(QCoreApplication.instance())
        self._lens = lens
        dual_jar = _dual_lens_jar(base)
        self._switchable = dual_jar is not None and (dual_lens or lens == 2)
        self.supports_quality = dual_jar is not None and dual_jar.name in _QUALITY_JARS
        self.supports_talk = dual_jar is not None and dual_jar.name == _TALK_JAR
        self._attempt = 0
        self._closed = False
        self._retrying = False
        self._retried: set[bytes] = set()
        self._quality = "1"
        self._pending_quality: int | None = None
        self._audio_enabled = False
        self._talk_requested = False
        self._talk_codec: str | None = None
        self._phase = b""
        self._relay_stage = ""
        self._payload = b""
        self._url = QUrl()
        self._process = QProcess(self)
        self._process.setWorkingDirectory(str(base))
        self._process.setProgram(str(base / "runtime/bin/java.exe"))
        classpath = str(base / "camera-relay.jar")
        main_args = ["CameraRelay"]
        if dual_jar is not None:
            classpath = os.pathsep.join((str(dual_jar), classpath))
            main_args = ["CameraRelayDualLens", str(lens)]
        elif lens == 2:
            classpath = os.pathsep.join((str(base / _SECOND_LENS_JAR), classpath))
            main_args = ["CameraRelaySecondLens"]
        self._process.setArguments([
            "-Xmx96m", "-XX:ErrorFile=NUL", "-cp", classpath, *main_args,
        ])
        self._process.setStandardErrorFile(QProcess.nullDevice())
        self._process.started.connect(self._send_authorization)
        self._process.readyReadStandardOutput.connect(self._read_status)
        self._process.finished.connect(self._finished)
        self._process.errorOccurred.connect(self._process_error)
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._on_timeout)
        self._talk_timeout = QTimer(self)
        self._talk_timeout.setSingleShot(True)
        self._talk_timeout.timeout.connect(self._fail_talk)

    def start(self, authorization: CameraRelayAuthorization) -> None:
        if self._closed:
            return
        try:
            self._url = self._stream_url()
        except OSError:
            self._fail("无法为公网视频分配本机端口。")
            return

        fields = (str(self._url.port()), self._url.path(), authorization.uid, authorization.relay_config,
                  authorization.client_public_key, authorization.client_private_key,
                  authorization.camera_public_key, authorization.signature, self._quality)
        self._payload = ("\n".join(fields) + "\n").encode("utf-8")
        self._phase = b""
        self._relay_stage = ""
        self._attempt += 1
        self._audio_enabled = False
        self._timeout.start(10_000)
        self._process.start()

    @staticmethod
    def _stream_url() -> QUrl:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        return QUrl(f"http://127.0.0.1:{port}/camera-{uuid.uuid4().hex}.ts")

    def switch_lens(self, lens: int) -> bool:
        if (not self._switchable or self._closed or self._phase != b"READY"
                or lens not in (1, 2) or lens == self._lens):
            return False
        if not self._change_stream("SWITCH", lens, 26_000):
            return False
        self._lens = lens
        logger.info("正在通过现有公网会话切换到镜头 %s", lens)
        return True

    @property
    def quality(self) -> int:
        return int(self._quality)

    def set_quality(self, quality: int) -> bool:
        if not self.supports_quality or quality not in (0, 1, 2) or quality == self.quality:
            return False
        if not self._change_stream("QUALITY", quality, 48_000):
            return False
        self._pending_quality = quality
        return True

    def set_audio(self, enabled: bool) -> None:
        if self._closed or self._phase != b"READY" or self._audio_enabled == enabled:
            return
        command = b"AUDIO 1\n" if enabled else b"AUDIO 0\n"
        if self._process.write(command) == len(command):
            self._audio_enabled = enabled

    def start_talk(self) -> bool:
        if (self._closed or not self.supports_talk or self._phase != b"READY"
                or not self._audio_enabled or self._talk_requested):
            return False
        command = b"TALK START\n"
        if self._process.write(command) != len(command):
            return False
        self._talk_requested = True
        self._talk_timeout.start(7000)
        return True

    def stop_talk(self) -> None:
        self._talk_timeout.stop()
        if self._talk_requested:
            self._talk_requested = False
            self._talk_codec = None
            self._process.write(b"TALK STOP\n")

    def send_talk_audio(self, packet: bytes) -> None:
        if (self._closed or self._phase != b"READY" or self._talk_codec is None
                or not 0 < len(packet) <= 1275 or self._process.bytesToWrite() > 4096):
            return
        self._process.write(b"TALK DATA " + b64encode(packet) + b"\n")

    def _fail_talk(self) -> None:
        if self._talk_requested:
            self.stop_talk()
            self.talk_failed.emit("摄像头通话连接超时或音频格式不可用。")

    def _read_audio(self, line: bytes) -> None:
        if not self._audio_enabled:
            return
        parts = line.split(b" ", 2)
        if len(parts) != 3 or parts[1] not in (b"8000", b"16000", b"OPUS") or len(parts[2]) > 8192:
            return
        try:
            pcm = b64decode(parts[2], validate=True)
        except Base64Error:
            return
        if not pcm:
            return
        if parts[1] == b"OPUS":
            self.audio_frame.emit("opus", 48000, pcm)
        elif len(pcm) % 2 == 0:
            self.audio_frame.emit("pcm", int(parts[1]), pcm)

    def _change_stream(self, action: str, value: int, timeout: int) -> bool:
        if self._closed or self._phase != b"READY":
            return False
        try:
            url = self._stream_url()
        except OSError:
            return False
        command = f"{action} {value} {url.port()} {url.path()}\n".encode("ascii")
        if self._process.write(command) != len(command):
            return False
        self._url = url
        self._phase = action.encode("ascii")
        self._timeout.start(timeout)
        return True

    def stop(self) -> None:
        if self._closed:
            return
        self.stop_talk()
        self._closed = True
        self._payload = b""
        if self._process.state() == QProcess.ProcessState.NotRunning:
            self._timeout.stop()
            self.deleteLater()
        else:
            self._stop_process()

    def _stop_process(self) -> None:
        self._process.write(b"STOP\n")
        self._process.closeWriteChannel()
        self._timeout.start(1500)

    def _send_authorization(self) -> None:
        if not self._closed:
            self._process.write(self._payload)
            self._payload = b""

    def _read_status(self) -> None:
        while self._process.canReadLine():
            status = bytes(self._process.readLine()).strip()
            if self._closed or self._retrying:
                continue
            if status.startswith(b"AUDIO "):
                self._read_audio(status)
            elif status in (b"TALK OPUS", b"TALK PCMA", b"TALK PCMU") and self._talk_requested and self._talk_codec is None:
                self._talk_timeout.stop()
                self._talk_codec = status[5:].decode("ascii").lower()
                self.talk_ready.emit(self._talk_codec)
            elif status == b"TALK FAILED":
                self._fail_talk()
            elif status == b"TALK PEER RESPONSE" and self._talk_requested:
                logger.info("镜头 %s: 收到摄像头的通话应答", self._lens)
            elif status == b"NOTICE AUDIO_UNSUPPORTED" and self._audio_enabled:
                self.audio_failed.emit("摄像头返回了当前不支持的音频格式。")
            elif status.startswith(b"BYTES "):
                value = status[6:]
                if 0 < len(value) <= 19 and value.isdigit():
                    self.received_bytes.emit(int(value))
            elif status == b"PHASE RELAY":
                self._phase = status
                retry = b"ERROR RELAY" in self._retried
                self._timeout.start(_RELAY_RETRY_TIMEOUT_MS if retry else _RELAY_TIMEOUT_MS)
                message = f"正在连接小米公网中继（第 {self._attempt} 次）…"
                logger.info("镜头 %s: %s", self._lens, message)
                self.progress.emit(message)
            elif status in _RELAY_STAGES and self._phase == b"PHASE RELAY":
                self._relay_stage = _RELAY_STAGES[status]
                self.progress.emit(f"{self._relay_stage}（第 {self._attempt} 次）…")
            elif status in _PROGRESS:
                self._phase = status
                timeout, message = _PROGRESS[status]
                self._timeout.start(timeout)
                self.progress.emit(message)
            elif status in _STARTUP_FAILURES:
                self._retry_connection(status)
            elif status in (b"QUALITY ACCEPTED", b"QUALITY REJECTED") and self._pending_quality is not None:
                accepted = status == b"QUALITY ACCEPTED"
                if accepted:
                    self._quality = str(self._pending_quality)
                self._pending_quality = None
                self.quality_changed.emit(self.quality, accepted)
            elif status in _ERRORS:
                self._fail(_ERRORS[status])
            elif status == b"READY":
                if self._phase in (b"SWITCH", b"QUALITY"):
                    logger.info("镜头 %s: 切换后已收到视频关键帧", self._lens)
                else:
                    logger.info("镜头 %s: 第 %s 次公网连接已收到视频关键帧", self._lens, self._attempt)
                self._phase = status
                self._timeout.stop()
                self.ready.emit(self._url)

    def _finished(self, _code, _status) -> None:
        self._timeout.stop()
        if self._closed:
            self.deleteLater()
        elif self._retrying:
            self._retrying = False
            self.retry.emit()
        else:
            self._fail("公网中继已退出，请再次点击「悬浮窗」重试。")

    def _process_error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            if self._closed:
                self._timeout.stop()
                self.deleteLater()
            else:
                self._fail("无法启动本机公网中继组件。")

    def _on_timeout(self) -> None:
        if self._closed or self._retrying:
            self._process.kill()
        elif self._phase == b"PHASE RELAY":
            self._retry_connection(b"ERROR RELAY")
        elif self._phase == b"PHASE AUTH":
            self._fail("摄像机公网视频授权验证超时。")
        elif self._phase == b"PHASE MEDIA":
            self._retry_connection(b"ERROR MEDIA")
        elif self._phase == b"SWITCH":
            self._fail(_ERRORS[b"ERROR SWITCH"])
        elif self._phase == b"QUALITY":
            self._fail(_ERRORS[b"ERROR QUALITY"])
        else:
            self._fail("连接摄像机公网中继超时。")

    def _retry_connection(self, status: bytes) -> None:
        progress, failure = _STARTUP_FAILURES[status]
        if status == b"ERROR RELAY" and self._relay_stage:
            logger.info("镜头 %s: 第 %s 次公网握手停在「%s」", self._lens, self._attempt, self._relay_stage)
        if status in self._retried:
            self._fail(failure)
            return
        self._retried.add(status)
        if status == b"ERROR MEDIA":
            self._quality = "2"
        self._retrying = True
        logger.info("镜头 %s: %s", self._lens, progress)
        self.progress.emit(progress)
        self._stop_process()

    def _fail(self, message: str) -> None:
        logger.warning("镜头 %s 公网视频预览失败: %s", self._lens, message)
        self.stop()
        self.failed.emit(message)
