# SPDX-License-Identifier: GPL-3.0-or-later
"""单路摄像机视频会话，独立于画面的布局与窗口。"""

from functools import partial
from time import monotonic

import shiboken6
from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QImage
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer, QVideoSink

from app.core import camera_stream
from app.core.camera_rate import ReceiveRate
from app.core.jobs import PRIORITY_CONTROL, JobExecutor
from app.core.models import CameraRelayAuthorization, DeviceInfo
from app.core.service import MijiaService
from app.ui.camera_relay import CameraRelay, configured_relay, supports_second_lens

_PLAYER_RETRIES = 2


class CameraPlayback(QObject):
    frame_changed = Signal(QImage)
    status_changed = Signal(str)
    failed = Signal(str)

    def __init__(self, device: DeviceInfo, lens: int, service: MijiaService,
                 jobs: JobExecutor, parent: QObject | None = None):
        super().__init__(parent)
        self.device = device
        self.lens = lens
        self._service = service
        self._jobs = jobs
        self._relay: CameraRelay | None = None
        self._started = False
        self._stopped = False
        self._url = QUrl()
        self._retries = 0
        self._has_frame = False
        self._recv_rate = ReceiveRate()
        self._rate_polling = False

        self._sink = QVideoSink(self)
        self._sink.videoFrameChanged.connect(self._on_frame)
        self._audio = QAudioOutput(self)
        self._audio.setMuted(True)
        self._player = QMediaPlayer(self)
        self._player.setVideoSink(self._sink)
        self._player.setAudioOutput(self._audio)
        self._player.errorOccurred.connect(self._schedule_retry)
        self._player.mediaStatusChanged.connect(self._on_status)
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.setInterval(3000)
        self._retry_timer.timeout.connect(self._retry)

    @property
    def is_remote(self) -> bool:
        return self._relay is not None

    def start(self) -> None:
        if self._started or self._stopped:
            return
        self._started = True
        self.status_changed.emit("正在检测摄像机连接…")
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
        self._relay = CameraRelay(base, lens=self.lens,
                                  dual_lens=camera_stream.has_second_lens(self.device.model))
        self._relay.progress.connect(self.status_changed)
        self._relay.ready.connect(self._play)
        self._relay.failed.connect(self._fail)
        self._relay.retry.connect(self._authorize)
        self._relay.received_bytes.connect(self._record_received)
        self._authorize()

    def _authorize(self) -> None:
        self._recv_rate.clear()
        self.status_changed.emit("正在获取米家视频授权…")
        self._jobs.submit(partial(self._service.authorize_camera_relay, self.device.did),
                          on_success=self._start_relay, on_error=lambda exc: self._fail(str(exc)),
                          priority=PRIORITY_CONTROL)

    def _start_relay(self, authorization: CameraRelayAuthorization) -> None:
        if not self._stopped:
            self._relay.start(authorization)

    def _play(self, url: QUrl) -> None:
        if self._stopped:
            return
        self._url = url
        self.status_changed.emit("正在载入摄像机画面…")
        self._player.setSource(url)
        self._player.play()

    def _on_frame(self, frame) -> None:
        if self._stopped or self._url.isEmpty() or not frame.isValid():
            return
        image = frame.toImage()
        if not image.isNull():
            self._retry_timer.stop()
            self._retries = 0
            self._has_frame = True
            self.frame_changed.emit(image)

    def _on_status(self, status) -> None:
        if status in (QMediaPlayer.MediaStatus.InvalidMedia, QMediaPlayer.MediaStatus.EndOfMedia,
                      QMediaPlayer.MediaStatus.StalledMedia):
            self._schedule_retry()

    def _schedule_retry(self, *_args) -> None:
        if self._stopped or self._url.isEmpty():
            return
        if self._retries >= _PLAYER_RETRIES:
            self._fail("视频加载失败，请重试。")
            return
        self._has_frame = False
        self.status_changed.emit("连接中断，正在重试…")
        self._retry_timer.start()

    def _retry(self) -> None:
        if self._stopped or self._url.isEmpty():
            return
        self._retries += 1
        url = self._url
        self._stop_player()
        self._play(url)

    def switch_lens(self, lens: int) -> bool:
        if (self._stopped or not self._has_frame or self._relay is None
                or not shiboken6.isValid(self._relay) or not self._relay.switch_lens(lens)):
            return False
        self.lens = lens
        self._stop_player()
        self._retries = 0
        self.status_changed.emit(f"正在通过现有公网连接切换到镜头 {lens}…")
        return True

    def set_muted(self, muted: bool) -> None:
        self._audio.setMuted(muted)

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
        self._url = QUrl()
        self._has_frame = False
        self._retry_timer.stop()
        self._player.stop()
        self._player.setSource(QUrl())
        self.frame_changed.emit(QImage())

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._recv_rate.clear()
        self._stop_player()
        if self._relay is not None and shiboken6.isValid(self._relay):
            self._relay.stop()

    def _fail(self, message: str) -> None:
        if not self._stopped:
            self.stop()
            self.status_changed.emit(message)
            self.failed.emit(message)
