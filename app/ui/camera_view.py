# SPDX-License-Identifier: GPL-3.0-or-later
"""设备详情里的实时视频及其布局。"""

from PySide6.QtCore import QPointF, QRect, QRectF, QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QComboBox, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.core.camera_stream import has_second_lens
from app.ui.camera_playback import CameraPlayback


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
        if self._thumbnail or self._image.isNull():
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

    def __init__(self, device, lens, service, jobs, parent=None):
        super().__init__(parent)
        self._device, self._lens, self._service, self._jobs = device, lens, service, jobs
        self.playback = None
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
        self.header.addWidget(self._resolution)
        self.header.addStretch()
        self.header.addWidget(self._retry)
        zoom_button = QPushButton("×1.0", self)
        zoom_button.setAccessibleName("恢复画面原始倍率")
        zoom_button.clicked.connect(lambda: self.canvas.set_zoom(1.0))
        self.canvas.zoom_changed.connect(lambda zoom: zoom_button.setText(f"×{zoom:.1f}"))
        self.header.addWidget(zoom_button)
        self._quality = QComboBox(self)
        self._quality.setAccessibleName(f"镜头 {lens} 清晰度")
        for value, label in enumerate(("自动", "标清", "高清")):
            self._quality.addItem(label, value)
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

    def start(self):
        self.stop()
        self._retry.hide()
        self.playback = CameraPlayback(self._device, self._lens, self._service, self._jobs, self)
        self.playback.frame_changed.connect(self.canvas.set_frame)
        self.playback.status_changed.connect(self.canvas.set_status)
        self.playback.failed.connect(self._show_retry)
        self.playback.resolution_changed.connect(self._update_resolution)
        self.playback.quality_changed.connect(self._quality_result)
        self.playback.start()

    def stop(self):
        if self.playback is not None:
            self.playback.stop()
            self.playback.deleteLater()
            self.playback = None

    def _show_retry(self, _message):
        self._retry.show()

    def _select_quality(self, index):
        if self.playback is not None and not self.playback.set_quality(self._quality.itemData(index)):
            self._quality_result(self.playback.quality, False)

    def _quality_result(self, value, accepted):
        with QSignalBlocker(self._quality):
            self._quality.setCurrentIndex(self._quality.findData(value))
        self._quality.setToolTip("" if accepted else "该清晰度未应用，已恢复原档位")

    def _update_resolution(self, size):
        available = size.width() > 0 and size.height() > 0
        self._resolution.setText(f"{size.width()}×{size.height()}" if available else "")
        quality = self.playback.quality if self.playback is not None else None
        self._quality.setVisible(quality is not None)
        self._quality.setEnabled(available)
        if available and quality is not None:
            with QSignalBlocker(self._quality):
                self._quality.setCurrentIndex(self._quality.findData(quality))

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
        lens_count = 2 if has_second_lens(device.model) else 1
        self.tiles = [CameraTile(device, lens, service, jobs, self) for lens in range(1, lens_count + 1)]
        for tile in self.tiles:
            tile.enlarge.connect(lambda t=tile: self._toggle_primary(t))
            tile.canvas.clicked.connect(lambda t=tile: self._toggle_primary(t))
        content = QWidget(self)
        self._grid = QGridLayout(content)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(8)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        title = QLabel(self)
        title.setTextFormat(Qt.TextFormat.PlainText)
        title.setText(device.name)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(title)
        self._speed = QLabel("— KB/s", self)
        self._speed.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._speed.setAccessibleName("视频接收速率")
        outer.addWidget(self._speed)
        outer.addWidget(content, stretch=1)
        self._rate_timer = QTimer(self)
        self._rate_timer.setInterval(1000)
        self._rate_timer.timeout.connect(self._update_rate)
        self._arrange(1)

    def start(self):
        if not self._open:
            self._open = True
            for tile in self.tiles:
                tile.start()
            self._rate_timer.start()

    def stop(self):
        self._open = False
        self._rate_timer.stop()
        self._speed.setText("— KB/s")
        for tile in self.tiles:
            tile.stop()

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
        if not was_expanded:
            self.expanded_changed.emit(True)

    def _size_thumbnail(self):
        width = max(164, min(280, self.width() // 4))
        for tile in self.tiles:
            if tile is not self._primary:
                tile.setFixedSize(width, round(width * 9 / 16))

    def collapse(self):
        if self.expanded:
            self._primary = None
            self._columns = 0
            self._arrange(len(self.tiles) if self.width() >= 640 else 1)
            self.expanded_changed.emit(False)
