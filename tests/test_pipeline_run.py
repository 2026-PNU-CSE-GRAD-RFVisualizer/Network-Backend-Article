"""새 흐름 종단 저장 검증 (6단계). 브로커·FastAPI 없이 measurement 테이블까지 확인.

mqtt_bridge._record_experiment 과 동일한 저장 매핑을 재현해:
- Offset → device offset 복원
- C1~C4 연속 저장(이동 구간엔 segment_id=NULL, 기록창엔 segment_id)
- T 는 기록창 안에서만, C1~C4 와 같은 segment_id
- 정/역방향 run 분리
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.experiment import ExperimentManager, compute_device_offsets  # noqa: E402
from backend.store import ExperimentStore  # noqa: E402

NODES = ["n1", "n2", "n3", "n4", "nt"]
TRUE_BIAS = {"n1": +1.8, "n2": -2.4, "n3": +0.6, "n4": -1.1, "nt": +3.2}
BASE = -60.0


def ingest(store, mgr, node, ts, rssi):
    """mqtt_bridge._record_experiment 과 동일한 저장 경로 재현."""
    ctx = mgr.offset_context(node) or mgr.context_at(node, ts)
    if ctx is None:
        return False
    store.insert_measurements([{
        "experiment_id": mgr.experiment_id, "session_id": ctx["run_id"],
        "run_id": ctx["run_id"], "segment_id": ctx["segment_id"],
        "point_id": ctx["point_id"], "point_role": ctx["point_role"],
        "node_id": node, "node_ts_ms": ts, "server_ts_ms": ts,
        "rssi_raw_dbm": rssi, "rssi_filtered_dbm": rssi,
        "sample_count": 5, "error_flags": 0, "valid": True,
    }])
    return True


def _setup():
    tmp = tempfile.mkdtemp()
    store = ExperimentStore(Path(tmp) / "data")
    mgr = ExperimentManager(store)
    mgr.start_experiment("exp1", "aa:bb", 6)
    return store, mgr


def _run_offset(store, mgr):
    for n in NODES:
        mgr.assign(n, "offset-00", "offset")
    off = mgr.start_offset_run()
    t = 1_700_000_000_000
    for i in range(30):
        for n in NODES:
            ingest(store, mgr, n, t + i * 1000, BASE + TRUE_BIAS[n])
    mgr.stop_offset_run()
    return off["offset_run_id"]


def test_offset_recovers_bias():
    store, mgr = _setup()
    off_id = _run_offset(store, mgr)
    res = compute_device_offsets(store, "exp1", offset_run_id=off_id)
    assert res["ok"], res.get("reason")
    ref = sorted(TRUE_BIAS.values())[len(TRUE_BIAS) // 2]
    for item in res["nodes"]:
        expected = ref - TRUE_BIAS[item["node_id"]]
        assert abs(item["device_offset_db"] - expected) < 0.5, item
    store.close()


def test_calibration_continuous_and_test_windowed():
    store, mgr = _setup()
    _run_offset(store, mgr)
    # C1~C4 고정 + 이동 센서 T
    for n, pt in [("n1", "C1"), ("n2", "C2"), ("n3", "C3"), ("n4", "C4")]:
        mgr.assign(n, pt, "calibration")
    mgr.assign("nt", "T-move", "test")

    mgr.start_run("forward", 1)
    run_id = mgr.active_run().run_id

    for order, tp in enumerate(["T1", "T2", "T3"], start=1):
        seg = mgr.prepare_test_segment(tp, order, 0, 2)  # 안정화 0, 기록 2s (압축)
        rs, re = seg["recording_started_at_ms"], seg["recording_ended_at_ms"]

        # 이동 구간(기록창 이전): C1~C4 저장(segment_id NULL), T 는 저장 안 됨
        move_ts = rs - 500
        for n in ["n1", "n2", "n3", "n4"]:
            assert ingest(store, mgr, n, move_ts, BASE + TRUE_BIAS[n]) is True
        assert ingest(store, mgr, "nt", move_ts, BASE + TRUE_BIAS["nt"]) is False

        # 기록창 안: C1~C4 + T 모두 저장, 같은 segment_id
        for k in range(3):
            ts = rs + k * 500
            if ts >= re:
                break
            for n in NODES:
                ingest(store, mgr, n, ts, BASE + TRUE_BIAS[n])
        mgr.finish_test_segment()

    # 검증
    rows = store.measurements_for_export("exp1")
    run_rows = [r for r in rows if r["run_id"] == run_id]

    # T 데이터는 전부 segment_id 가 있어야 한다(이동 구간 T 는 저장 안 됨)
    t_rows = [r for r in run_rows if r["node_id"] == "nt"]
    assert t_rows and all(r["segment_id"] is not None for r in t_rows)
    assert all(r["point_role"] == "test" for r in t_rows)

    # C1~C4 는 segment_id 있는 행(기록창)과 NULL 행(이동) 둘 다 있어야 한다
    c_rows = [r for r in run_rows if r["node_id"] in ("n1", "n2", "n3", "n4")]
    assert any(r["segment_id"] is None for r in c_rows), "이동 구간 C1~C4 저장 안 됨"
    assert any(r["segment_id"] is not None for r in c_rows), "기록창 C1~C4 저장 안 됨"

    # 각 기록창에서 C1~C4 와 T 의 segment_id 가 일치
    segs = {r["segment_id"] for r in t_rows}
    for sid in segs:
        c_in_seg = {r["node_id"] for r in c_rows if r["segment_id"] == sid}
        assert {"n1", "n2", "n3", "n4"} <= c_in_seg, f"{sid}: 동시간 C1~C4 누락"
    store.close()


def test_forward_reverse_separate_runs_in_storage():
    store, mgr = _setup()
    _run_offset(store, mgr)
    for n, pt in [("n1", "C1"), ("n2", "C2"), ("n3", "C3"), ("n4", "C4")]:
        mgr.assign(n, pt, "calibration")
    mgr.assign("nt", "T-move", "test")

    run_ids = []
    for direction, pidx in [("forward", 1), ("reverse", 2)]:
        mgr.start_run(direction, pidx)
        rid = mgr.active_run().run_id
        run_ids.append(rid)
        seg = mgr.prepare_test_segment("T1", 1, 0, 2)
        mid = (seg["recording_started_at_ms"] + seg["recording_ended_at_ms"]) // 2
        ingest(store, mgr, "nt", mid, BASE)
        mgr.finish_test_segment()
        mgr.end_run()

    # 정/역방향 T1 은 서로 다른 run_id 로 분리 저장
    rows = store.measurements_for_export("exp1")
    t1 = [r for r in rows if r["node_id"] == "nt" and r["point_id"] == "T1"]
    assert {r["run_id"] for r in t1} == set(run_ids)
    assert len(t1) >= 2
    store.close()


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
