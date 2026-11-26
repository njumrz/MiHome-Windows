# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""用 QtMultimedia 播放摄像机实时流的置顶悬浮窗。"""

import shiboken6
from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QAction, QColor, QGuiApplication, QImage, QPainter
from PySide6.QtWidgets import QMenu, QWidget

from app.core import camera_stream
from app.core.jobs import JobExecutor
from app.core.models import DeviceInfo
from app.core.service import MijiaService
from app.ui.camera_playback import CameraPlayback
from app.ui.camera_view import draw_frame

_PRESETS = (("小", 320), ("中", 460), ("大", 680))
_DEFAULT_WIDTH = 380

# 流名 -> 已打开的窗口，同一设备的两个镜头可以各开一个窗口。
_OPEN: dict[str, "CameraPipWindow"] = {}


class CameraPipWindow(QWidget):
    def __init__(self, playback: CameraPlayback, name: str):
        super().__init__(None, Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool)
        self._stream_name = camera_stream.stream_name(playback.device.did, playback.lens)
        self._playback = playback
        playback.setParent(self)
        self._failed = False
        self._image = QImage()
        self._status_text = "正在连接摄像机…"
        self._aspect = 16 / 9
        self._drag_from: QPoint | None = None
        self._on_top = True
        self._muted = True

        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(f"{name} - 实时画面")
        self.setMinimumSize(240, 135)
        self.setMouseTracking(True)

        playback.frame_changed.connect(self._on_frame)
        playback.status_changed.connect(self._show_status)
        playback.failed.connect(self._on_error)
        self._apply_size(_DEFAULT_WIDTH)
        self._place_default()

    def _show_status(self, message: str) -> None:
        self._status_text = message
        self.update()

    def _on_error(self, _message: str) -> None:
        self._failed = True

    def _on_frame(self, image: QImage) -> None:
        self._image = image
        if image.height() > 0:
            self._aspect = image.width() / image.height()
        self.update()

    def switch_remote_lens(self, stream_name: str, name: str, lens: int) -> bool:
        if not self._playback.switch_lens(lens):
            return False
        _OPEN.pop(self._stream_name, None)
        self._stream_name = stream_name
        _OPEN[stream_name] = self
        self.setWindowTitle(f"{name} - 实时画面")
        return True

    # ---------- 绘制 ----------

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        rect = self.rect().adjusted(1, 1, -1, -1)
        draw_frame(p, rect, self._image, self._status_text)
        # 细边框，深色背景上也能看出窗口边界
        p.setPen(QColor(0x2a, 0x3a, 0x36))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))

    # ---------- 尺寸 ----------

    def _apply_size(self, width: int) -> None:
        height = max(90, round(width / (self._aspect or 16 / 9)))
        self.resize(int(width), int(height))

    def _place_default(self) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        x = area.right() - self.width() - 24
        y = area.bottom() - self.height() - 24
        self.move(max(area.left(), x), max(area.top(), y))

    # ---------- 鼠标 ----------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_from = event.globalPosition().toPoint() - self.pos()
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_from is not None:
            self.move(event.globalPosition().toPoint() - self._drag_from)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_from = None
        event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        # 双击在「小」和「中」之间来回切，不用翻菜单
        current = self.width()
        nxt = _PRESETS[0][1] if current > _PRESETS[0][1] else _PRESETS[1][1]
        self._apply_size(nxt)

    def wheelEvent(self, event) -> None:  # noqa: N802
        step = 40 if event.angleDelta().y() > 0 else -40
        width = min(_PRESETS[-1][1], max(_PRESETS[0][1], self.width() + step))
        self._apply_size(width)
        event.accept()

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        act_top = QAction("窗口置顶", menu)
        act_top.setCheckable(True)
        act_top.setChecked(self._on_top)
        act_top.triggered.connect(self._toggle_on_top)
        menu.addAction(act_top)

        if not self._playback.is_remote:
            act_mute = QAction("静音", menu)
            act_mute.setCheckable(True)
            act_mute.setChecked(self._muted)
            act_mute.triggered.connect(self._toggle_mute)
            menu.addAction(act_mute)

        size_menu = menu.addMenu("窗口大小")
        for label, width in _PRESETS:
            act = QAction(label, size_menu)
            act.triggered.connect(lambda _=False, w=width: self._apply_size(w))
            size_menu.addAction(act)

        menu.addSeparator()
        act_close = QAction("关闭", menu)
        act_close.triggered.connect(self.close)
        menu.addAction(act_close)
        menu.exec(event.globalPos())

    def _toggle_on_top(self, checked: bool) -> None:
        self._on_top = checked
        flags = self.windowFlags()
        if checked:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        else:
            flags &= ~Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)
        self.show()

    def _toggle_mute(self, checked: bool) -> None:
        self._muted = checked
        self._playback.set_muted(checked)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    # ---------- 生命周期 ----------

    def closeEvent(self, event) -> None:  # noqa: N802
        self._playback.stop()
        _OPEN.pop(self._stream_name, None)
        super().closeEvent(event)


def show_existing_pip(stream_name: str) -> CameraPipWindow | None:
    win = _OPEN.get(stream_name)
    if win is None or not shiboken6.isValid(win):
        return None
    if win._failed:
        win.close()
        return None
    win.show()
    win.raise_()
    win.activateWindow()
    return win


def open_camera_pip(device: DeviceInfo, lens: int, name: str, service: MijiaService,
                    jobs: JobExecutor, *, reuse_relay: bool = True) -> CameraPipWindow:
    stream = camera_stream.stream_name(device.did, lens)
    win = show_existing_pip(stream)
    if win is not None:
        return win
    if reuse_relay and camera_stream.has_second_lens(device.model):
        other = _OPEN.get(camera_stream.stream_name(device.did, 3 - lens))
        if (other is not None and shiboken6.isValid(other)
                and other.switch_remote_lens(stream, name, lens)):
            return show_existing_pip(stream)
    playback = CameraPlayback(device, lens, service, jobs)
    win = CameraPipWindow(playback, name)
    _OPEN[stream] = win
    win.show()
    playback.start()
    win.raise_()
    return win
