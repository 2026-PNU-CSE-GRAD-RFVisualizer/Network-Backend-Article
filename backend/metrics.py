import time
from collections import deque
from threading import Lock
from typing import Any, Iterable

def now_ms() -> int:
    return int(time.time() * 1000)

class Metrics:
    """스레드 세이프 성능 지표 수집기.

    - 수집 지연(collect latency): 노드 수신 시각 -> 프레임 발행 시각 (ms)
    - 프레임 생성 시간(window flush): flush 루프 1회 처리 시간 (ms)
    - 카운터: 수신/드롭 메시지, 발행 프레임, 저장 행 수
    """

    def __init__(self, sample_size: int = 500) -> None:
        self._lock = Lock()
        self.messages_received = 0
        self.messages_dropped = 0
        self.frames_emitted = 0
        self.raw_rows_written = 0
        self._collect_latency: deque[float] = deque(maxlen=sample_size)
        self._flush_ms: deque[float] = deque(maxlen=sample_size)

    def inc_received(self) -> None:
        with self._lock:
            self.messages_received += 1

    def inc_dropped(self) -> None:
        with self._lock:
            self.messages_dropped += 1

    def inc_frames(self, n: int = 1) -> None:
        with self._lock:
            self.frames_emitted += n

    def add_rows(self, n: int) -> None:
        with self._lock:
            self.raw_rows_written += n

    def observe_collect_latency(self, ms: float) -> None:
        with self._lock:
            self._collect_latency.append(ms)

    def observe_flush_ms(self, ms: float) -> None:
        with self._lock:
            self._flush_ms.append(ms)

    @staticmethod
    def _stats(samples: Iterable[float]) -> dict[str, float]:
        ordered = sorted(samples)
        n = len(ordered)
        if n == 0:
            return {"count": 0, "avg": 0.0, "p95": 0.0, "max": 0.0}
        idx = min(n - 1, int(n * 0.95))
        return {
            "count": n,
            "avg": round(sum(ordered) / n, 2),
            "p95": round(ordered[idx], 2),
            "max": round(ordered[-1], 2),
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            loss_total = self.messages_received + self.messages_dropped
            drop_rate = round(self.messages_dropped / loss_total, 4) if loss_total else 0.0
            return {
                "messages_received": self.messages_received,
                "messages_dropped": self.messages_dropped,
                "drop_rate": drop_rate,
                "frames_emitted": self.frames_emitted,
                "raw_rows_written": self.raw_rows_written,
                "collect_latency_ms": self._stats(self._collect_latency),
                "window_flush_ms": self._stats(self._flush_ms),
            }
