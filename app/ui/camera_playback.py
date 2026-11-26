# SPDX-License-Identifier: GPL-3.0-or-later
"""单路摄像机视频会话，独立于画面的布局与窗口。"""

from collections.abc import Sequence
from functools import partial
import logging
from pathlib import Path
from time import monotonic

import shiboken6
from PySide6.QtCore import QObject, QSize, QTimer, QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

from app.core import camera_stream
from app.core.camera_rate import ReceiveRate
from app.core.jobs import PRIORITY_CONTROL, JobExecutor
from app.core.models import DeviceInfo
from app.core.service import MijiaService
from app.ui.camera_audio import CameraAudioOutput, CameraMicrophone
from app.ui.camera_relay import CameraRelay, CameraRelayFeed, configured_relay, supports_paired_lenses, supports_second_lens

_PLAYER_RETRIES = 2
_FIRST_FRAME_TIMEOUT_MS = 15_000
_VIDEO_STALL_TIMEOUT_MS = 12_000
_STABLE_VIDEO_SECONDS = 20
logger = logging.getLogger(__name__)


class CameraPlayback(QObject):
    frame_changed = Signal(QImage)
    status_changed = Signal(str)
    failed = Signal(str)
    resolution_changed = Signal(QSize)
    quality_changed = Signal(int, bool)
    audio_failed = Signal(str)
    talk_changed = Signal(bool, str)

    def __init__(self, device: DeviceInfo, lens: int, service: MijiaService,
                 jobs: JobExecutor, parent: QObject | None = None):
        super().__init__(parent)
        self.device = device
        self.lens = lens
        self._service = service
        self._jobs = jobs
        self._views: list[QObject] = []
        self._relay: CameraRelay | CameraRelayFeed | None = None
        self._started = False
        self._stopped = False
        self._url = QUrl()
        self._retries = 0
        self._has_frame = False
        self._decode_started_at = 0.0
        self._stable_since: float | None = None
        self._resolution = QSize()
        self._recv_rate = ReceiveRate()
        self._rate_polling = False
        self._muted = True
        self._talk_requested = False

        self._sink = QVideoSink(self)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._audio = QAudioOutput(self)
        self._audio.setMuted(True)
        self._remote_audio = CameraAudioOutput(self)
        self._remote_audio.failed.connect(self._audio_failed)
        self._microphone = CameraMicrophone(self)
        self._microphone.packet.connect(self._send_talk_packet)
        self._microphone.failed.connect(self.stop_talk)
        self._player = QMediaPlayer(self)
        self._player.setVideoSink(self._sink)
        self._player.setAudioOutput(self._audio)
        self._player.errorOccurred.connect(self._schedule_retry)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.setInterval(3000)
        self._retry_timer.timeout.connect(self._retry)
        self._video_timer = QTimer(self)
        self._video_timer.setSingleShot(True)
        self._video_timer.timeout.connect(self._recover_remote_video)

    @property
    def is_remote(self) -> bool:
        return self._relay is not None

    @property
    def is_active(self) -> bool:
        return self._started and not self._stopped

    @property
    def shared(self) -> bool:
        return len(self._views) > 1

    @property
    def resolution(self) -> QSize:
        return self._resolution

    def retain(self, view: QObject) -> None:
        if not self._views:
            self.setParent(view)
        self._views.append(view)

    def release(self, view: QObject) -> None:
        self._views.remove(view)
        if not self._views:
            self.stop()
            self.deleteLater()
        elif self.parent() is view:
            self.setParent(self._views[0])

    def _begin(self) -> bool:
        if self._started or self._stopped:
            return False
        self._started = True
        self.status_changed.emit("正在检测摄像机连接…")
        return True

    def start(self) -> None:
        if not self._begin():
            return
        stream_name = camera_stream.stream_name(self.device.did, self.lens)
        self._jobs.submit(partial(camera_stream.probe_local_video, stream_name),
                          on_success=self._local_result, on_error=lambda _: self._local_result(False),
                          priority=PRIORITY_CONTROL)

    def _local_result(self, ready: bool) -> None:
        if self._stopped:
            return
        if ready:
            self._play(QUrl(camera_stream.rtsp_url(camera_stream.stream_name(self.device.did, self.lens))))
            return
        base = configured_relay()
        if base is None or (self.lens == 2 and not supports_second_lens(base)):
            self._fail("无法从局域网取流，此镜头需要配置公网中继。")
            return
        relay = self._new_relay(base)
        self._bind_relay(relay)
        self._authorize_relay(relay)

    def _new_relay(self, base: Path, *, paired: bool = False) -> CameraRelay:
        return CameraRelay(base, lens=1 if paired else self.lens, paired=paired, model=self.device.model,
                           dual_lens=camera_stream.has_second_lens(self.device.model),
                           preferred_route=self._service.camera_route_hint(self.device.did))

    def _bind_relay(self, relay: CameraRelay | CameraRelayFeed) -> None:
        self._relay = relay
        self._relay.progress.connect(self.status_changed)
        self._relay.ready.connect(self._play)
        self._relay.failed.connect(self._fail)
        self._relay.recovering.connect(self._stop_player)
        self._relay.received_bytes.connect(self._record_received)
        self._relay.quality_changed.connect(self.quality_changed)
        self._relay.audio_frame.connect(self._on_audio)
        self._relay.audio_failed.connect(self._audio_failed)
        self._relay.talk_ready.connect(self._on_talk_ready)
        self._relay.talk_failed.connect(self.stop_talk)

    def _authorize_relay(self, relay: CameraRelay) -> None:
        relay.authorize(self._jobs, partial(self._service.authorize_camera_relay, self.device.did))

    def resume_shared_feed(self, previous: "CameraPlayback") -> bool:
        if self.device.did != previous.device.did or self.lens not in (1, 2):
            return False
        relay = previous._relay
        if not isinstance(relay, CameraRelayFeed) or not shiboken6.isValid(relay):
            return False
        feed = relay.feed_for(self.lens)
        if not feed.resume():
            return False
        self._started = True
        self._bind_relay(feed)
        if feed.ready_for_video:
            self._play(feed.url)
        return True

    def _play(self, url: QUrl) -> None:
        if self._stopped:
            return
        self._url = url
        if self.is_remote:
            self._decode_started_at = monotonic()
            self._video_timer.start(_FIRST_FRAME_TIMEOUT_MS)
        self.status_changed.emit("正在载入摄像机画面…")
        self._player.setSource(url)
        self._player.play()
        self._apply_mute()

    def _on_frame(self, frame) -> None:
        if self._stopped or self._url.isEmpty() or not frame.isValid():
            return
        image = frame.toImage()
        if not image.isNull():
            self._retry_timer.stop()
            self._retries = 0
            if self.is_remote:
                self._video_timer.start(_VIDEO_STALL_TIMEOUT_MS)
                now = monotonic()
                if not self._has_frame:
                    self._stable_since = now
                    self._service.remember_camera_route(self.device.did, self._relay.transport)
                    logger.info("公网视频首帧 session=%s lens=%d attempt=%s transport=%s decode_ms=%d resolution=%dx%d",
                                self._relay.trace_id, self.lens, self._relay.attempt, self._relay.transport,
                                round((now - self._decode_started_at) * 1000), image.width(), image.height())
                elif self._stable_since is not None and now - self._stable_since >= _STABLE_VIDEO_SECONDS:
                    self._stable_since = None
                    self._relay.video_stable()
            self._has_frame = True
            if image.size() != self._resolution:
                self._resolution = image.size()
                logger.info("镜头 %s: 实际解码分辨率 %s×%s", self.lens, image.width(), image.height())
                self.resolution_changed.emit(self._resolution)
            self.frame_changed.emit(image)

    def _on_status(self, status) -> None:
        if status in (QMediaPlayer.MediaStatus.InvalidMedia, QMediaPlayer.MediaStatus.EndOfMedia) or (
            status == QMediaPlayer.MediaStatus.StalledMedia and not self.is_remote
        ):
            self._schedule_retry()

    def _schedule_retry(self, *_args) -> None:
        if self._stopped or self._url.isEmpty():
            return
        if self.is_remote:
            if not self._retry_timer.isActive():
                self._retry_timer.start()
            return
        if self._retries >= _PLAYER_RETRIES:
            self._fail("视频加载失败，请重试。")
            return
        self.stop_talk("视频连接中断，已结束通话。")
        self._has_frame = False
        self.status_changed.emit("连接中断，正在重试…")
        self._retry_timer.start()

    def _retry(self) -> None:
        if self._stopped or self._url.isEmpty():
            return
        if self.is_remote:
            self._recover_remote_video()
            return
        self._retries += 1
        url = self._url
        self._stop_player()
        self._play(url)

    def _recover_remote_video(self) -> None:
        if not self._stopped and not self._url.isEmpty() and self.is_remote:
            logger.info("公网视频解码中断 session=%s lens=%d attempt=%s reason=%s", self._relay.trace_id, self.lens,
                        self._relay.attempt, "frame_stall" if self._has_frame else "no_first_frame")
            self._relay.recover_video(no_first_frame=not self._has_frame)

    def switch_lens(self, lens: int) -> bool:
        if (self.shared or self._stopped or not self._has_frame or self._relay is None
                or not shiboken6.isValid(self._relay) or not self._relay.switch_lens(lens)):
            return False
        self.lens = lens
        self._stop_player()
        self._retries = 0
        self.status_changed.emit(f"正在通过现有公网连接切换到镜头 {lens}…")
        return True

    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        self._apply_mute()

    def _apply_mute(self) -> None:
        self._audio.setMuted(self._muted or self.is_remote)
        if self._muted:
            self._remote_audio.stop()
        if self._relay is not None and shiboken6.isValid(self._relay):
            self._relay.set_audio(not self._muted or self._talk_requested)

    def _on_audio(self, codec: str, rate: int, data: bytes) -> None:
        if not self._stopped and not self._muted:
            self._remote_audio.write(codec, rate, data)

    def _audio_failed(self, message: str) -> None:
        if not self._stopped and not self._muted:
            self.set_muted(True)
            self.audio_failed.emit(message)

    @property
    def can_talk(self) -> bool:
        relay = self._relay
        return (not self._stopped and self._has_frame and relay is not None
                and shiboken6.isValid(relay) and relay.supports_talk)

    def start_talk(self) -> bool:
        if not self.can_talk or self._talk_requested:
            return False
        self._talk_requested = True
        self._apply_mute()
        if not self._relay.start_talk():
            self._talk_requested = False
            self._apply_mute()
            return False
        self.talk_changed.emit(True, "正在连接摄像头通话…")
        return True

    def _on_talk_ready(self, codec: str) -> None:
        if not self._talk_requested or self._stopped:
            return
        if self._microphone.start(codec):
            self.talk_changed.emit(True, "通话中")
        else:
            self.stop_talk("无法打开麦克风。")

    def _send_talk_packet(self, packet: bytes) -> None:
        if self._talk_requested and not self._stopped and shiboken6.isValid(self._relay):
            self._relay.send_talk_audio(packet)

    def stop_talk(self, message: str = "") -> None:
        if not self._talk_requested:
            return
        self._talk_requested = False
        self._microphone.stop()
        if self._relay is not None and shiboken6.isValid(self._relay):
            self._relay.stop_talk()
        self._apply_mute()
        self.talk_changed.emit(False, message)

    @property
    def quality(self) -> int | None:
        relay = self._relay
        return relay.quality if relay is not None and shiboken6.isValid(relay) and relay.supports_quality else None

    @property
    def quality_options(self) -> tuple[tuple[int, str], ...]:
        relay = self._relay
        return relay.quality_options if self.quality is not None else ()

    def set_quality(self, quality: int) -> bool:
        if not self._has_frame or self.quality is None or not self._relay.set_quality(quality):
            return False
        self._stop_player()
        self._retries = 0
        self.status_changed.emit("正在切换清晰度…")
        return True

    def receive_rate(self) -> float | None:
        return self._recv_rate.current(monotonic())

    def sample_receive_rate(self) -> None:
        if self._stopped or self._url.isEmpty() or self.is_remote or self._rate_polling:
            return
        self._rate_polling = True
        stream_name = camera_stream.stream_name(self.device.did, self.lens)
        self._jobs.submit(partial(camera_stream.received_bytes, stream_name),
                          on_success=self._record_received, on_error=lambda _: self._record_received(None))

    def _record_received(self, total: int | None) -> None:
        self._rate_polling = False
        if total is not None and not self._stopped:
            self._recv_rate.record(total, monotonic())

    def _stop_player(self) -> None:
        self.stop_talk()
        self._url = QUrl()
        self._has_frame = False
        self._stable_since = None
        self._resolution = QSize()
        self.resolution_changed.emit(self._resolution)
        self._retry_timer.stop()
        self._video_timer.stop()
        self._recv_rate.clear()
        self._player.stop()
        self._player.setSource(QUrl())
        self.frame_changed.emit(QImage())

    def stop(self) -> None:
        if self._stopped:
            return
        self.stop_talk()
        self._stopped = True
        self._remote_audio.stop()
        self._recv_rate.clear()
        self._stop_player()
        if self._relay is not None and shiboken6.isValid(self._relay):
            self._relay.stop()

    def _fail(self, message: str) -> None:
        if not self._stopped:
            self.stop()
            self.status_changed.emit(message)
            self.failed.emit(message)


