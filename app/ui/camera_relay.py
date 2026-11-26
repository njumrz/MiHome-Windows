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
from time import monotonic
from typing import Callable

from PySide6.QtCore import QCoreApplication, QObject, QProcess, QTimer, QUrl, Signal

from app.core.camera_stream import DEFAULT_RELAY_QUALITIES, has_second_lens, relay_quality_options
from app.core.jobs import PRIORITY_CONTROL, JobExecutor
from app.core.models import CameraRelayAuthorization

logger = logging.getLogger(__name__)
_SECOND_LENS_JAR = "camera-relay-second-lens.jar"
_ROUTE_RACE_JAR = "camera-relay-dual-lens-v10.jar"
_PAIRED_JARS = (_ROUTE_RACE_JAR, "camera-relay-dual-lens-v9.jar")
_TALK_JARS = (*_PAIRED_JARS, "camera-relay-dual-lens-v8.jar", "camera-relay-dual-lens-v7.jar", "camera-relay-dual-lens-v6.jar")
_QUALITY_JARS = (*_TALK_JARS, "camera-relay-dual-lens-v5.jar", "camera-relay-dual-lens-v4.jar")
_DUAL_LENS_JARS = (*_QUALITY_JARS, "camera-relay-dual-lens-v3.jar", "camera-relay-dual-lens-v2.jar", "camera-relay-dual-lens.jar")
_RELAY_TIMEOUT_MS = 15_000
_PUBLIC_RACE_TIMEOUT_MS = 36_000
# 容许中继组件用完自己的 45 秒发现和握手窗口。
_RELAY_RETRY_TIMEOUT_MS = 48_000
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
    b"ERROR SWITCH": "切换镜头后未收到画面，请重试。",
    b"ERROR QUALITY": "切换清晰度后未收到画面，请重试。",
}
_STREAM_LOST = b"ERROR STREAM"
_RECOVERABLE_FAILURES = {
    b"ERROR RELAY": ("小米公网中继未接通，正在重新连接…", "重试后仍无法连接小米公网中继。"),
    b"ERROR MEDIA": ("公网中继未收到画面，正在更换画质重试…", "公网中继已接通，但重试后仍未收到画面。"),
    _STREAM_LOST: ("公网视频已断开，正在重新连接…", "公网视频重连后仍然断开，请重试。"),
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


def supports_paired_lenses(base: Path) -> bool:
    return any((base / name).is_file() for name in _PAIRED_JARS)


def _dual_lens_jar(base: Path) -> Path | None:
    return next((base / name for name in _DUAL_LENS_JARS if (base / name).is_file()), None)


class CameraRelay(QObject):
    progress = Signal(str)
    ready = Signal(QUrl)
    failed = Signal(str)
    retry = Signal()
    recovering = Signal()
    received_bytes = Signal(object)
    quality_changed = Signal(int, bool)
    audio_frame = Signal(str, int, bytes)
    audio_failed = Signal(str)
    talk_ready = Signal(str)
    talk_failed = Signal(str)

    def __init__(self, base: Path, *, lens: int = 1, dual_lens: bool = False, paired: bool = False,
                 model: str = "", preferred_route: str | None = None):
        # 悬浮窗关闭后仍有机会让中继正常释放摄像机会话。
        super().__init__(QCoreApplication.instance())
        self._lens = lens
        self.trace_id = uuid.uuid4().hex[:8]
        dual_jar = _dual_lens_jar(base)
        route_race = dual_jar is not None and dual_jar.name == _ROUTE_RACE_JAR
        self.quality_options = relay_quality_options(model) if route_race else DEFAULT_RELAY_QUALITIES
        self._qualities = {value for value, _ in self.quality_options}
        self._switchable = dual_jar is not None and (dual_lens or lens == 2)
        self.supports_quality = dual_jar is not None and dual_jar.name in _QUALITY_JARS
        self.supports_talk = dual_jar is not None and dual_jar.name in _TALK_JARS
        self._attempt = 0
        self._attempt_started_at = 0.0
        self._phase_started_at = 0.0
        self._relay_started_at = 0.0
        self._relay_progressed = False
        self._using_direct = False
        self._relay_only = False
        self._closed = False
        self._retrying = False
        self._retried: set[bytes] = set()
        self._quality = "1"
        self._pending_quality: int | None = None
        self._changing_feed: int | None = None
        self._audio_enabled = False
        self._talk_requested = False
        self._talk_codec: str | None = None
        self._phase = b""
        self._relay_stage = ""
        self._payload = b""
        self._url = QUrl()
        self.feeds = {index: CameraRelayFeed(self, index) for index in (1, 2)} if paired else {}
        self._process = QProcess(self)
        self._process.setWorkingDirectory(str(base))
        self._process.setProgram(str(base / "runtime/bin/java.exe"))
        classpath = str(base / "camera-relay.jar")
        main_args = ["CameraRelay"]
        if dual_jar is not None:
            classpath = os.pathsep.join((str(dual_jar), classpath))
            main_args = ["CameraRelayDualLens", str(lens)]
            if paired:
                main_args.append("--paired")
                if route_race and has_second_lens(model):
                    main_args.append("--channel-index")
            if route_race and preferred_route == "p2p":
                main_args.append("--prefer-p2p")
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
            if self.feeds:
                # Reserve both ports together so the loopback listeners cannot be issued the same port.
                with socket.socket() as reserved:
                    reserved.bind(("127.0.0.1", self._url.port()))
                    self.feeds[2].url = self._stream_url()
                self.feeds[1].url = self._url
                for feed in self.feeds.values():
                    feed.ready_for_video = False
                    feed.next_url = None
                self._changing_feed = None
        except OSError:
            self._fail("无法为公网视频分配本机端口。")
            return

        fields = (str(self._url.port()), self._url.path(), authorization.uid, authorization.relay_config,
                  authorization.client_public_key, authorization.client_private_key,
                  authorization.camera_public_key, authorization.signature,
                  str(self.feeds[1].quality) if self.feeds else self._quality)
        if self.feeds:
            second = self.feeds[2]
            fields += (str(second.url.port()), second.url.path(), str(second.quality))
        self._payload = ("\n".join(fields) + "\n").encode("utf-8")
        self._phase = b""
        self._relay_stage = ""
        self._attempt += 1
        self._attempt_started_at = self._phase_started_at = monotonic()
        self._relay_progressed = False
        self._using_direct = False
        self._audio_enabled = False
        arguments = self._process.arguments()
        if self._relay_only and "--relay-only" not in arguments:
            self._process.setArguments([*arguments, "--relay-only"])
        self._timeout.start(10_000)
        self._process.start()

    def authorize(self, jobs: JobExecutor, request: Callable[[], CameraRelayAuthorization]) -> None:
        def enqueue() -> None:
            if self._closed:
                return
            queued_at = monotonic()
            attempt = self._attempt + 1
            lenses = self._session_lenses

            def authorize_request() -> CameraRelayAuthorization:
                called_at = monotonic()
                try:
                    return request()
                finally:
                    logger.info("公网授权请求 session=%s lens=%s attempt=%d queue_ms=%d call_ms=%d",
                                self.trace_id, lenses, attempt, round((called_at - queued_at) * 1000),
                                round((monotonic() - called_at) * 1000))

            def authorized(grant: CameraRelayAuthorization) -> None:
                if not self._closed:
                    logger.info("公网授权 session=%s lens=%s attempt=%d result=ok wait_ms=%d",
                                self.trace_id, lenses, attempt, round((monotonic() - queued_at) * 1000))
                    self.start(grant)

            def failed(error: Exception) -> None:
                if not self._closed:
                    self._fail(str(error))

            self.progress.emit("正在获取米家视频授权…")
            jobs.submit(authorize_request, on_success=authorized, on_error=failed, priority=PRIORITY_CONTROL)

        self.retry.connect(enqueue)
        enqueue()

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

    @property
    def attempt(self) -> int:
        return self._attempt

    @property
    def transport(self) -> str:
        return "p2p" if self._using_direct else "relay"

    def set_quality(self, quality: int) -> bool:
        if not self.supports_quality or quality not in self._qualities or quality == self.quality:
            return False
        if not self._change_stream("QUALITY", quality, 48_000):
            return False
        self._pending_quality = quality
        return True

    def video_stable(self) -> None:
        self._retried.clear()

    def recover_video(self, *, no_first_frame: bool = False) -> None:
        if not self._closed and not self._retrying and self._phase == b"READY":
            if no_first_frame and self._using_direct:
                self._fallback_to_relay("no_decoded_frame")
            else:
                self._retry_connection(_STREAM_LOST)

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

    @property
    def _session_lenses(self) -> str:
        return "1,2" if self.feeds else str(self._lens)

    def _log_checkpoint(self, event: str, *, transport: str | None = None) -> None:
        now = monotonic()
        logger.info("公网视频 session=%s lens=%s attempt=%d transport=%s event=%s phase_ms=%d attempt_ms=%d",
                    self.trace_id, self._session_lenses, self._attempt, transport or self.transport, event,
                    round((now - self._phase_started_at) * 1000), round((now - self._attempt_started_at) * 1000))
        self._phase_started_at = now

    def _read_relay_round(self, status: bytes) -> None:
        fields = status.split()[2:]
        if len(fields) != 10 or any(not 0 < len(field) <= 9 or not field.isdigit() for field in fields):
            return
        round_index, millis, candidates, discovery_sent, discovery_ack, offers, hello_sent, hello_ack, port_ack, peer_ready = map(int, fields)
        logger.info("公网握手 session=%s lens=%s attempt=%d round=%d elapsed_ms=%d candidates=%d "
                    "discovery_sent=%d discovery_ack=%d offers=%d hello_sent=%d hello_ack=%d port_ack=%d peer_ready=%d",
                    self.trace_id, self._session_lenses, self._attempt, round_index, millis, candidates,
                    discovery_sent, discovery_ack, offers, hello_sent, hello_ack, port_ack, peer_ready)
        if (self._phase == b"PHASE RELAY" and candidates and hello_sent and not hello_ack
                and not self._relay_progressed and b"ERROR RELAY" not in self._retried):
            self._retry_connection(b"ERROR RELAY", reason="no_hello_ack")

    def _change_feed(self, feed: "CameraRelayFeed", quality: int) -> bool:
        if self._closed or self._phase != b"READY" or self._changing_feed is not None or quality not in self._qualities:
            return False
        try:
            url = self._stream_url()
        except OSError:
            return False
        command = f"FEED {feed.lens} {quality} {url.port()} {url.path()}\n".encode("ascii")
        if self._process.write(command) != len(command):
            return False
        feed.next_url = url
        self._changing_feed = feed.lens
        logger.info("公网画质请求 session=%s lens=%d quality=%d", self.trace_id, feed.lens, quality)
        return True

    def _read_feed(self, status: bytes) -> None:
        fields = status.split()
        if len(fields) < 3 or fields[2] not in (b"1", b"2"):
            return
        feed = self.feeds[int(fields[2])]
        action, data = fields[1], fields[3:]
        if action == b"READY" and self._phase in (b"PHASE MEDIA", b"READY"):
            if self._phase != b"READY":
                self._log_checkpoint("video_keyframe")
                self._phase = b"READY"
                self._timeout.stop()
            logger.info("共享公网视频 session=%s lens=%d attempt=%d transport=%s event=feed_ready",
                        self.trace_id, feed.lens, self._attempt, self.transport)
            feed.ready_for_video = True
            if feed.next_url is not None:
                feed.url = feed.next_url
                feed.next_url = None
            if feed.active:
                feed.ready.emit(feed.url)
        elif action == b"QUALITY" and self._changing_feed == feed.lens and len(data) == 2:
            if len(data[0]) == 1 and data[0].isdigit() and int(data[0]) in self._qualities and data[1] in (b"0", b"1"):
                self._changing_feed = None
                feed.quality = int(data[0])
                ready = data[1] == b"1"
                logger.info("公网画质结果 session=%s lens=%d quality=%d keyframe=%s",
                            self.trace_id, feed.lens, feed.quality, ready)
                feed.quality_changed.emit(feed.quality, ready)
        elif action == b"BYTES" and feed.active and len(data) == 1 and 0 < len(data[0]) <= 19 and data[0].isdigit():
            feed.received_bytes.emit(int(data[0]))
        elif action == b"FAILED":
            feed.ready_for_video = False
            feed.next_url = None
            if feed.active:
                feed.failed.emit(f"共享公网会话未收到镜头 {feed.lens} 的画面，请重试。")

    def _read_status(self) -> None:
        while self._process.canReadLine():
            status = bytes(self._process.readLine()).strip()
            if self._closed or self._retrying:
                continue
            if status.startswith(b"FEED ") and self.feeds:
                self._read_feed(status)
            elif status.startswith(b"AUDIO "):
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
            elif status == b"PHASE PUBLIC":
                self._phase = status
                self._timeout.start(_PUBLIC_RACE_TIMEOUT_MS)
                self.progress.emit("正在寻找可用的摄像机公网连接…")
            elif status in (b"ROUTE START RELAY", b"ROUTE START P2P") and self._phase == b"PHASE PUBLIC":
                route = "p2p" if status.endswith(b"P2P") else "relay"
                self._log_checkpoint("candidate_start", transport=route)
            elif status in (b"ROUTE SELECT RELAY", b"ROUTE SELECT P2P") and self._phase == b"PHASE PUBLIC":
                self._using_direct = status.endswith(b"P2P")
                self._log_checkpoint("route_selected")
            elif status == b"PHASE DIRECT":
                self._phase = status
                self._using_direct = True
                self._log_checkpoint("direct_start")
                self._timeout.start(10_000)
                self.progress.emit("正在尝试公网 P2P 直连…")
            elif status.startswith(b"DIRECT STATS ") and self._phase in (b"PHASE DIRECT", b"PHASE PUBLIC"):
                fields = status.split()[2:]
                if len(fields) == 5 and all(0 < len(field) <= 9 and field.isdigit() for field in fields):
                    logger.info("公网直连 session=%s lens=%s attempt=%d sent=%d ack=%d offers=%d punches=%d elapsed_ms=%d",
                                self.trace_id, self._session_lenses, self._attempt, *map(int, fields))
            elif status == b"DIRECT CONNECTED" and self._phase == b"PHASE DIRECT":
                self._log_checkpoint("direct_connected")
            elif status == b"DIRECT FALLBACK" and self._phase == b"PHASE DIRECT":
                self._log_checkpoint("direct_no_peer")
                self.progress.emit("公网直连未接通，正在切换到中继…")
            elif status == b"PHASE RELAY":
                self._phase = status
                self._using_direct = False
                self._log_checkpoint("relay_start")
                self._relay_started_at = self._phase_started_at
                retry = b"ERROR RELAY" in self._retried
                self._timeout.start(_RELAY_RETRY_TIMEOUT_MS if retry else _RELAY_TIMEOUT_MS)
                message = f"正在连接小米公网中继（第 {self._attempt} 次）…"
                logger.info("镜头 %s: %s", self._session_lenses, message)
                self.progress.emit(message)
            elif status in _RELAY_STAGES and self._phase in (b"PHASE RELAY", b"PHASE PUBLIC"):
                self._relay_stage = _RELAY_STAGES[status]
                if status in (b"RELAY PORT", b"RELAY CAMERA") and not self._relay_progressed:
                    self._relay_progressed = True
                    if self._phase == b"PHASE RELAY":
                        elapsed = round((monotonic() - self._relay_started_at) * 1000)
                        self._timeout.start(max(1, _RELAY_RETRY_TIMEOUT_MS - elapsed))
                self.progress.emit(f"{self._relay_stage}（第 {self._attempt} 次）…")
            elif status.startswith(b"RELAY ROUND ") and self._phase in (b"PHASE RELAY", b"PHASE PUBLIC"):
                self._read_relay_round(status)
            elif status in _PROGRESS:
                self._phase = status
                self._log_checkpoint("transport_ready" if status == b"PHASE AUTH" else "device_authorized")
                timeout, message = _PROGRESS[status]
                self._timeout.start(timeout)
                self.progress.emit(message)
            elif status == _STREAM_LOST:
                self.recover_video()
            elif status in (b"ERROR DIRECT", b"ERROR AUTH", b"ERROR MEDIA") and self._using_direct:
                self._fallback_to_relay(status.decode("ascii"))
            elif status in _RECOVERABLE_FAILURES:
                self._retry_connection(status)
            elif status in (b"QUALITY ACCEPTED", b"QUALITY REJECTED") and self._pending_quality is not None:
                accepted = status == b"QUALITY ACCEPTED"
                logger.info("镜头 %s: 清晰度请求 %s，%s", self._lens, self._pending_quality,
                            "已收到关键帧" if accepted else "已恢复原档位")
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
                    self._log_checkpoint("video_keyframe")
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
        elif self._phase == b"READY":
            self._retry_connection(_STREAM_LOST, process_finished=True)
        elif self._phase == b"PHASE PUBLIC":
            self._retry_connection(b"ERROR RELAY", process_finished=True, reason="helper_exit")
        elif self._using_direct and self._phase in (b"PHASE DIRECT", b"PHASE AUTH", b"PHASE MEDIA"):
            self._fallback_to_relay("helper_exit", process_finished=True)
        else:
            self._fail("公网中继已退出，请重试。")

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
        elif self._phase in (b"PHASE RELAY", b"PHASE PUBLIC"):
            self._retry_connection(b"ERROR RELAY", reason="phase_timeout")
        elif self._using_direct and self._phase in (b"PHASE DIRECT", b"PHASE AUTH", b"PHASE MEDIA"):
            self._fallback_to_relay("phase_timeout")
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

    def _retry_connection(self, status: bytes, *, process_finished: bool = False, reason: str = "helper_error") -> None:
        progress, failure = _RECOVERABLE_FAILURES[status]
        if status == b"ERROR RELAY" and self._relay_stage:
            logger.info("镜头 %s: 第 %s 次公网握手停在「%s」", self._session_lenses, self._attempt, self._relay_stage)
        logger.info("公网重连 session=%s lens=%s attempt=%d status=%s reason=%s retry=%s",
                    self.trace_id, self._session_lenses, self._attempt, status.decode("ascii"), reason, status not in self._retried)
        if status in self._retried:
            self._fail(failure)
            return
        self._retried.add(status)
        if status == b"ERROR MEDIA":
            fallback_quality = 2 if 2 in self._qualities else 1
            if self.feeds:
                for feed in self.feeds.values():
                    feed.quality = fallback_quality
            else:
                self._quality = str(fallback_quality)
        if status == _STREAM_LOST:
            self.recovering.emit()
        logger.info("镜头 %s: %s", self._session_lenses, progress)
        self.progress.emit(progress)
        self._restart(process_finished)

    def _fallback_to_relay(self, reason: str, *, process_finished: bool = False) -> None:
        self._relay_only = True
        logger.info("公网直连回退 session=%s lens=%s attempt=%d reason=%s", self.trace_id, self._session_lenses,
                    self._attempt, reason)
        if self._phase == b"READY":
            self.recovering.emit()
        self.progress.emit("公网直连未收到画面，正在重新授权并切换到中继…")
        self._restart(process_finished)

    def _restart(self, process_finished: bool) -> None:
        self._retrying = not process_finished
        self._phase = b""
        if process_finished:
            self.retry.emit()
        else:
            self._stop_process()

    def _fail(self, message: str) -> None:
        logger.warning("公网视频预览失败 session=%s lens=%s attempt=%d message=%s",
                       self.trace_id, self._session_lenses, self._attempt, message)
        self.stop()
        self.failed.emit(message)


class CameraRelayFeed(QObject):
    """One decoder's controls and events for a shared public connection."""

    ready = Signal(QUrl)
    failed = Signal(str)
    recovering = Signal()
    received_bytes = Signal(object)
    quality_changed = Signal(int, bool)
    supports_quality = True
    supports_talk = True

    def __init__(self, relay: CameraRelay, lens: int):
        super().__init__(relay)
        self._relay = relay
        self.lens = lens
        self.quality_options = relay.quality_options
        self.quality = 1
        self.url = QUrl()
        self.next_url: QUrl | None = None
        self.ready_for_video = False
        self.active = True
        self._audio = False
        self._recovered = False
        self.progress = relay.progress
        self.audio_frame = relay.audio_frame
        self.audio_failed = relay.audio_failed
        self.talk_ready = relay.talk_ready
        self.talk_failed = relay.talk_failed
        self.start_talk = relay.start_talk
        self.stop_talk = relay.stop_talk
        self.send_talk_audio = relay.send_talk_audio
        relay.failed.connect(self.failed)
        relay.recovering.connect(self.recovering)

    @property
    def trace_id(self) -> str:
        return self._relay.trace_id

    @property
    def attempt(self) -> int:
        return self._relay.attempt

    @property
    def transport(self) -> str:
        return self._relay.transport

    def switch_lens(self, _lens: int) -> bool:
        return False

    def feed_for(self, lens: int) -> "CameraRelayFeed":
        return self._relay.feeds[lens]

    def resume(self) -> bool:
        relay = self._relay
        if relay._closed or self.active or relay._phase != b"READY":
            return False
        if not self.ready_for_video and not relay._change_feed(self, self.quality):
            return False
        self.active = True
        self._recovered = False
        return True

    def set_quality(self, quality: int) -> bool:
        return self.active and self.quality != quality and self._relay._change_feed(self, quality)

    def set_audio(self, enabled: bool) -> None:
        self._audio = enabled and self.active
        self._relay.set_audio(any(feed._audio for feed in self._relay.feeds.values()))

    def video_stable(self) -> None:
        self._recovered = False
        self._relay.video_stable()

    def recover_video(self, *, no_first_frame: bool = False) -> None:
        if not self.active:
            return
        other = self._relay.feeds[3 - self.lens]
        if not other.active or not other.ready_for_video:
            self._relay.recover_video(no_first_frame=no_first_frame)
        elif self._relay._changing_feed == self.lens:
            return
        elif not self._recovered and self._relay._change_feed(self, self.quality):
            self._recovered = True
            self.recovering.emit()
        else:
            self.failed.emit(f"镜头 {self.lens} 视频中断，请重试。")

    def stop(self) -> None:
        if self.active:
            self.active = False
            self.set_audio(False)
            if not any(feed.active for feed in self._relay.feeds.values()):
                self._relay.stop()
