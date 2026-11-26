# SPDX-License-Identifier: GPL-3.0-or-later
"""设备详情里的实时视频及其布局。"""

from PySide6.QtCore import QObject, QPointF, QRect, QRectF, QSize, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QComboBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.core.camera_stream import has_second_lens, stream_name
from app.ui.camera_playback import CameraPlayback, start_camera_playbacks


def draw_frame(painter, area, image, status, zoom=1.0, center=None):
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.fillRect(area, QColor("#0b0f0e"))
    if image.isNull():
        painter.setPen(QColor("#a6b0ad"))
        painter.drawText(area.adjusted(8, 0, -8, 0), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, status)
        return
    target = QRect(area.topLeft(), image.size().scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio))
    target.moveCenter(area.center())
    center = center if center is not None else QPointF(0.5, 0.5)
    width, height = image.width() / zoom, image.height() / zoom
    source = QRectF(center.x() * image.width() - width / 2,
                    center.y() * image.height() - height / 2, width, height)
    painter.drawImage(QRectF(target), image, source)


class CameraCanvas(QWidget):
    clicked = Signal()
    zoom_changed = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = QImage()
        self._status = "正在连接摄像机…"
        self._zoom = 1.0
        self._center = QPointF(0.5, 0.5)
        self._anchor = None
        self._thumbnail = False
        self.setMinimumHeight(90)
        self.setToolTip("按住 Ctrl 滚轮缩放，放大后可拖动画面")

    def sizeHint(self):
        return QSize(320, 180)

    def set_frame(self, image):
        self._image = image
        self.update()

    def set_status(self, status):
        self._status = status
        self.update()

    def paintEvent(self, event):
        with QPainter(self) as painter:
            if self._thumbnail:
                draw_frame(painter, self.rect().adjusted(2, 2, -2, -2), self._image, self._status)
                painter.setPen(QColor("#eeeeee"))
                painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
            else:
                draw_frame(painter, self.rect(), self._image, self._status, self._zoom, self._center)

    def set_thumbnail(self, enabled):
        self._thumbnail = enabled
        self.setCursor(Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus if enabled else Qt.FocusPolicy.NoFocus)
        self.update()

    def set_zoom(self, zoom):
        self._zoom = min(4.0, max(1.0, zoom))
        self._pan_to(self._center)
        self.zoom_changed.emit(self._zoom)

    def _pan_to(self, center):
        edge = 0.5 / self._zoom
        self._center = QPointF(min(1 - edge, max(edge, center.x())), min(1 - edge, max(edge, center.y())))
        self.update()

    def wheelEvent(self, event):
        if (self._thumbnail or self._image.isNull()
                or not event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            event.ignore()
            return
        self.set_zoom(self._zoom * 1.25 ** (event.angleDelta().y() / 120))
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._anchor = event.position()

    def mouseMoveEvent(self, event):
        if self._anchor is not None and not self._thumbnail and self._zoom > 1 and not self._image.isNull():
            size = self._image.size().scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio)
            delta = event.position() - self._anchor
            self._anchor = event.position()
            self._pan_to(self._center - QPointF(delta.x() / max(1, size.width()) / self._zoom,
                                                delta.y() / max(1, size.height()) / self._zoom))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._anchor is not None:
            self._anchor = None
            if self._thumbnail:
                self.clicked.emit()

    def keyPressEvent(self, event):
        if self._thumbnail and event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit()
            return
        super().keyPressEvent(event)


