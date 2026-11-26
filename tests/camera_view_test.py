# SPDX-License-Identifier: GPL-3.0-or-later
"""应用内摄像机视图使用虚构设备和模拟播放会话。"""

import os
import unittest
from unittest.mock import Mock, patch

from tests._isolate import isolate

isolate()
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, QPointF, Qt, Signal
from PySide6.QtGui import QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.core.models import DeviceInfo
from app.ui.camera_view import CameraView
from app.ui.device_dialog import DeviceDetailDialog


class FakePlayback(QObject):
    frame_changed = Signal(QImage)
    status_changed = Signal(str)
    failed = Signal(str)

    def __init__(self, _device, lens, _service, _jobs, parent=None):
        super().__init__(parent)
        self.lens = lens
        self.start = Mock()
        self.stop = Mock()


class CameraViewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.device = DeviceInfo("fixture", "Test camera", "test.camera.example", "Test room", "Test home", True)
        patcher = patch("app.ui.camera_view.CameraPlayback", FakePlayback)
        patcher.start()
        self.addCleanup(patcher.stop)

    def make_view(self, dual):
        with patch("app.ui.camera_view.has_second_lens", return_value=dual):
            view = CameraView(self.device, Mock(), Mock())
        self.addCleanup(view.stop)
        self.addCleanup(view.close)
        return view

    def test_dual_feed_retries_and_resizes_independently(self):
        view = self.make_view(dual=True)
        view.resize(500, 300)
        view.show()
        view.start()
        view.start()
        first, second = view.tiles
        original_first, original_second = first.playback, second.playback
        original_first.start.assert_called_once()
        original_second.start.assert_called_once()
        self.assertEqual(view._grid.getItemPosition(view._grid.indexOf(second))[:2], (1, 0))
        view.resize(800, 300)
        self.assertEqual(view._grid.getItemPosition(view._grid.indexOf(second))[:2], (0, 1))
        original_second.failed.emit("连接失败")
        self.assertFalse(second._retry.isHidden())
        self.assertTrue(first._retry.isHidden())
        second._retry.click()
        self.assertIs(first.playback, original_first)
        self.assertIsNot(second.playback, original_second)
        original_first.stop.assert_not_called()
        original_second.stop.assert_called_once()
        view.stop()
        original_first.stop.assert_called_once()
        self.assertIsNone(second.playback)

    def test_device_detail_starts_and_stops_a_single_lens(self):
        dialog = DeviceDetailDialog(Mock(), Mock(), self.device)
        dialog.load()
        playback = dialog._camera_view.tiles[0].playback
        self.assertEqual(playback.lens, 1)
        playback.start.assert_called_once()
        self.assertEqual(len(dialog._camera_view.tiles), 1)
        dialog.finished.emit(0)
        playback.stop.assert_called_once()
        dialog.deleteLater()

    def test_expand_swap_zoom_and_escape_preserve_both_sessions(self):
        with patch("app.ui.camera_view.has_second_lens", return_value=True):
            dialog = DeviceDetailDialog(Mock(), Mock(), self.device)
        dialog.resize(900, 650)
        dialog.load()
        view = dialog._camera_view
        first, second = view.tiles
        active = [tile.playback for tile in view.tiles]
        first._expand.click()
        self.assertTrue(view.expanded)
        self.assertEqual(dialog._panel.geometry(), dialog.rect())
        self.assertTrue(second.canvas._thumbnail)
        second.canvas.clicked.emit()
        self.assertIs(view._primary, second)
        self.assertTrue(first.canvas._thumbnail)
        second.canvas.set_zoom(10)
        second.canvas._pan_to(QPointF(0, 1))
        self.assertEqual(second.canvas._zoom, 4.0)
        self.assertEqual(second.canvas._center, QPointF(0.125, 0.875))
        second.canvas.set_zoom(1)
        self.assertEqual(second.canvas._center, QPointF(0.5, 0.5))
        QTest.keyClick(dialog, Qt.Key.Key_Escape)
        self.assertFalse(view.expanded)
        self.assertEqual([tile.playback for tile in view.tiles], active)
        for playback in active:
            playback.start.assert_called_once()
            playback.stop.assert_not_called()
        view.stop()
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
