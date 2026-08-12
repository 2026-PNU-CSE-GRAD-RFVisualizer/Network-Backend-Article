"""Export/QC 검증 (8단계). 새 흐름을 돌려 CSV 산출물을 확인.

- test_points.csv 에 Run·방향·Segment 정보
- 각 완료 Segment 마다 동일 시간대 C1~C4 (calibration_by_test_window)
- 폐기 Segment 는 기본 Export 에서 제외
- QC 통과
"""

from __future__ import annotations

import csv
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.experiment import ExperimentManager, compute_device_offsets  # noqa: E402
from backend.export import export_experiment  # noqa: E402
from backend.store import ExperimentStore  # noqa: E402

NODES = ["n1", "n2", "n3", "n4", "nt"]
BIAS = {"n1": 1.8, "n2": -2.4, "n3": 0.6, "n4": -1.1, "nt": 3.2}
BASE = -60.0


def ingest(store, mgr, node, ts, rssi):
    ctx = mgr.offset_context(node) or mgr.context_at(node, ts)
    if ctx is None:
        return False
    store.insert_measurements([{
        "experiment_id": mgr.experiment_id, "session_id": ctx["run_id"],
        "run_id": ctx["run_id"], "segment_id": ctx["segment_id"],
        "point_id": ctx["point_id"], "point_role": ctx["point_role"],
        "node_id": node, "node_ts_ms": ts, "server_ts_ms": ts,
        "rssi_raw_dbm": rssi, "rssi_filtered_dbm": rssi, "sample_count": 5,
        "error_flags": 0, "valid": True,
    }])
    return True


def _fill_segment(store, mgr, seg):
    rs, re = seg["recording_started_at_ms"], seg["recording_ended_at_ms"]
    for k in range(4):
        ts = rs + k * 400
        if ts >= re:
            break
        for n in NODES:
            ingest(store, mgr, n, ts, BASE + BIAS[n])


def _read_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def _build(tmp):
    store = ExperimentStore(Path(tmp) / "data")
    mgr = ExperimentManager(store)
    mgr.start_experiment("exp1", "aa:bb", 6)

    # offset
    for n in NODES:
        mgr.assign(n, "offset-00", "offset")
    off = mgr.start_offset_run()
    t = 1_700_000_000_000
    for i in range(30):
        for n in NODES:
            ingest(store, mgr, n, t + i * 100, BASE + BIAS[n])
    mgr.stop_offset_run()
    compute_device_offsets(store, "exp1", offset_run_id=off["offset_run_id"])

    # 좌표 등록 (T1~T3, C1~C4) + TX
    for n, pt in [("n1", "C1"), ("n2", "C2"), ("n3", "C3"), ("n4", "C4")]:
        mgr.assign(n, pt, "calibration")
        store.upsert_point("exp1", pt, "calibration", 1.0, 1.0, 0.8, None, t)
    mgr.assign("nt", "T-move", "test")
    for i, tp in enumerate(["T1", "T2", "T3"], start=1):
        store.upsert_point("exp1", tp, "test", float(i), 2.0, 0.8, None, t)
    store.upsert_tx("exp1", "tx-01", 7.7, 1.5, 1.2, 2_400_000_000, "aa:bb", 6, None)

    # forward run: T1(완료), T2(폐기 후 재측정), T3(완료).
    # 실제로는 T 가 물리적으로 이동해 창이 겹치지 않는다. 압축 테스트에서는 sleep 으로 재현.
    mgr.start_run("forward", 1)
    _fill_segment(store, mgr, mgr.prepare_test_segment("T1", 1, 0, 1)); mgr.finish_test_segment()
    time.sleep(1.1)

    bad = mgr.prepare_test_segment("T2", 2, 0, 1)
    _fill_segment(store, mgr, bad); mgr.discard_test_segment()          # 폐기
    good = mgr.prepare_test_segment("T2", 2, 0, 1)
    _fill_segment(store, mgr, good); mgr.finish_test_segment()          # 재측정
    time.sleep(1.1)

    _fill_segment(store, mgr, mgr.prepare_test_segment("T3", 3, 0, 1)); mgr.finish_test_segment()
    mgr.end_run()
    return store, bad, good