def start_camera_playbacks(playbacks: Sequence[CameraPlayback]) -> None:
    """Use one public connection when both requested lenses need the public path."""
    pending = [playback for playback in playbacks if not playback.is_active]
    if len(playbacks) == 2:
        first, second = playbacks
        if first.device.did == second.device.did and {first.lens, second.lens} == {1, 2}:
            if len(pending) == 1:
                previous = first if pending[0] is second else second
                if pending[0].resume_shared_feed(previous):
                    return
            elif len(pending) == 2:
                base = configured_relay()
                if base is not None and supports_paired_lenses(base):
                    _start_camera_pair(first, second, base)
                    return
    for playback in pending:
        playback.start()


def _start_camera_pair(first: CameraPlayback, second: CameraPlayback, base: Path) -> None:
    playbacks = (first, second)
    for playback in playbacks:
        playback._begin()

    def probed(results: tuple[bool, bool]) -> None:
        remote = []
        for playback, ready in zip(playbacks, results):
            if ready:
                playback._local_result(True)
            elif playback.is_active:
                remote.append(playback)
        if len(remote) == 2:
            relay = first._new_relay(base, paired=True)
            for item in remote:
                item._bind_relay(relay.feeds[item.lens])
            first._authorize_relay(relay)
        else:
            for item in remote:
                item._local_result(False)

    names = [camera_stream.stream_name(item.device.did, item.lens) for item in playbacks]
    first._jobs.submit(partial(camera_stream.probe_local_pair, *names),
                       on_success=probed, on_error=lambda _: probed((False, False)), priority=PRIORITY_CONTROL)
