from backend.metrics import Metrics


def test_counters_and_drop_rate():
    m = Metrics()
    for _ in range(8):
        m.inc_received()
    for _ in range(2):
        m.inc_dropped()
    snap = m.snapshot()
    assert snap["messages_received"] == 8
    assert snap["messages_dropped"] == 2
    assert snap["drop_rate"] == round(2 / 10, 4)


def test_latency_stats():
    m = Metrics()
    for v in [10, 20, 30, 40, 100]:
        m.observe_collect_latency(v)
    st = m.snapshot()["collect_latency_ms"]
    assert st["count"] == 5
    assert st["max"] == 100
    assert st["avg"] == 40.0


def test_empty_stats():
    st = Metrics().snapshot()["window_flush_ms"]
    assert st == {"count": 0, "avg": 0.0, "p95": 0.0, "max": 0.0}
