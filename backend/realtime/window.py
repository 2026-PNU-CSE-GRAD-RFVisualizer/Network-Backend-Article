"""200ms Time Window 동기화 — 9월 졸업작품 실시간 시각화용.

논문 실험(7/23 강의실 측정)은 위치마다 30초 정지 측정이라 프레임 동기화가 필요 없다.
ENABLE_REALTIME=false 이면 이 모듈은 인스턴스화되지 않는다.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any


class WindowBuffer:
    def __init__(self, window_size_ms: int) -> None:
        self.window_size_ms = window_size_ms
        self.buckets: dict[int, dict[str, dict[str, Any]]] = defaultdict(dict)

    def add(self, measurement: dict[str, Any], receive_ms: int) -> None:
        bucket_ts = receive_ms - (receive_ms % self.window_size_ms)
        node_id = measurement["node_id"]
        current = self.buckets[bucket_ts].get(node_id)
        if current is None or receive_ms >= current["_receive_ms"]:
            item = dict(measurement)
            item["_receive_ms"] = receive_ms
            self.buckets[bucket_ts][node_id] = item

    def pop_ready(self, current_ms: int, known_nodes: set[str]) -> list[dict[str, Any]]:
        ready_before = current_ms - self.window_size_ms
        frames: list[dict[str, Any]] = []
        for bucket_ts in sorted(list(self.buckets)):
            if bucket_ts > ready_before:
                continue
            bucket = self.buckets.pop(bucket_ts)
            nodes = {
                node_id: {
                    "rssi": item["rssi"],
                    "ap_bssid": item.get("ap_bssid"),
                    "seq": item.get("seq"),
                    "node_ts": item.get("timestamp"),
                    "server_receive_ms": item["_receive_ms"],
                    "pos": {"x": item.get("pos_x"), "y": item.get("pos_y"), "z": item.get("pos_z")},
                    "rot": {"w": item.get("rot_w"), "x": item.get("rot_x"),
                            "y": item.get("rot_y"), "z": item.get("rot_z")},
                }
                for node_id, item in sorted(bucket.items())
            }
            frames.append(
                {
                    "type": "rssi_frame",
                    "window_ts": bucket_ts,
                    "window_size_ms": self.window_size_ms,
                    "nodes": nodes,
                    "missing": sorted(known_nodes - set(nodes)),
                }
            )
        return frames
