"""endurance_monitor 검증 — build_row 순수 로직 + mock HTTP 폴링.

    python tests/test_endurance_monitor.py
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import endurance_monitor as em  # noqa: E402

SAMPLE_METRICS = {
    "messages_received": 5000, "messages_dropped": 12, "drop_rate": 0.0024,
    "frames_emitted": 990, "raw_rows_written": 5000,
    "collect_latency_ms": {"count": 500, "avg": 40.0, "p95": 244.0, "max": 610.0},
}
SAMPLE_NODES = [
    {"node_id": "node-01", "online": True, "msg_rate_hz": 1.0, "lost_packets": 3},
    {"node_id": "node-02", "online": True, "msg_rate_hz": 0.9, "lost_packets": 7},
    {"node_id": "node-03", "online": False, "msg_rate_hz": 0.0, "lost_packets": 1},
]
SAMPLE_HEALTH = {
    "mqtt": {"connected": True, "connect_count": 2, "disconnect_count": 1},
}


def test_build_row_aggregates():
    row = em.build_row(120, SAMPLE_METRICS, SAMPLE_NODES, SAMPLE_HEALTH)
    assert row["elapsed_s"] == 120
    assert row["ok"] == 1
    assert row["drop_rate"] == 0.0024
    assert row["collect_latency_p95_ms"] == 244.0
    assert row["nodes_total"] == 3
    assert row["nodes_online"] == 2
    assert row["total_lost_packets"] == 11         # 3+7+1
    assert row["min_msg_rate_hz"] == 0.0           # offline 노드 포함 최저
    assert row["mqtt_disconnect_count"] == 1        # 재연결 지표
    assert set(row) == set(em.FIELDS)               # 컬럼 누락/초과 없음


def test_build_row_handles_empty_on_fetch_fail():
    row = em.build_row(30, {}, [], {}, ok=False)
    assert row["ok"] == 0
    assert row["nodes_total"] == 0
    assert set(row) == set(em.FIELDS)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 조용히
        pass

    def do_GET(self):
        body = {
            "/metrics": SAMPLE_METRICS,
            "/nodes/status": {"nodes": SAMPLE_NODES},
            "/health": SAMPLE_HEALTH,
        }.get(self.path, {})
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def test_poll_once_against_mock_server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        row = em.poll_once(base, elapsed_s=10)
        assert row["ok"] == 1
        assert row["messages_received"] == 5000
        assert row["total_lost_packets"] == 11
        assert row["collect_latency_p95_ms"] == 244.0
    finally:
        srv.shutdown()


def test_poll_once_records_failure_when_server_down():
    row = em.poll_once("http://127.0.0.1:1", elapsed_s=5)  # 열려 있지 않은 포트
    assert row["ok"] == 0
    assert "fetch 실패" in row["timestamp"]


if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