class CameraTile(QWidget):
    enlarge = Signal()
    session_changed = Signal()
    audio_failed = Signal(str)
    talk_changed = Signal(bool, str)
    video_changed = Signal()

    def __init__(self, device, lens, service, jobs, parent=None):
        super().__init__(parent)
        self._device, self._lens, self._service, self._jobs = device, lens, service, jobs
        self.playback = None
        self._playback_connections = []
        self.canvas = CameraCanvas(self)
        self.canvas.setAccessibleName(f"镜头 {lens} 实时画面")
        self._retry = QPushButton("重试", self)
        self._retry.setAccessibleName(f"重试镜头 {lens}")
        self._retry.hide()
        self._retry.clicked.connect(self.start)

        self._header_widget = QWidget(self)
        self.header = QHBoxLayout(self._header_widget)
        self.header.setContentsMargins(0, 0, 0, 0)
        self.header.addWidget(QLabel(f"镜头 {lens}", self))
        self._resolution = QLabel(self)
        self._resolution_before_quality = None
        self.header.addWidget(self._resolution)
        self.header.addStretch()
        self.header.addWidget(self._retry)
        zoom_button = QPushButton("×1.0", self)
        zoom_button.setAccessibleName("恢复画面原始倍率")
        zoom_button.setToolTip("重置数字缩放。按住 Ctrl 滚轮可放大或缩小画面。")
        zoom_button.clicked.connect(lambda: self.canvas.set_zoom(1.0))
        self.canvas.zoom_changed.connect(lambda zoom: zoom_button.setText(f"×{zoom:.1f}"))
        self.header.addWidget(zoom_button)
        self._quality = QComboBox(self)
        self._quality.setAccessibleName(f"镜头 {lens} 清晰度")
        self._quality_options = ()
        self._quality.hide()
        self._quality.activated.connect(self._select_quality)
        self.header.addWidget(self._quality)
        self._expand = QPushButton("放大", self)
        self._expand.clicked.connect(self.enlarge)
        self.header.addWidget(self._expand)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self._header_widget)
        layout.addWidget(self.canvas, stretch=1)

    def start(self, *, autoplay=True):
        from app.ui.camera_pip import existing_pip_playback

        previous = self.playback
        self.stop()
        self._retry.hide()
        self._resolution_before_quality = None
        self._quality.setToolTip("")
        playback = existing_pip_playback(stream_name(self._device.did, self._lens))
        if playback is None:
            playback = CameraPlayback(self._device, self._lens, self._service, self._jobs, self)
        self.playback = playback
        playback.retain(self)
        self._playback_connections = [
            playback.frame_changed.connect(self.canvas.set_frame),
            playback.status_changed.connect(self.canvas.set_status),
            playback.failed.connect(self._show_retry),
            playback.resolution_changed.connect(self._update_resolution),
            playback.quality_changed.connect(self._quality_result),
            playback.audio_failed.connect(self.audio_failed),
            playback.talk_changed.connect(self.talk_changed),
        ]
        self.canvas.set_frame(QImage())
        self._update_resolution(playback.resolution)
        self.session_changed.emit()
        if autoplay and not playback.is_active:
            if previous is None or not playback.resume_shared_feed(previous):
                playback.start()
        return playback

    def stop(self):
        if self.playback is not None:
            for connection in self._playback_connections:
                QObject.disconnect(connection)
            self._playback_connections.clear()
            self.playback.release(self)
            self.playback = None

    def _show_retry(self, _message):
        self._retry.show()

    def _select_quality(self, index):
        if self.playback is None:
            return
        requested = self._quality.itemData(index)
        if requested == self.playback.quality:
            return
        previous = self._resolution.text()
        if self.playback.set_quality(requested):
            self._resolution_before_quality = previous
        else:
            self._quality_result(self.playback.quality, False)

    def _quality_result(self, value, accepted):
        with QSignalBlocker(self._quality):
            self._quality.setCurrentIndex(self._quality.findData(value))
        if accepted:
            self._quality.setToolTip("已发送档位请求，实际分辨率以解码画面为准。")
        else:
            self._resolution_before_quality = None
            self._quality.setToolTip("该清晰度未应用，已恢复原档位")

    def _update_resolution(self, size):
        available = size.width() > 0 and size.height() > 0
        resolution = f"{size.width()}×{size.height()}" if available else ""
        self._resolution.setText(resolution)
        if available and self._resolution_before_quality is not None:
            if resolution == self._resolution_before_quality:
                self._quality.setToolTip(f"档位已请求，摄像头当前仍输出 {resolution}。")
            else:
                self._quality.setToolTip(f"摄像头当前输出 {resolution}。")
                self._resolution_before_quality = None
        quality = self.playback.quality if self.playback is not None else None
        options = self.playback.quality_options if quality is not None else ()
        if options != self._quality_options:
            self._quality_options = options
            with QSignalBlocker(self._quality):
                self._quality.clear()
                for value, label in options:
                    self._quality.addItem(label, value)
        self._quality.setVisible(quality is not None)
        self._quality.setEnabled(available)
        if available and quality is not None:
            with QSignalBlocker(self._quality):
                self._quality.setCurrentIndex(self._quality.findData(quality))
        self.video_changed.emit()

    def set_view_mode(self, expanded, thumbnail):
        self._header_widget.setVisible(not thumbnail)
        self.canvas.set_thumbnail(thumbnail)
        self._expand.setText("退出放大" if expanded else "放大")


