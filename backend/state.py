import time
from collections import defaultdict, deque
from typing import Any

def now_ms() -> int:
    return int(time.time() * 1000)

class NodeRegistry:
    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_ms = int(timeout_seconds * 1000)
        self.nodes: dict[str, dict[str, Any]] = {}
        self.last_seq: dict[str, int] = {}
        self.receive_times: dict[str, deque[int]] = defaultdict(lambda: deque(maxlen=200))

    def mark_online(self, node_id: str, online: bool) -> dict[str, Any]:
        status = self._ensure(node_id)
        status["online"] = online
        if online:
            status["last_seen"] = now_ms()
        return status.copy()

    def mark_seen(self, node_id: str, seq: int | None = None) -> tuple[dict[str, Any], bool]:
        """측정값 수신 처리. (status_copy, became_online) 반환.

        became_online: 직전 offline/미등록 -> online 으로 전환된 경우 True.
        """
        status = self._ensure(node_id)
        was_online = status["online"]
        current_ms = now_ms()
        status["online"] = True
        status["last_seen"] = current_ms
        self.receive_times[node_id].append(current_ms)
        status["msg_rate_hz"] = self._rate_hz(node_id, current_ms)

        if seq is not None:
            prev = self.last_seq.get(node_id)
            if prev is not None and seq > prev + 1:
                status["lost_packets"] += seq - prev - 1
            if prev is None or seq > prev:
                self.last_seq[node_id] = seq

        return status.copy(), (not was_online)

    def expire_offline(self) -> list[dict[str, Any]]:
        current_ms = now_ms()
        changed: list[dict[str, Any]] = []
        for status in self.nodes.values():
            last_seen = status.get("last_seen")
            if last_seen is not None and current_ms - last_seen > self.timeout_ms:
                if status["online"]:
                    status["online"] = False
                    changed.append(status.copy())
        return changed

    def snapshot(self) -> list[dict[str, Any]]:
        current_ms = now_ms()
        result = []
        for node_id, status in self.nodes.items():
            item = status.copy()
            item["msg_rate_hz"] = self._rate_hz(node_id, current_ms)
            result.append(item)
        return sorted(result, key=lambda item: item["node_id"])

    def known_nodes(self) -> set[str]:
        return set(self.nodes)

    def _ensure(self, node_id: str) -> dict[str, Any]:
        if node_id not in self.nodes:
            self.nodes[node_id] = {
                "node_id": node_id,
                "online": False,
                "last_seen": None,
                "msg_rate_hz": 0.0,
                "lost_packets": 0,
            }
        return self.nodes[node_id]

    def _rate_hz(self, node_id: str, current_ms: int) -> float:
        samples = self.receive_times[node_id]
        while samples and current_ms - samples[0] > 5000:
            samples.popleft()
        if len(samples) < 2:
            return 0.0
        elapsed_s = max((samples[-1] - samples[0]) / 1000, 0.001)
        return round((len(samples) - 1) / elapsed_s, 3)
