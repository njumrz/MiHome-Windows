# SPDX-License-Identifier: GPL-3.0-or-later
"""局域网视频探测和公网中继重连的离线回归测试。"""

import os
import sys
import unittest
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock, patch

from tests._isolate import isolate

_DATA_DIR = Path(isolate())
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer, QUrl
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from app.core.camera_stream import probe_local_pair, probe_local_video
from app.core.camera_rate import ReceiveRate
from app.core.models import CameraRelayAuthorization, DeviceInfo
from app.ui.camera_pip import CameraPipWindow, _OPEN, open_camera_pip, show_existing_pip
from app.ui.camera_playback import CameraPlayback, start_camera_playbacks
from app.ui.camera_relay import CameraRelay
from app.ui.camera_view import CameraView


class CameraPlaybackTest(unittest.TestCase):
    def setUp(self):
        self.device = DeviceInfo("fixture", "Test camera", "test.camera.example", "Test home", "Test room", True)
        self.service = Mock()
        self.jobs = Mock()

    def test_receive_rate_handles_stall_and_counter_reset(self):
        rate = ReceiveRate()
        rate.record(1000, 1)
        self.assertIsNone(rate.current(1))
        rate.record(3500, 2)
        self.assertEqual(rate.current(2), 2500)
        self.assertEqual(rate.current(6), 0)
        rate.record(100, 7)
        self.assertIsNone(rate.current(7))
        rate.record(300, 9)
        self.assertEqual(rate.current(9), 100)

    def test_quality_change_uses_the_same_relay_and_handles_rejection(self):
        base = _DATA_DIR / "quality"
        base.mkdir()
        (base / "camera-relay-dual-lens-v4.jar").touch()
        relay = CameraRelay(base, lens=2)
        relay._phase = b"READY"
        outcomes = []
        relay.quality_changed.connect(lambda quality, accepted: outcomes.append((quality, accepted)))
        try:
            for response in (b"QUALITY REJECTED", b"QUALITY ACCEPTED"):
                with patch.object(relay._process, "write", side_effect=len) as write:
                    self.assertTrue(relay.set_quality(2))
                    write.assert_called_once_with(f"QUALITY 2 {relay._url.port()} {relay._url.path()}\n".encode("ascii"))
                with patch.object(relay._process, "canReadLine", side_effect=(True, True, False)), patch.object(
                    relay._process, "readLine", side_effect=(response, b"READY")
                ):
                    relay._read_status()
            self.assertEqual(outcomes, [(1, False), (2, True)])
        finally:
            relay.stop()

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

    def test_paired_local_probes_overlap_and_allow_one_available_feed(self):
        running = Barrier(2, timeout=2)

        def probe(name):
            running.wait()
            return name == "first"

        with patch("app.core.camera_stream.ensure_service", return_value=True) as ensure, \
             patch("app.core.camera_stream._probe_local_frame", side_effect=probe):
            self.assertEqual(probe_local_pair("first", "second"), (True, False))
        ensure.assert_called_once()

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
    print('RELAY REGISTER', 'RELAY ROUND 1 12500 5 3 1 5 20 0 0 0', sep='\\n', flush=True)
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
        with self.assertLogs("app.ui.camera_relay", level="INFO") as logs:
            authorize()
            loop.exec()
        if not deadline.isActive():
            relay.stop()
            self.fail("中继测试进程超时")
        self.assertEqual(failures, [])
        self.assertEqual(len(ready), 1)
        self.assertEqual(attempts_file.read_text().splitlines(), ["1 1", "2 1", "3 2"])
        self.assertIn("hello_sent=20 hello_ack=0", "\n".join(logs.output))
        self.assertIn("reason=no_hello_ack retry=True", "\n".join(logs.output))
        for attempt in (1, 2, 3):
            self.assertTrue(any(f"第 {attempt} 次" in message for message in progress))

    def test_local_player_stops_after_repeated_load_failures(self):
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

    def test_remote_player_monitors_decoded_frames_and_requests_recovery(self):
        playback = CameraPlayback(self.device, 2, self.service, self.jobs)
        playback._relay = Mock()
        playback._player = Mock()
        image = QImage(16, 16, QImage.Format.Format_RGB32)
        frame = Mock()
        frame.toImage.return_value = image
        url = QUrl("http://127.0.0.1:1234/test.ts")
        with patch.object(playback, "_apply_mute"), patch(
            "app.ui.camera_playback.monotonic", side_effect=(-1, 0, 10, 21, 22)
        ):
            playback._play(url)
            self.assertEqual(playback._video_timer.interval(), 15000)
            playback._video_timer.timeout.emit()
            playback._relay.recover_video.assert_called_once()
            for _ in range(4):
                playback._on_frame(frame)
            self.assertEqual(playback._video_timer.interval(), 12000)
            playback._relay.video_stable.assert_called_once()
            playback._video_timer.timeout.emit()
            self.assertEqual(playback._relay.recover_video.call_count, 2)
            playback._stop_player()
            playback._video_timer.timeout.emit()
        self.assertEqual(playback._relay.recover_video.call_count, 2)
        self.assertFalse(playback._video_timer.isActive())

    def test_relay_recovers_once_after_video_loss_with_a_new_authorization(self):
        child = """
import sys

fields = [sys.stdin.readline().rstrip() for _ in range(9)]
print('READY', flush=True)
if sys.stdin.readline().strip() == 'DROP':
    if fields[7] == '1':
        sys.exit(0)
    print('ERROR STREAM', flush=True)
sys.stdin.readline()
"""
        relay = CameraRelay(_DATA_DIR, lens=2)
        relay._process.setProgram(sys.executable)
        relay._process.setArguments(["-u", "-c", child])
        loop = QEventLoop()
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        authorizations = []
        urls = []
        failures = []
        recovering = Mock()

        def authorize():
            signature = str(len(authorizations) + 1)
            authorizations.append(signature)
            relay.start(CameraRelayAuthorization("uid", "config", "client-pub", "client-priv", "camera-pub", signature))

        def drop_stream(url):
            urls.append(url)
            relay._process.write(b"DROP\n")

        relay.retry.connect(authorize)
        relay.ready.connect(drop_stream)
        relay.recovering.connect(recovering)
        relay.failed.connect(failures.append)
        relay.destroyed.connect(loop.quit)
        deadline.start(10000)
        authorize()
        loop.exec()
        if not deadline.isActive():
            relay.stop()
            self.fail("中继测试进程超时")
        self.assertEqual(authorizations, ["1", "2"])
        self.assertEqual(len(set(urls)), 2)
        recovering.assert_called_once()
        self.assertEqual(failures, ["公网视频重连后仍然断开，请重试。"])

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
        with self.assertLogs("app.ui.camera_relay", level="INFO") as logs:
            authorization_job()
        self.service.authorize_camera_relay.assert_called_once_with("fixture")
        self.assertRegex(logs.output[0], r"queue_ms=\d+ call_ms=\d+")
        self.assertNotIn("fixture", logs.output[0])
        with patch.object(remote._relay, "start") as start:
            remote.stop()
            authorization_callback(Mock())
            start.assert_not_called()

    def test_two_public_feeds_probe_then_share_one_authorization_and_publish_separately(self):
        base = _DATA_DIR / "paired"
        base.mkdir()
        (base / "camera-relay-dual-lens-v10.jar").touch()
        first, second = [CameraPlayback(self.device, lens, self.service, self.jobs) for lens in (1, 2)]
        with patch("app.ui.camera_playback.configured_relay", return_value=base):
            start_camera_playbacks([first, second])
            self.assertEqual(self.jobs.submit.call_count, 1)
            self.jobs.submit.call_args.kwargs["on_success"]((False, False))
        relay = first._relay._relay
        try:
            self.assertIs(relay, second._relay._relay)
            self.assertIn("--paired", relay._process.arguments())
            self.assertEqual(self.jobs.submit.call_count, 2)
            self.jobs.submit.call_args.args[0]()
            self.service.authorize_camera_relay.assert_called_once_with("fixture")
            grant = CameraRelayAuthorization("uid", "config", "client-pub", "client-priv", "camera-pub", "sign")
            with patch.object(relay._process, "start"):
                self.jobs.submit.call_args.kwargs["on_success"](grant)
            self.assertNotEqual(first._relay.url.port(), second._relay.url.port())
            self.assertEqual(len(relay._payload.splitlines()), 12)

            ready = []
            first._relay.ready.connect(lambda url: ready.append((1, url)))
            second._relay.ready.connect(lambda url: ready.append((2, url)))
            relay._phase = b"PHASE MEDIA"
            first._player = Mock()
            second._player = Mock()
            with self.assertLogs("app.ui.camera_relay", level="INFO") as logs:
                relay._read_feed(b"FEED READY 2")
            self.assertTrue(any("lens=1,2" in line and "event=video_keyframe" in line for line in logs.output))
            self.assertTrue(any("lens=2" in line and "event=feed_ready" in line for line in logs.output))
            self.assertEqual(ready, [(2, second._relay.url)])
            relay._read_feed(b"FEED READY 1")
            self.assertEqual(ready[-1], (1, first._relay.url))

            relay._read_feed(b"FEED FAILED 2")
            self.assertTrue(first.is_active)
            self.assertFalse(second.is_active)
            self.assertFalse(relay._closed)
            replacement = CameraPlayback(self.device, 2, self.service, self.jobs)
            with patch.object(relay._process, "write", side_effect=len) as write:
                self.assertTrue(replacement.resume_shared_feed(first))
            self.assertTrue(write.call_args.args[0].startswith(b"FEED 2 1 "))
            self.assertEqual(self.jobs.submit.call_count, 2)
            replacement.stop()
        finally:
            first.stop()
            second.stop()
        self.assertTrue(relay._closed)

    def test_v10_logs_the_winning_route_without_renewing_before_other_routes_finish(self):
        base = _DATA_DIR / "race"
        base.mkdir()
        (base / "camera-relay-dual-lens-v10.jar").touch()
        relay = CameraRelay(base, paired=True, model="xiaomi.camera.082ac1", preferred_route="p2p")
        self.addCleanup(relay.stop)
        self.assertIn("--prefer-p2p", relay._process.arguments())
        self.assertIn("--channel-index", relay._process.arguments())
        self.assertEqual(dict(relay.quality_options)[3], "3K")
        statuses = iter((b"PHASE PUBLIC", b"ROUTE START RELAY", b"ROUTE START P2P", b"RELAY REGISTER",
                         b"RELAY ROUND 1 2700 1 3 1 1 4 0 0 0", b"ROUTE SELECT P2P", b"PHASE AUTH"))
        with patch.object(relay._process, "canReadLine", side_effect=[True] * 7 + [False]), \
             patch.object(relay._process, "readLine", side_effect=statuses), \
             patch.object(relay, "_retry_connection") as retry, \
             self.assertLogs("app.ui.camera_relay", level="INFO") as logs:
            relay._read_status()
        retry.assert_not_called()
        self.assertEqual(relay.transport, "p2p")
        self.assertTrue(any("transport=p2p event=route_selected" in line for line in logs.output))
        relay._phase = b"READY"
        with patch.object(relay._process, "write", side_effect=len) as write:
            self.assertFalse(relay.feeds[2].set_quality(2))
            self.assertTrue(relay.feeds[2].set_quality(3))
        self.assertTrue(write.call_args.args[0].startswith(b"FEED 2 3 "))

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

    def test_relay_ack_keeps_current_socket_with_a_bounded_deadline(self):
        relay = CameraRelay(_DATA_DIR, lens=2)
        relay._attempt_started_at = relay._phase_started_at = 100.0
        statuses = iter((b"PHASE RELAY", b"RELAY REGISTER", b"RELAY PORT", b"RELAY CAMERA", b"RELAY DISCOVERY",
                         b"RELAY ROUND 2 12000 2 3 0 2 8 0 0 0", b"RELAY ROUND 3 12500 1 3 0 1 4 private 0 0"))
        try:
            with patch("app.ui.camera_relay.monotonic", side_effect=(100.0, 112.0)), patch.object(
                relay._process, "canReadLine", side_effect=[True] * 7 + [False]
            ), patch.object(relay._process, "readLine", side_effect=statuses), patch.object(
                relay, "_retry_connection"
            ) as retry, self.assertLogs("app.ui.camera_relay", level="INFO") as logs:
                relay._read_status()
            retry.assert_not_called()
            self.assertEqual(relay._timeout.interval(), 36_000)
            self.assertEqual(sum("公网握手" in row for row in logs.output), 1)
            self.assertNotIn("private", "\n".join(logs.output))
        finally:
            relay.stop()

    def test_direct_handshake_without_video_reauthorizes_and_uses_relay_only(self):
        child = """
import sys

fields = [sys.stdin.readline().rstrip() for _ in range(9)]
if '--relay-only' not in sys.argv:
    print('PHASE DIRECT', 'DIRECT STATS 9 2 2 4 200', 'DIRECT CONNECTED', 'PHASE AUTH',
          'PHASE MEDIA', 'ERROR MEDIA', sep='\\n', flush=True)
elif fields[7] == 'second':
    print('PHASE RELAY', 'PHASE AUTH', 'PHASE MEDIA', 'READY', sep='\\n', flush=True)
sys.stdin.readline()
"""
        relay = CameraRelay(_DATA_DIR, lens=2)
        relay._process.setProgram(sys.executable)
        relay._process.setArguments(["-u", "-c", child])
        loop = QEventLoop()
        deadline = QTimer()
        deadline.setSingleShot(True)
        deadline.timeout.connect(loop.quit)
        ready = []
        failures = []

        def authorize(signature):
            relay.start(CameraRelayAuthorization("uid", "config", "client-pub", "client-priv", "camera-pub", signature))

        relay.retry.connect(lambda: authorize("second"))
        relay.ready.connect(lambda _url: ready.append((relay.transport, relay.quality)))
        relay.ready.connect(relay.stop)
        relay.failed.connect(failures.append)
        relay.destroyed.connect(loop.quit)
        deadline.start(10000)
        with self.assertLogs("app.ui.camera_relay", level="INFO") as logs:
            authorize("first")
            loop.exec()
        if not deadline.isActive():
            relay.stop()
            self.fail("直连回退测试进程超时")
        self.assertEqual(failures, [])
        self.assertEqual(ready, [("relay", 1)])
        self.assertIn("transport=p2p event=direct_connected", "\n".join(logs.output))
        self.assertIn("reason=ERROR MEDIA", "\n".join(logs.output))

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

    def test_details_and_floating_window_reuse_one_playback_in_both_directions(self):
        view = CameraView(self.device, self.service, self.jobs)
        view.start()
        tile = view.tiles[0]
        playback = tile.playback
        window = open_camera_pip(self.device, 1, "Test camera", self.service, self.jobs, playback=playback)
        try:
            self.assertIs(window._playback, playback)
            self.assertTrue(playback.shared)
            self.jobs.submit.assert_called_once()
            view.stop()
            self.assertTrue(playback.is_active)
            self.assertIs(playback.parent(), window)
            view.start()
            self.assertIs(tile.playback, playback)
            self.jobs.submit.assert_called_once()
            window.close()
            self.assertTrue(playback.is_active)
            self.assertIs(playback.parent(), tile)
        finally:
            if window._playback is not None:
                window.close()
            view.stop()
        self.assertFalse(playback.is_active)

    def test_selecting_another_embedded_lens_moves_the_existing_floating_window(self):
        with patch("app.ui.camera_view.has_second_lens", return_value=True), patch(
            "app.core.camera_stream.has_second_lens", return_value=True
        ):
            view = CameraView(self.device, self.service, self.jobs)
            view.start()
            first, second = [tile.playback for tile in view.tiles]
            window = open_camera_pip(self.device, 1, "Lens 1", self.service, self.jobs, playback=first)
            try:
                self.assertIs(open_camera_pip(self.device, 2, "Lens 2", self.service, self.jobs, playback=second), window)
                self.assertNotIn("fixture", _OPEN)
                self.assertIs(window._playback, second)
                self.assertTrue(first.is_active)
                self.assertTrue(second.is_active)
                self.assertEqual(self.jobs.submit.call_count, 2)
            finally:
                window.close()
                view.stop()


if __name__ == "__main__":
    unittest.main()
