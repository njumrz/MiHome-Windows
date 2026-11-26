# SPDX-License-Identifier: GPL-3.0-or-later
"""根据累计接收字节计算实时速率。"""


class ReceiveRate:
    def __init__(self):
        self.clear()

    def clear(self):
        self._sample = None
        self._rate = None

    def record(self, total: int, now: float):
        if self._sample is not None:
            previous, timestamp = self._sample
            if now <= timestamp:
                return
            self._rate = (total - previous) / (now - timestamp) if total >= previous else None
        self._sample = total, now

    def current(self, now: float) -> float | None:
        if self._sample is not None and now - self._sample[1] > 3:
            return 0.0
        return self._rate
