# SPDX-License-Identifier: GPL-3.0-or-later
"""使用合成音频验证监听解码和输出控制。"""

from array import array
from base64 import b64encode
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from tests._isolate import isolate

_DATA_DIR = Path(isolate())

import av
from PySide6.QtCore import QCoreApplication

from app.core.models import DeviceInfo
from app.ui.camera_audio import CameraAudioOutput, MicrophoneEncoder, convert_pcm
from app.ui.camera_playback import CameraPlayback
from app.ui.camera_relay import CameraRelay


class CameraAudioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def test_pcm_conversion_to_device_format(self):
        output = array("f")
        output.frombytes(convert_pcm(array("h", [8192, -8192]).tobytes(), 8000, 16000, 2, True))
        self.assertEqual(output.tolist(), [0.25] * 4 + [-0.25] * 4)

    def test_decodes_an_in_memory_opus_packet(self):
        encoder = av.CodecContext.create("libopus", "w")
        encoder.format = "s16"
        encoder.layout = "mono"
        encoder.sample_rate = 48000
        frame = av.AudioFrame(format="s16", layout="mono", samples=960)
        frame.sample_rate = 48000
        frame.planes[0].update(array("h", [2000, -2000] * 480).tobytes())
        packets = encoder.encode(frame) + encoder.encode(None)
        output = CameraAudioOutput()
        pcm = output._decode_opus(bytes(packets[0]))
        self.assertGreater(len(pcm), 0)
        self.assertEqual(len(pcm) % 2, 0)
        output.stop()
        self.assertIsNone(output._opus)

    def test_relay_delivers_audio_only_while_enabled(self):
        relay = CameraRelay(_DATA_DIR)
        relay._phase = b"READY"
        frames = []
        relay.audio_frame.connect(lambda *args: frames.append(args))
        pcm = b"\x01\x00\x02\x00"
        pcm_line = b"AUDIO 8000 " + b64encode(pcm)
        opus_line = b"AUDIO OPUS " + b64encode(b"test")
        relay._read_audio(pcm_line)
        with patch.object(relay._process, "write", side_effect=len):
            relay.set_audio(True)
            relay._read_audio(pcm_line)
            relay._read_audio(opus_line)
            relay._read_audio(b"AUDIO 8000 !@#")
            relay.set_audio(False)
        relay._read_audio(pcm_line)
        self.assertEqual(frames, [("pcm", 8000, pcm), ("opus", 48000, b"test")])
        relay.stop()

    def test_encodes_synthetic_mic_audio_for_supported_speaker_formats(self):
        for codec in ("pcma", "pcmu", "opus"):
            with self.subTest(codec=codec):
                encoder = MicrophoneEncoder(codec, 48000, 2, True)
                stereo = array("f", [0.05, 0.05] * 4800).tobytes()
                packets = encoder.encode(stereo[:13]) + encoder.encode(stereo[13:])
                self.assertTrue(packets)
                if codec != "opus":
                    self.assertTrue(all(len(packet) == 320 for packet in packets))
                else:
                    self.assertTrue(CameraAudioOutput()._decode_opus(packets[0]))

    def test_microphone_does_not_open_before_talk_ready_and_stops_on_close(self):
        device = DeviceInfo("fixture", "Test camera", "test.camera.example", "Test home", "Test room", True)
        playback = CameraPlayback(device, 1, Mock(), Mock())
        playback._relay = CameraRelay(_DATA_DIR)
        playback._relay.supports_talk = True
        playback._has_frame = True
        playback._microphone = Mock()
        playback._on_talk_ready("opus")
        playback._microphone.start.assert_not_called()
        with patch.object(playback._relay, "start_talk", return_value=True):
            self.assertTrue(playback.start_talk())
            playback._microphone.start.assert_not_called()
        playback._on_talk_ready("opus")
        playback._microphone.start.assert_called_once_with("opus")
        playback.stop()
        playback._microphone.stop.assert_called_once()
        playback._on_talk_ready("opus")
        playback._microphone.start.assert_called_once_with("opus")

    def test_relay_only_sends_packets_during_an_active_talk(self):
        relay = CameraRelay(_DATA_DIR)
        relay._phase = b"READY"
        relay.supports_talk = True
        packets = b"synthetic"
        with patch.object(relay._process, "write", side_effect=len) as write:
            relay.send_talk_audio(packets)
            write.assert_not_called()
            relay.set_audio(True)
            self.assertTrue(relay.start_talk())
            relay.send_talk_audio(packets)
            self.assertEqual(write.call_count, 2)
            with patch.object(relay._process, "canReadLine", side_effect=(True, False)), patch.object(
                relay._process, "readLine", return_value=b"TALK OPUS"
            ):
                relay._read_status()
            relay.send_talk_audio(packets)
            write.assert_called_with(b"TALK DATA " + b64encode(packets) + b"\n")
            relay.stop_talk()
            relay.send_talk_audio(packets)
            write.assert_called_with(b"TALK STOP\n")
        relay.stop()


if __name__ == "__main__":
    unittest.main()