def test_export_files_and_contents():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store, bad_seg, good_seg = _build(tmp)
        out = export_experiment(store, "exp1", Path(tmp) / "out",
                                expected_samples=3, expected_test_points=3)
        root = Path(out["path"])

        # 파일 존재
        for rel in ["processed/test_points.csv", "processed/calibration_by_test_window.csv",
                    "processed/calibration_points.csv", "config/runs.json",
                    "config/test_segments.json", "raw/measurements_raw.csv"]:
            assert (root / rel).exists(), f"{rel} 없음"

        tp = _read_csv(root / "processed" / "test_points.csv")
        # T1, T2(재측정), T3 = 3행. 폐기된 T2 attempt1 은 제외
        assert len(tp) == 3, [r["point_id"] for r in tp]
        assert sorted(r["point_id"] for r in tp) == ["T1", "T2", "T3"]
        assert all(r["direction"] == "forward" and r["run_id"] and r["segment_id"] for r in tp)
        assert bad_seg["segment_id"] not in {r["segment_id"] for r in tp}   # 폐기 제외
        t2 = next(r for r in tp if r["point_id"] == "T2")
        assert t2["segment_id"] == good_seg["segment_id"] and t2["attempt_index"] == "2"

        # calibration_by_test_window: 완료 Segment 3개 × C1~C4 = 12행, 창이 test 와 동일
        cw = _read_csv(root / "processed" / "calibration_by_test_window.csv")
        assert len(cw) == 12, len(cw)
        by_seg = {}
        for r in cw:
            by_seg.setdefault(r["segment_id"], set()).add(r["calibration_point_id"])
        for seg_id, cals in by_seg.items():
            assert cals == {"C1", "C2", "C3", "C4"}
        # 같은 segment 의 test 와 calibration 창 시각 일치
        tp_by_seg = {r["segment_id"]: r for r in tp}
        for r in cw:
            assert r["window_started_at_ms"] == tp_by_seg[r["segment_id"]]["recording_started_at_ms"]

        # QC 통과
        assert out["qc"]["ok"], out["qc"]["problems"]
        store.close()


def test_qc_fails_when_calibration_missing():
    """C1~C4 동시간 데이터가 없는 Test 구간은 QC 실패여야 한다(리뷰 재현)."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = ExperimentStore(Path(tmp) / "data")
        mgr = ExperimentManager(store)
        mgr.start_experiment("exp1", "aa", 6)
        for n in NODES:
            mgr.assign(n, "offset-00", "offset")
        off = mgr.start_offset_run("pre")
        t = 1_700_000_000_000
        for i in range(20):
            for n in NODES:
                ingest(store, mgr, n, t + i * 50, BASE + BIAS[n])
        mgr.stop_offset_run()
        compute_device_offsets(store, "exp1", off["offset_run_id"])

        mgr.assign("nt", "T-move", "test")
        store.upsert_point("exp1", "T1", "test", 2.0, 2.0, 0.8, None, t)
        store.upsert_tx("exp1", "tx-01", 7.0, 1.5, 1.2, 2_400_000_000, "aa", 6, None)

        mgr.start_run("forward", 1)
        seg = mgr.prepare_test_segment("T1", 1, 0, 2)
        rs = seg["recording_started_at_ms"]
        for k in range(3):                       # T 만 저장, C1~C4 는 저장하지 않음
            ingest(store, mgr, "nt", rs + k * 300, BASE + BIAS["nt"])
        mgr.finish_test_segment()

        out = export_experiment(store, "exp1", Path(tmp) / "out",
                                expected_samples=3, expected_test_points=1)
        assert out["qc"]["ok"] is False
        assert any("C1~C4" in p for p in out["qc"]["problems"]), out["qc"]["problems"]
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
