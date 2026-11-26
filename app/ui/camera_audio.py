# SPDX-License-Identifier: GPL-3.0-or-later
"""播放公网中继返回的单声道 PCM。"""

from array import array
from struct import pack

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtMultimedia import QAudio, QAudioFormat, QAudioSink, QAudioSource, QMediaDevices


def convert_pcm(data: bytes, source_rate: int, output_rate: int, channels: int, floating: bool) -> bytes:
    samples = array("h")
    samples.frombytes(data)
    count = len(samples) * output_rate // source_rate
    output = array("f" if floating else "h")
    for index in range(count):
        value = samples[index * source_rate // output_rate]
        output.extend([value / 32768 if floating else value] * channels)
    return output.tobytes()


class CameraAudioOutput(QObject):
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._sink = None
        self._output = None
        self._source_rate = None
        self._format = None
        self._opus = None
        self._resampler = None

    def write(self, codec: str, sample_rate: int, data: bytes) -> None:
        if codec == "opus":
            try:
                data = self._decode_opus(data)
            except (ImportError, ValueError, RuntimeError, OSError):
                self.failed.emit("无法解码摄像头音频。")
                return
        if not data:
            return
        if self._source_rate != sample_rate:
            self._stop_sink()
            if not self._start(sample_rate):
                self.failed.emit("无法打开音频输出设备。")
                return
        audio_format = self._format
        if (audio_format.sampleRate() != sample_rate or audio_format.channelCount() != 1
                or audio_format.sampleFormat() != QAudioFormat.SampleFormat.Int16):
            data = convert_pcm(data, sample_rate, audio_format.sampleRate(), audio_format.channelCount(),
                               audio_format.sampleFormat() == QAudioFormat.SampleFormat.Float)
        if len(data) <= self._sink.bytesFree():
            self._output.write(data)

    def _decode_opus(self, data: bytes) -> bytes:
        import av

        if self._opus is None:
            self._opus = av.CodecContext.create("opus", "r")
            self._opus.extradata = b"OpusHead" + pack("<BBHIhB", 1, 1, 0, 48000, 0, 0)
            self._resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
        return b"".join(bytes(out.planes[0])[:out.samples * 2]
                        for frame in self._opus.decode(av.Packet(data))
                        for out in self._resampler.resample(frame))

    def _start(self, sample_rate: int) -> bool:
        device = QMediaDevices.defaultAudioOutput()
        if device.isNull():
            return False
        audio_format = QAudioFormat()
        audio_format.setSampleRate(sample_rate)
        audio_format.setChannelCount(1)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not device.isFormatSupported(audio_format):
            audio_format = device.preferredFormat()
            if audio_format.sampleFormat() not in (QAudioFormat.SampleFormat.Float, QAudioFormat.SampleFormat.Int16):
                return False
        sink = QAudioSink(device, audio_format, self)
        sink.setBufferSize(audio_format.bytesForDuration(160_000))
        output = sink.start()
        if output is None:
            sink.deleteLater()
            return False
        self._sink = sink
        self._output = output
        self._source_rate = sample_rate
        self._format = audio_format
        return True

    def stop(self) -> None:
        self._stop_sink()
        self._opus = self._resampler = None

    def _stop_sink(self) -> None:
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
        self._sink = self._output = self._source_rate = self._format = None


class MicrophoneEncoder:
    def __init__(self, codec: str, rate: int, channels: int, floating: bool):
        import av

        encoders = {"opus": "libopus", "pcma": "pcm_alaw", "pcmu": "pcm_mulaw"}
        if codec not in encoders:
            raise ValueError("Unsupported microphone codec")
        self._av = av
        self._input_rate = rate
        self._input_layout = "mono" if channels == 1 else "stereo"
        self._input_format = "flt" if floating else "s16"
        self._stride = channels * (4 if floating else 2)
        output_rate = 48000 if codec == "opus" else 8000
        self._packet_samples = 960 if codec == "opus" else 320
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=output_rate)
        self._fifo = av.AudioFifo()
        self._encoder = av.CodecContext.create(encoders[codec], "w")
        self._encoder.format = "s16"
        self._encoder.layout = "mono"
        self._encoder.sample_rate = output_rate
        self._pending = bytearray()

    def encode(self, data: bytes) -> list[bytes]:
        self._pending.extend(data)
        samples = len(self._pending) // self._stride
        if not samples:
            return []
        length = samples * self._stride
        frame = self._av.AudioFrame(format=self._input_format, layout=self._input_layout, samples=samples)
        frame.sample_rate = self._input_rate
        plane = frame.planes[0]
        plane.update(bytes(self._pending[:length]).ljust(plane.buffer_size, b"\x00"))
        del self._pending[:length]
        for output in self._resampler.resample(frame):
            self._fifo.write(output)
        packets = []
        while self._fifo.samples >= self._packet_samples:
            packets.extend(bytes(packet) for packet in self._encoder.encode(self._fifo.read(self._packet_samples)))
        return packets


class CameraMicrophone(QObject):
    packet = Signal(bytes)
    failed = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source = None
        self._input = None
        self._encoder = None

    def start(self, codec: str) -> bool:
        self.stop()
        device = QMediaDevices.defaultAudioInput()
        if device.isNull():
            return False
        audio_format = QAudioFormat()
        audio_format.setSampleRate(48000)
        audio_format.setChannelCount(1)
        audio_format.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        if not device.isFormatSupported(audio_format):
            audio_format = device.preferredFormat()
        if (audio_format.channelCount() not in (1, 2)
                or audio_format.sampleFormat() not in (QAudioFormat.SampleFormat.Float, QAudioFormat.SampleFormat.Int16)):
            return False
        try:
            encoder = MicrophoneEncoder(codec, audio_format.sampleRate(), audio_format.channelCount(),
                                        audio_format.sampleFormat() == QAudioFormat.SampleFormat.Float)
        except (ImportError, ValueError, RuntimeError, OSError):
            return False
        source = QAudioSource(device, audio_format, self)
        source.setBufferSize(audio_format.bytesForDuration(160_000))
        audio_input = source.start()
        if audio_input is None or source.error() != QAudio.Error.NoError:
            source.stop()
            source.deleteLater()
            return False
        self._source, self._input, self._encoder = source, audio_input, encoder
        source.stateChanged.connect(self._source_state_changed)
        audio_input.readyRead.connect(self._read)
        QTimer.singleShot(0, self._read)
        return True

    def _read(self) -> None:
        if self._input is None:
            return
        try:
            for packet in self._encoder.encode(bytes(self._input.readAll())):
                self.packet.emit(packet)
        except (ValueError, RuntimeError, OSError):
            self.failed.emit("麦克风音频编码失败。")

    def _source_state_changed(self, state) -> None:
        if self._source is not None and state == QAudio.State.StoppedState:
            self.failed.emit("麦克风已停止。")

    def stop(self) -> None:
        source, self._source = self._source, None
        self._input = self._encoder = None
        if source is not None:
            source.stop()
            source.deleteLater()
