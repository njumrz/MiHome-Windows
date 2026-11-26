# SPDX-License-Identifier: GPL-3.0-or-later
"""播放公网中继返回的单声道 PCM。"""

from array import array
from struct import pack

from PySide6.QtCore import QObject, Signal
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices


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