class CameraView(QWidget):
    expanded_changed = Signal(bool)

    def __init__(self, device, service, jobs, parent=None):
        super().__init__(parent)
        self._open = False
        self._columns = 0
        self._primary = None
        self._talk_tile = None
        lens_count = 2 if has_second_lens(device.model) else 1
        self.tiles = [CameraTile(device, lens, service, jobs, self) for lens in range(1, lens_count + 1)]
        for tile in self.tiles:
            tile.enlarge.connect(lambda t=tile: self._toggle_primary(t))
            tile.canvas.clicked.connect(lambda t=tile: self._toggle_primary(t))
            tile.audio_failed.connect(lambda message, t=tile: self._audio_failed(t, message))
            tile.session_changed.connect(self._sync_audio)
            tile.session_changed.connect(self._sync_talk_button)
            tile.video_changed.connect(self._sync_talk_button)
            tile.talk_changed.connect(lambda active, message, t=tile: self._talk_state(t, active, message))
        content = QWidget(self)
        self._grid = QGridLayout(content)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(8)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self._speed = QLabel("— KB/s", self)
        self._speed.setAccessibleName("视频接收速率")
        self._speaker = QPushButton("声音", self)
        self._speaker.setCheckable(True)
        self._speaker.setAccessibleName("播放现场声音")
        self._speaker.toggled.connect(self._sync_audio)
        controls = QHBoxLayout()
        controls.addWidget(self._speed)
        controls.addStretch()
        controls.addWidget(self._speaker)
        self._talk = QPushButton("通话", self)
        self._talk.setCheckable(True)
        self._talk.setEnabled(False)
        self._talk.setToolTip("支持公网通话的画面载入后可开启麦克风")
        self._talk.toggled.connect(self._toggle_talk)
        controls.addWidget(self._talk)
        outer.addLayout(controls)
        self._audio_status = QLabel(self)
        outer.addWidget(self._audio_status, alignment=Qt.AlignmentFlag.AlignRight)
        self._audio_status.hide()
        self._talk_status = QLabel(self)
        outer.addWidget(self._talk_status, alignment=Qt.AlignmentFlag.AlignRight)
        self._talk_status.hide()
        outer.addWidget(content, stretch=1)
        self._rate_timer = QTimer(self)
        self._rate_timer.setInterval(1000)
        self._rate_timer.timeout.connect(self._update_rate)
        self._arrange(1)

    def start(self):
        if not self._open:
            self._open = True
            start_camera_playbacks([tile.start(autoplay=False) for tile in self.tiles])
            self._rate_timer.start()

    def stop(self):
        self._open = False
        self._hang_up()
        self._speaker.setChecked(False)
        self._audio_status.hide()
        self._rate_timer.stop()
        self._speed.setText("— KB/s")
        for tile in self.tiles:
            tile.stop()

    def _active_tile(self):
        return self._primary or self.tiles[0]

    def _sync_audio(self):
        active = self._active_tile()
        self._audio_status.hide()
        for tile in self.tiles:
            if tile is not active and tile.playback is not None:
                tile.playback.set_muted(True)
        if active.playback is not None:
            active.playback.set_muted(not self._speaker.isChecked())

    def _audio_failed(self, tile, message):
        if tile is self._active_tile() and self._speaker.isChecked():
            self._speaker.setChecked(False)
            self._audio_status.setText(message)
            self._audio_status.show()

    def _sync_talk_button(self):
        playback = self._active_tile().playback
        self._talk.setEnabled(self._talk_tile is not None or (playback is not None and playback.can_talk))

    def _toggle_talk(self, enabled):
        if not enabled:
            self._hang_up()
            return
        tile = self._active_tile()
        self._talk_tile = tile
        if tile.playback is None or not tile.playback.start_talk():
            self._talk_state(tile, False, "当前摄像头连接暂无法通话。")

    def _talk_state(self, tile, active, message):
        if tile is not self._talk_tile:
            return
        with QSignalBlocker(self._talk):
            self._talk.setChecked(active)
        self._talk.setText("结束通话" if active else "通话")
        self._talk_status.setText(message)
        self._talk_status.setVisible(bool(message))
        if not active:
            self._talk_tile = None
        self._sync_talk_button()

    def _hang_up(self):
        tile = self._talk_tile
        if tile is not None:
            if tile.playback is not None:
                tile.playback.stop_talk()
            self._talk_state(tile, False, "")

    def _update_rate(self):
        rates = []
        for tile in self.tiles:
            if tile.playback is not None:
                tile.playback.sample_receive_rate()
                rate = tile.playback.receive_rate()
                if rate is not None:
                    rates.append(rate)
        self._speed.setText(f"{sum(rates) / 1000:.1f} KB/s" if rates else "— KB/s")

    def _arrange(self, columns):
        if columns == self._columns:
            return
        self._columns = columns
        for index, tile in enumerate(self.tiles):
            self._grid.removeWidget(tile)
            tile.set_view_mode(False, False)
            tile.setMinimumSize(0, 0)
            tile.setMaximumSize(16_777_215, 16_777_215)
            self._grid.addWidget(tile, index // columns, index % columns)
        for index in range(len(self.tiles)):
            self._grid.setColumnStretch(index, int(index < columns))
            self._grid.setRowStretch(index, int(index * columns < len(self.tiles)))

    def resizeEvent(self, event):
        if self.expanded:
            self._size_thumbnail()
        else:
            self._arrange(len(self.tiles) if event.size().width() >= 640 else 1)
        super().resizeEvent(event)

    @property
    def expanded(self):
        return self._primary is not None

    def _toggle_primary(self, tile):
        if self._primary is tile:
            self.collapse()
            return
        was_expanded = self.expanded
        if self._active_tile() is not tile:
            self._hang_up()
        self._primary = tile
        for index, item in enumerate(self.tiles):
            self._grid.removeWidget(item)
            item.setMinimumSize(0, 0)
            item.setMaximumSize(16_777_215, 16_777_215)
            item.set_view_mode(item is tile, item is not tile)
            self._grid.setColumnStretch(index, int(index == 0))
            self._grid.setRowStretch(index, int(index == 0))
        self._grid.addWidget(tile, 0, 0)
        for item in self.tiles:
            if item is not tile:
                self._grid.addWidget(item, 0, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
                item.raise_()
        self._size_thumbnail()
        self._sync_audio()
        self._sync_talk_button()
        if not was_expanded:
            self.expanded_changed.emit(True)

    def _size_thumbnail(self):
        width = max(164, min(280, self.width() // 4))
        for tile in self.tiles:
            if tile is not self._primary:
                tile.setFixedSize(width, round(width * 9 / 16))

    def collapse(self):
        if self.expanded:
            if self._primary is not self.tiles[0]:
                self._hang_up()
            self._primary = None
            self._columns = 0
            self._arrange(len(self.tiles) if self.width() >= 640 else 1)
            self._sync_audio()
            self._sync_talk_button()
            self.expanded_changed.emit(False)
