# SPDX-License-Identifier: GPL-3.0-or-later
# MiHome-Windows: 米家设备的 Windows 桌面控制端
# Copyright (C) 2026 MiHome-Windows contributors
"""设备详情抽屉：与添加功能同款的侧滑抽屉。

不再作为独立窗口弹出，而是以遮罩 + 右侧面板的形式覆盖在主窗口
之上，观感与添加功能抽屉一致；内部直接复用 WorkbenchPanel。
"""

import shiboken6
from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QMenu, QPushButton, QVBoxLayout

from app.core import camera_stream
from app.core.jobs import PRIORITY_CONTROL, JobExecutor
from app.core.models import DeviceInfo
from app.core.service import MijiaService
from app.ui.overlay_dialog import OverlayDialog
from app.ui.si_theme import primary_button_qss, secondary_button_qss
from app.ui.toast import Toast
from app.ui.workbench_panel import WorkbenchPanel


class DeviceDetailDialog(OverlayDialog):
    def __init__(self, service: MijiaService, jobs: JobExecutor,
                 device: DeviceInfo, parent=None):
        super().__init__(parent)
        self.device = device
        self._service = service
        self._jobs = jobs
        self._lens: QComboBox | None = None
        self._pip_button: QPushButton | None = None
        self._web_button: QPushButton | None = None
        self._camera_view = None

        outer = QVBoxLayout(self._panel)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(12)

        header = QHBoxLayout()
        header.addStretch(1)
        if camera_stream.is_camera(device.model):
            if camera_stream.has_second_lens(device.model):
                self._lens = QComboBox()
                self._lens.setAccessibleName("摄像机镜头")
                self._lens.addItem("镜头 1", device.did)
                self._lens.addItem("镜头 2", f"{device.did}-2")
                header.addWidget(self._lens)

            self._web_button = QPushButton("网页播放")
            self._web_button.setToolTip("在浏览器中播放局域网实时画面")
            self._web_button.clicked.connect(lambda: self._start_camera(floating=False))
            header.addWidget(self._web_button)
            self._pip_button = QPushButton("悬浮窗")
            self._pip_button.setToolTip("在置顶小窗中播放，并自动选择局域网或公网中继")
            self._pip_button.clicked.connect(lambda: self._start_camera(floating=True))
            if self._lens is not None:
                self._pip_button.setToolTip("在悬浮窗播放所选镜头。公网播放时直接切换镜头，右键可另开窗口。")
                self._pip_button.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                self._pip_button.customContextMenuRequested.connect(self._camera_pip_menu)
            header.addWidget(self._pip_button)
            for button in (self._web_button, self._pip_button):
                button.setCursor(Qt.CursorShape.PointingHandCursor)
                button.setFixedHeight(30)
            self._style_camera_buttons()
        header.addWidget(self._make_close_button())
        outer.addLayout(header)

        if camera_stream.is_camera(device.model):
            from app.ui.camera_view import CameraView

            self._camera_view = CameraView(device, service, jobs, self._panel)
            outer.addWidget(self._camera_view, stretch=3)
            self.finished.connect(self._camera_view.stop)
            self._camera_view.expanded_changed.connect(self._show_expanded_camera)

        # 工作台
        self._workbench = WorkbenchPanel(service, jobs, self._panel)
        outer.addWidget(self._workbench, stretch=1)

    @property
    def panel(self) -> WorkbenchPanel:
        return self._workbench

    def _style_camera_buttons(self) -> None:
        if self._web_button is not None and self._pip_button is not None:
            self._web_button.setStyleSheet(secondary_button_qss("4px 14px", radius=9, font_size="9pt"))
            self._pip_button.setStyleSheet(primary_button_qss("4px 14px", radius=9, font_size="9pt"))

    def _camera_pip_menu(self, position) -> None:
        menu = QMenu()
        menu.addAction("另开悬浮窗", lambda: self._start_camera(floating=True, reuse_relay=False))
        menu.exec(self._pip_button.mapToGlobal(position))

    def _start_camera(self, *, floating: bool, reuse_relay: bool = True) -> None:
        stream_name = self.device.did if self._lens is None else self._lens.currentData()
        lens = 1 if self._lens is None else self._lens.currentIndex() + 1
        name = self.device.name
        if self._lens is not None:
            name = f"{name} · {self._lens.currentText()}"

        if floating:
            from app.ui.camera_pip import open_camera_pip

            open_camera_pip(self.device, lens, name, self._service, self._jobs, reuse_relay=reuse_relay)
            return
        button = self._web_button
        if button is None or not button.isEnabled():
            return
        button.setEnabled(False)

        def show(ready: bool) -> None:
            if not shiboken6.isValid(self):
                return
            button.setEnabled(True)
            if not self.isVisible():
                return
            if not ready:
                Toast.info(self, "无法连接本地 go2rtc，请检查它的配置和运行状态。")
            elif not QDesktopServices.openUrl(QUrl(camera_stream.live_url(stream_name))):
                Toast.info(self, "无法打开浏览器。")

        self._jobs.submit(camera_stream.ensure_service, on_success=show,
                          on_error=lambda _exc: show(False), priority=PRIORITY_CONTROL)

    def load(self) -> None:
        if self._camera_view is not None:
            self._camera_view.start()
        self._workbench.show_device(self.device.did, online=self.device.online, device=self.device)

    def retheme(self) -> None:
        """主题切换：面板底色 + 工作台头部按钮与功能区块重建。"""
        super().retheme()
        self._workbench.retheme()
        self._style_camera_buttons()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._place_overlay()
        self._layout_panel()

    def _show_expanded_camera(self, expanded: bool) -> None:
        self._workbench.setVisible(not expanded)
        self._layout_panel()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape and self._camera_view is not None and self._camera_view.expanded:
            self._camera_view.collapse()
            event.accept()
            return
        super().keyPressEvent(event)

    def _layout_panel(self) -> None:
        if self._camera_view is not None and self._camera_view.expanded:
            self._panel.setGeometry(self.rect())
            return
        # 居中面板：宽度 900，窗口过窄时自适应收缩
        pw = 900
        pw = min(pw, max(320, self.width() - 40))
        ph = min(640, self.height() - 40)
        x = (self.width() - pw) // 2
        y = (self.height() - ph) // 2
        self._panel.setGeometry(x, y, pw, ph)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if self._fill_parent_window():
            self.raise_()
            self._fade_in()
            return
        # 托盘独立弹出：铺满可用屏幕（遮罩盖住全屏，避免只罩一小块、
        # 周围露出桌面壁纸的突兀观感），面板由 resizeEvent 居中放置
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.availableGeometry())
        self.raise_()
        self._fade_in()


