# SPDX-License-Identifier: GPL-3.0-or-later
"""设备详情里的实时视频及其布局。"""

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.core.camera_stream import has_second_lens
from app.ui.camera_playback import CameraPlayback


def draw_frame(painter, area, image, status):
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    painter.fillRect(area, QColor("#0b0f0e"))
    if image.isNull():
        painter.setPen(QColor("#a6b0ad"))
        painter.drawText(area.adjusted(8, 0, -8, 0), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, status)
        return
    target = QRect(area.topLeft(), image.size().scaled(area.size(), Qt.AspectRatioMode.KeepAspectRatio))
    target.moveCenter(area.center())
    painter.drawImage(target, image)


class CameraCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._image = QImage()
        self._status = "正在连接摄像机…"
        self.setMinimumHeight(90)

    def set_frame(self, image):
        self._image = image
        self.update()

    def set_status(self, status):
        self._status = status
        self.update()

    def paintEvent(self, event):
        with QPainter(self) as painter:
            draw_frame(painter, self.rect(), self._image, self._status)


class CameraTile(QWidget):
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

        self.header = QHBoxLayout()
        self.header.addWidget(QLabel(f"镜头 {lens}", self))
        self.header.addStretch()
        self.header.addWidget(self._retry)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(self.header)
        layout.addWidget(self.canvas, stretch=1)

    def start(self):
        self.stop()
        self._retry.hide()
        self.playback = CameraPlayback(self._device, self._lens, self._service, self._jobs, self)
        self.playback.frame_changed.connect(self.canvas.set_frame)
        self.playback.status_changed.connect(self.canvas.set_status)
        self.playback.failed.connect(self._show_retry)
        self.playback.start()

    def stop(self):
        if self.playback is not None:
            self.playback.stop()
            self.playback.deleteLater()
            self.playback = None

    def _show_retry(self, _message):
        self._retry.show()


class CameraView(QWidget):
    def __init__(self, device, service, jobs, parent=None):
        super().__init__(parent)
        self._open = False
        self._columns = 0
        lens_count = 2 if has_second_lens(device.model) else 1
        self.tiles = [CameraTile(device, lens, service, jobs, self) for lens in range(1, lens_count + 1)]
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(8)
        self._arrange(1)

    def start(self):
        if not self._open:
            self._open = True
            for tile in self.tiles:
                tile.start()

    def stop(self):
        self._open = False
        for tile in self.tiles:
            tile.stop()

    def _arrange(self, columns):
        if columns == self._columns:
            return
        self._columns = columns
        for index, tile in enumerate(self.tiles):
            self._grid.removeWidget(tile)
            self._grid.addWidget(tile, index // columns, index % columns)
        for index in range(len(self.tiles)):
            self._grid.setColumnStretch(index, int(index < columns))
            self._grid.setRowStretch(index, int(index * columns < len(self.tiles)))

    def resizeEvent(self, event):
        self._arrange(len(self.tiles) if event.size().width() >= 640 else 1)
        super().resizeEvent(event)
