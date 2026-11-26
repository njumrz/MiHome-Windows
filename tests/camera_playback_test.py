# SPDX-License-Identifier: GPL-3.0-or-later
"""局域网视频探测和公网中继重连的离线回归测试。"""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests._isolate import isolate

_DATA_DIR = Path(isolate())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer, QUrl
from PySide6.QtWidgets import QApplication

from app.core.camera_stream import probe_local_video
from app.core.models import CameraRelayAuthorization, DeviceInfo
from app.ui.camera_pip import CameraPipWindow, _OPEN, open_camera_pip, show_existing_pip
from app.ui.camera_playback import CameraPlayback
from app.ui.camera_relay import CameraRelay


class CameraPlaybackTest(unittest.TestCase):
    def setUp(self):
        self.device = DeviceInfo("fixture", "Test camera", "test.camera.example", "Test home", "Test room", True)
        self.service = Mock()
        self.jobs = Mock()

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(sys.argv)
        cls.app.setQuitOnLastWindowClosed(False)

    def test_local_probe_requires_video_from_selected_stream(self):
        with patch("app.core.camera_stream.ensure_service", return_value=True), patch(
            "app.core.camera_stream.HTTPConnection"
        ) as connect:
            connection = connect.return_value
            response = connection.getresponse.return_value
            response.status = 200
            response.read.return_value = b"\x00\x00\x00\x18ftyp"
            self.assertTrue(probe_local_video("camera-2"))
            connection.request.assert_called_once_with("GET", "/api/frame.mp4?src=camera-2")
            connection.close.assert_called_once()

            response.read.return_value = b"<html>\r\n"
            self.assertFalse(probe_local_video("camera-2"))
            connection.getresponse.side_effect = TimeoutError
            self.assertFalse(probe_local_video("camera-2"))
            self.assertEqual(connection.close.call_count, 3)

    def test_relay_reconnects_with_fresh_authorization_and_switches_quality(self):
        attempts_file = _DATA_DIR / "relay-attempts.txt"
        attempts_file.write_text("")
        child = """
import sys
from pathlib import Path

attempts_file = Path(sys.argv[1])
attempts = len(attempts_file.read_text().splitlines())
fields = [sys.stdin.readline().rstrip() for _ in range(9)]
with attempts_file.open('a') as log:
    print(fields[7], fields[8], file=log)
print('PHASE RELAY', flush=True)
if attempts == 0:
    print('ERROR RELAY', flush=True)
elif attempts == 1:
    print('PHASE MEDIA', 'ERROR MEDIA', sep='\\n', flush=True)
else:
    print('READY', flush=True)
if sys.stdin.readline().strip() != 'STOP':
    sys.exit(1)
"""
        relay = CameraRelay(_DATA_DIR, lens=2)
        relay._process.setProgram(sys.executable)
        relay._process.setArguments(["-u", "-c", child, str(attempts_file)])
        loop = QEventLoop()
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        ready = []
        failures = []
        progress = []
        authorizations = []

        def authorize():
            signature = str(len(authorizations) + 1)
            authorizations.append(signature)
            relay.start(CameraRelayAuthorization("uid", "config", "client-pub", "client-priv", "camera-pub", signature))

        relay.retry.connect(authorize)
        relay.ready.connect(ready.append)
        relay.ready.connect(relay.stop)
        relay.failed.connect(failures.append)
        relay.progress.connect(progress.append)
        relay.destroyed.connect(loop.quit)
        deadline.start(10000)
        authorize()
        loop.exec()
        if not deadline.isActive():
            relay.stop()
            self.fail("中继测试进程超时")
        self.assertEqual(failures, [])
        self.assertEqual(len(ready), 1)
        self.assertEqual(attempts_file.read_text().splitlines(), ["1 1", "2 1", "3 2"])
        for attempt in (1, 2, 3):
            self.assertTrue(any(f"第 {attempt} 次" in message for message in progress))

    def test_remote_player_stops_after_repeated_load_failures(self):
        playback = CameraPlayback(self.device, 1, self.service, self.jobs)
        playback._url = QUrl("http://127.0.0.1:1234/test.ts")
        failures = []
        playback.failed.connect(failures.append)

        def replay(source):
            playback._url = source

        with patch.object(playback, "_play", side_effect=replay) as play:
            for _ in range(2):
                playback._schedule_retry()
                self.assertTrue(playback._retry_timer.isActive())
                playback._retry_timer.stop()
                playback._retry()
            playback._schedule_retry()
            self.assertEqual(play.call_count, 2)
        self.assertEqual(len(failures), 1)
        self.assertTrue(playback._url.isEmpty())
        self.assertFalse(playback._retry_timer.isActive())

    def test_playback_routes_locally_or_requests_authorization(self):
        local = CameraPlayback(self.device, 1, self.service, self.jobs)
        local.start()
        callback = self.jobs.submit.call_args.kwargs["on_success"]
        with patch.object(local, "_play") as play:
            callback(True)
            play.assert_called_once_with(QUrl("rtsp://127.0.0.1:8554/fixture"))
        local.stop()

        remote = CameraPlayback(self.device, 1, self.service, self.jobs)
        with patch("app.ui.camera_playback.configured_relay", return_value=_DATA_DIR):
            remote.start()
            self.jobs.submit.call_args.kwargs["on_success"](False)
            authorization_job = self.jobs.submit.call_args.args[0]
            authorization_callback = self.jobs.submit.call_args.kwargs["on_success"]
        authorization_job()
        self.service.authorize_camera_relay.assert_called_once_with("fixture")
        with patch.object(remote._relay, "start") as start:
            remote.stop()
            authorization_callback(Mock())
            start.assert_not_called()

    def test_stalled_relay_restarts_once_without_resetting_phase_deadline(self):
        child = """
import sys

for _ in range(9):
    sys.stdin.readline()
print('PHASE RELAY', 'RELAY DISCOVERY', 'RELAY REGISTER', sep='\\n', flush=True)
sys.stdin.readline()
"""
        relay = CameraRelay(_DATA_DIR, lens=2)
        relay._process.setProgram(sys.executable)
        relay._process.setArguments(["-u", "-c", child])
        authorization = CameraRelayAuthorization("uid", "config", "client-pub", "client-priv", "camera-pub", "sign")
        loop = QEventLoop()
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        stages = []
        failures = []

        def watch(message):
            if "第" in message and relay._phase == b"PHASE RELAY":
                stages.append((message, relay._timeout.interval()))

        relay.progress.connect(watch)
        relay.retry.connect(lambda: relay.start(authorization))
        relay.failed.connect(failures.append)
        relay.destroyed.connect(loop.quit)
        with patch("app.ui.camera_relay._RELAY_TIMEOUT_MS", 250), patch(
            "app.ui.camera_relay._RELAY_RETRY_TIMEOUT_MS", 400
        ):
            deadline.start(10000)
            relay.start(authorization)
            loop.exec()
        if not deadline.isActive():
            relay.stop()
            self.fail("中继测试进程超时")
        self.assertEqual([interval for _, interval in stages], [250] * 3 + [400] * 3)
        self.assertIn("等待中继握手应答", stages[-1][0])
        self.assertEqual(failures, ["重试后仍无法连接小米公网中继。"])

    def test_switching_lens_reuses_the_existing_relay_and_window(self):
        base = _DATA_DIR / "dual-lens"
        base.mkdir()
        (base / "camera-relay-dual-lens.jar").touch()
        relay = CameraRelay(base, dual_lens=True)
        relay._phase = b"READY"
        playback = CameraPlayback(self.device, 1, self.service, self.jobs)
        playback._has_frame = True
        playback._relay = relay
        window = CameraPipWindow(playback, "Test camera")
        _OPEN["fixture"] = window
        try:
            with patch.object(relay._process, "write", side_effect=len) as write, patch(
                "app.core.camera_stream.has_second_lens", return_value=True
            ):
                self.assertIs(open_camera_pip(self.device, 2, "镜头 2", self.service, self.jobs), window)
                self.assertFalse(relay.switch_lens(1))
            write.assert_called_once_with(f"SWITCH 2 {relay._url.port()} {relay._url.path()}\n".encode("ascii"))
            self.assertNotIn("fixture", _OPEN)
            self.assertIs(_OPEN["fixture-2"], window)
            self.assertIs(playback._relay, relay)
            self.assertTrue(window._image.isNull())
        finally:
            window.close()

    def test_clicking_again_replaces_a_failed_preview(self):
        playback = CameraPlayback(self.device, 2, self.service, self.jobs)
        window = CameraPipWindow(playback, "Test camera")
        _OPEN["fixture-2"] = window
        self.assertIs(show_existing_pip("fixture-2"), window)
        playback._fail("连接失败")
        self.assertIsNone(show_existing_pip("fixture-2"))
        self.assertNotIn("fixture-2", _OPEN)


if __name__ == "__main__":
    unittest.main()
