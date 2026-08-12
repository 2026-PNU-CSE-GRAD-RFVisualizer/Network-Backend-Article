"""NodeRegistry(논문 실험에서도 사용) + WindowBuffer(9월 졸업작품 범위) 단위 테스트."""

import time

from backend.realtime import WindowBuffer
from backend.state import NodeRegistry, now_ms


def test_window_not_ready_immediately():
    wb = WindowBuffer(200)
    base = now_ms()
    wb.add({"node_id": "n1", "rssi": -54, "seq": 1, "timestamp": base}, base)
    assert wb.pop_ready(base, {"n1"}) == []


def test_window_emits_after_window():
    # 새 WindowBuffer 는 측정시각 버킷 + 유예(grace)+정지대기(stall) 후 확정한다.
    # 확정 시각 = bucket + window + grace + stall = bucket + 600. 여유 있게 +700 에서 확인.
    wb = WindowBuffer(200)
    b = (now_ms() // 200) * 200
    wb.add({"node_id": "n1", "rssi": -54, "seq": 1, "timestamp": b}, b)
    frames = wb.pop_ready(b + 700, {"n1"})
    assert len(frames) == 1
    assert list(frames[0]["nodes"]) == ["n1"]
    assert frames[0]["missing"] == []


def test_window_marks_missing_nodes():
    wb = WindowBuffer(200)
    b = (now_ms() // 200) * 200
    wb.add({"node_id": "A", "rssi": -50, "seq": 1, "timestamp": b}, b + 10)
    wb.add({"node_id": "B", "rssi": -60, "seq": 1, "timestamp": b}, b + 20)
    frames = wb.pop_ready(b + 700, {"A", "B", "C"})
    assert len(frames) == 1
    assert sorted(frames[0]["nodes"]) == ["A", "B"]
    assert frames[0]["missing"] == ["C"]


def test_packet_loss_counting():
    reg = NodeRegistry(5.0)
    reg.mark_seen("A", 1)
    reg.mark_seen("A", 2)
    reg.mark_seen("A", 5)  # 3,4 손실
    status, _ = reg.mark_seen("A", 6)
    assert status["lost_packets"] == 2
    reg.mark_seen("A", 4)  # 과거 seq 무시
    snap = [n for n in reg.snapshot() if n["node_id"] == "A"][0]
    assert snap["lost_packets"] == 2


def test_mark_seen_transition_flag():
    reg = NodeRegistry(5.0)
    _, became = reg.mark_seen("A", 1)
    assert became is True  # 신규 -> online
    _, became2 = reg.mark_seen("A", 2)
    assert became2 is False  # 계속 online


def test_heartbeat_timeout():
    reg = NodeRegistry(0.001)
    reg.mark_seen("A", 1)
    time.sleep(0.05)
    changed = reg.expire_offline()
    assert len(changed) == 1
    assert changed[0]["online"] is False


def test_msg_rate_positive():
    reg = NodeRegistry(5.0)
    for i in range(1, 11):
        reg.mark_seen("A", i)
        time.sleep(0.005)
    snap = [n for n in reg.snapshot() if n["node_id"] == "A"][0]
    assert snap["msg_rate_hz"] > 0