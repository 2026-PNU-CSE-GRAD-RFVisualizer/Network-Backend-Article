"""WindowBuffer 검증 — 측정시각 버킷팅 + 늦은 도착 유예 (전문가 자문 반영).

    pytest tests/test_window.py -q
    python  tests/test_window.py        # pytest 없이도 실행 가능
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.realtime.window import WindowBuffer  # noqa: E402


def _m(node_id: str, ts: int, seq: int = 0, rssi: int = -60) -> dict:
    return {"node_id": node_id, "timestamp": ts, "seq": seq, "rssi": rssi}


def test_buckets_by_measurement_time_not_arrival():
    """도착 시각이 늦어도 '측정 시각'이 같은 구간이면 한 프레임에 묶인다."""
    wb = WindowBuffer(window_size_ms=200, grace_ms=100, stall_flush_ms=100)
    wb.add(_m("n1", ts=1000), receive_ms=1000)     # 제때 도착
    wb.add(_m("n2", ts=1100), receive_ms=1350)     # 측정은 같은 구간(1000~1200), 도착만 늦음

    frames = wb.pop_ready(current_ms=1600, known_nodes={"n1", "n2"})
    assert len(frames) == 1
    f = frames[0]
    assert f["window_ts"] == 1000
    assert set(f["nodes"]) == {"n1", "n2"}, "늦게 도착한 n2 가 측정 구간에 안 묶였다"
    assert f["nodes"]["n2"]["node_ts"] == 1100


def test_measurements_split_into_correct_windows():
    wb = WindowBuffer(window_size_ms=200, grace_ms=0, stall_flush_ms=0)
    wb.add(_m("n1", ts=1000), receive_ms=1000)  # bucket 1000
    wb.add(_m("n1", ts=1250), receive_ms=1250)  # bucket 1200
    frames = wb.pop_ready(current_ms=2000, known_nodes={"n1"})
    assert [f["window_ts"] for f in frames] == [1000, 1200]


def test_late_after_emit_is_dropped_and_counted():
    """유예까지 지나 확정된 구간에 뒤늦게 도착한 샘플은 버리고 카운트한다."""
    wb = WindowBuffer(window_size_ms=200, grace_ms=100, stall_flush_ms=100)
    wb.add(_m("n1", ts=1000), receive_ms=1000)
    emitted = wb.pop_ready(current_ms=1600, known_nodes={"n1"})
    assert len(emitted) == 1 and emitted[0]["window_ts"] == 1000

    wb.add(_m("n1", ts=1050, seq=9), receive_ms=1650)  # 이미 확정된 1000 구간에 늦게 도착
    assert wb.late_dropped == 1
    # 재확정되어 중복 프레임이 나오면 안 된다.
    assert wb.pop_ready(current_ms=2000, known_nodes={"n1"}) == []


def test_grace_holds_bucket_until_late_window_passes():
    """유예 구간 동안은 확정하지 않는다 (늦은 도착을 기다림)."""
    wb = WindowBuffer(window_size_ms=200, grace_ms=100, stall_flush_ms=100)
    wb.add(_m("n1", ts=1000), receive_ms=1000)   # watermark=1000
    # 구간끝(1200)+유예(100)=1300 이전에는 확정 금지. watermark 로는 아직 부족.
    assert wb.pop_ready(current_ms=1250, known_nodes={"n1"}) == []
    # watermark 가 1300 이상으로 올라오면 확정.
    wb.add(_m("n2", ts=1300), receive_ms=1300)   # watermark=1300 → 1300-200-100=1000
    frames = wb.pop_ready(current_ms=1350, known_nodes={"n1", "n2"})
    assert [f["window_ts"] for f in frames] == [1000]


def test_stall_fallback_forces_emit_when_stream_stops():
    """스트림이 멈춰 워터마크가 정체돼도 서버 시계로 결국 확정된다."""
    wb = WindowBuffer(window_size_ms=200, grace_ms=100, stall_flush_ms=100)
    wb.add(_m("n1", ts=1000), receive_ms=1000)   # 이후 아무 데이터 없음
    # 워터마크(1000)만으로는 event_ready=700 < 1000 이라 확정 안 됨.
    assert wb.pop_ready(current_ms=1100, known_nodes={"n1"}) == []
    # 서버 시계가 충분히 지나면 wall_ready 가 올라와 강제 확정.
    frames = wb.pop_ready(current_ms=1600, known_nodes={"n1"})
    assert [f["window_ts"] for f in frames] == [1000]


def test_latest_sample_wins_within_bucket():
    wb = WindowBuffer(window_size_ms=200, grace_ms=0, stall_flush_ms=0)
    wb.add(_m("n1", ts=1000, rssi=-70), receive_ms=1000)
    wb.add(_m("n1", ts=1150, rssi=-55), receive_ms=1150)  # 같은 버킷, 더 최신
    frames = wb.pop_ready(current_ms=2000, known_nodes={"n1"})
    assert frames[0]["nodes"]["n1"]["rssi"] == -55


def test_missing_nodes_reported():
    wb = WindowBuffer(window_size_ms=200, grace_ms=0, stall_flush_ms=0)
    wb.add(_m("n1", ts=1000), receive_ms=1000)
    frames = wb.pop_ready(current_ms=2000, known_nodes={"n1", "n2", "n3"})
    assert frames[0]["missing"] == ["n2", "n3"]


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
