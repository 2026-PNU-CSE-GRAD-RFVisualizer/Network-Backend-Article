"""Run / TestSegment 상태 관리자 검증 (3단계). 브로커·FastAPI 없이 실행.

    python tests/test_run_flow.py

상태 규칙(§7.3), 시간 매칭(§3.4), C1~C4 연속 저장, 재측정 supersede, 정/역방향 분리를 검증.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.experiment import Conflict, ExperimentManager  # noqa: E402
from backend.experiment import compute_device_offsets  # noqa: E402
from backend.store import ExperimentStore  # noqa: E402


def _pre_offset(store, mgr):
    """start_run 전제인 사전 Offset 을 준비한다."""
    off = mgr.start_offset_run("pre")
    t = 1_700_000_000_000
    for i in range(10):
        for node in ["n1", "n2", "n3", "n4", "nt"]:
            ctx = mgr.offset_context(node)
            store.insert_measurements([{
                "experiment_id": "exp1", "session_id": ctx["run_id"],
                "run_id": ctx["run_id"], "segment_id": None,
                "point_id": ctx["point_id"], "point_role": "offset",
                "node_id": node, "server_ts_ms": t + i * 50, "node_ts_ms": t + i * 50,
                "rssi_filtered_dbm": -60.0, "valid": True}])
    mgr.stop_offset_run()
    compute_device_offsets(store, "exp1", off["offset_run_id"])


def _mgr(tmp):
    store = ExperimentStore(Path(tmp) / "data")
    mgr = ExperimentManager(store)
    mgr.start_experiment("exp1", "aa:bb", 6)
    # C1~C4 고정 배치 + 이동 센서 T
    for node, pt in [("n1", "C1"), ("n2", "C2"), ("n3", "C3"), ("n4", "C4")]:
        mgr.assign(node, pt, "calibration")
    mgr.assign("nt", "T-move", "test")
    _pre_offset(store, mgr)   # start_run 이 사전 Offset 을 요구함
    return store, mgr


def test_segment_without_run_fails():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store, mgr = _mgr(tmp)
        try:
            mgr.prepare_test_segment("T1", 1, 20, 120)
        except Conflict:
            store.close(); return
        raise AssertionError("Run 없이 Segment 시작이 허용됨")


def test_second_run_fails():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store, mgr = _mgr(tmp)
        mgr.start_run("forward", 1)
        try:
            mgr.start_run("forward", 1)
        except Conflict:
            store.close(); return
        raise AssertionError("활성 Run 중 새 Run 이 허용됨")


def test_second_segment_fails():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store, mgr = _mgr(tmp)
        mgr.start_run("forward", 1)
        mgr.prepare_test_segment("T1", 1, 20, 120)
        try:
            mgr.prepare_test_segment("T2", 2, 20, 120)
        except Conflict:
            store.close(); return
        raise AssertionError("활성 Segment 중 새 Segment 가 허용됨")


def test_end_run_with_active_segment_fails():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store, mgr = _mgr(tmp)
        mgr.start_run("forward", 1)
        mgr.prepare_test_segment("T1", 1, 20, 120)
        try:
            mgr.end_run()
        except Conflict:
            store.close(); return
        raise AssertionError("활성 Segment 가 있는데 Run 종료가 허용됨")


def test_run_survives_segment_completion():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store, mgr = _mgr(tmp)
        mgr.start_run("forward", 1)
        mgr.prepare_test_segment("T1", 1, 20, 120)
        mgr.finish_test_segment()
        assert mgr.active_run() is not None, "Segment 완료 후 Run 이 사라짐"
        assert mgr.active_test_segment() is None
        store.close()


def test_offset_and_run_are_exclusive():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store, mgr = _mgr(tmp)
        mgr.start_offset_run()
        try:
            mgr.start_run("forward", 1)
        except Conflict:
            mgr.stop_offset_run()
            mgr.start_run("forward", 1)  # offset 종료 후엔 가능
            store.close(); return
        raise AssertionError("Offset 중 Run 시작이 허용됨")


def test_time_matching_calibration_and_test():
    """context_at: C1~C4 는 항상, T 는 기록창 안일 때만. 창 밖 ts 는 segment_id=None."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store, mgr = _mgr(tmp)
        mgr.start_run("forward", 1)
        seg = mgr.prepare_test_segment("T1", 1, 20, 120)
        rs, re = seg["recording_started_at_ms"], seg["recording_ended_at_ms"]

        # 안정화 중(기록창 이전): C1 은 저장(run_id 있고 segment_id 없음), T 는 저장 안 함
        c1_before = mgr.context_at("n1", rs - 5000)
        assert c1_before is not None and c1_before["run_id"] and c1_before["segment_id"] is None
        assert c1_before["point_role"] == "calibration"
        assert mgr.context_at("nt", rs - 5000) is None  # 이동·안정화 중 T 제외

        # 기록창 안: C1 과 T 모두 같은 segment_id
        mid = (rs + re) // 2
        c1_in = mgr.context_at("n1", mid)
        t_in = mgr.context_at("nt", mid)
        assert c1_in["segment_id"] == t_in["segment_id"] and t_in["segment_id"] is not None
        assert t_in["point_role"] == "test" and t_in["point_id"] == "T1"
        assert c1_in["point_id"] == "C1"

        # 종료 시각 정각: [start, end) 규칙으로 제외
        assert mgr.context_at("nt", re) is None
        c1_after = mgr.context_at("n1", re)
        assert c1_after is not None and c1_after["segment_id"] is None  # C1 은 계속 저장
        store.close()


def test_delayed_sample_still_matches_segment_by_time():
    """자동 완료(자연 종료) 후 지연 도착해도 ts 가 기록창 안이면 그 Segment 로 판정."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store, mgr = _mgr(tmp)
        mgr.start_run("forward", 1)
        seg = mgr.prepare_test_segment("T1", 1, 0, 2)   # 안정화 0, 기록 2s
        mid = (seg["recording_started_at_ms"] + seg["recording_ended_at_ms"]) // 2
        time.sleep(2.1)
        fin = mgr.auto_advance_segment()                # 자연 종료(창 절단 없음)
        assert fin is not None and mgr.active_test_segment() is None
        # 활성 포인터는 없지만 시간 범위로 판정되어야 한다
        t_delayed = mgr.context_at("nt", mid)
        assert t_delayed is not None and t_delayed["segment_id"] == seg["segment_id"]
        store.close()


def test_early_stop_truncates_recording_window():
    """조기 종료하면 기록창이 종료 시각으로 절단되어, 이후 데이터가 이 T 에 오염되지 않는다."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store, mgr = _mgr(tmp)
        mgr.start_run("forward", 1)
        seg = mgr.prepare_test_segment("T1", 1, 0, 120)  # 계획 2분
        orig_end = seg["recording_ended_at_ms"]
        time.sleep(0.2)
        mgr.finish_test_segment()                        # 조기 종료
        row = store.get_test_segment(seg["segment_id"])
        assert row["recording_ended_at_ms"] < orig_end   # 창이 줄었다
        # 원래 창 후반(mid) 시각의 T 샘플은 이제 이 Segment 에 안 붙는다
        mid = (seg["recording_started_at_ms"] + orig_end) // 2
        assert mgr.context_at("nt", mid) is None
        store.close()


def test_remeasure_supersedes_only_that_point():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store, mgr = _mgr(tmp)
        mgr.start_run("forward", 1)
        s1 = mgr.prepare_test_segment("T4", 4, 20, 120)
        mgr.discard_test_segment()               # T4 attempt1 폐기
        s2 = mgr.prepare_test_segment("T4", 4, 20, 120)  # attempt2
        assert s2["attempt_index"] == 2
        seg1 = store.get_test_segment(s1["segment_id"])
        assert seg1["superseded"] == 1 and seg1["status"] == "discarded"
        # 폐기된 구간 시간대는 segment 매칭에서 제외, 새 구간만 유효
        mid2 = (s2["recording_started_at_ms"] + s2["recording_ended_at_ms"]) // 2
        assert mgr.context_at("nt", mid2)["segment_id"] == s2["segment_id"]
        store.close()


def test_forward_and_reverse_are_separate_runs():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store, mgr = _mgr(tmp)
        mgr.start_run("forward", 1)
        fwd = mgr.active_run().run_id
        mgr.prepare_test_segment("T1", 1, 20, 120); mgr.finish_test_segment()
        mgr.end_run()
        mgr.start_run("reverse", 2)
        rev = mgr.active_run().run_id
        mgr.prepare_test_segment("T1", 1, 20, 120); mgr.finish_test_segment()
        mgr.end_run()
        assert fwd != rev
        runs = store.list_runs("exp1")
        dirs = sorted(r["direction"] for r in runs)
        assert dirs == ["forward", "reverse"]
        # 정방향 T1 과 역방향 T1 은 서로 다른 run_id 의 별도 Segment
        fwd_segs = store.list_test_segments(fwd)
        rev_segs = store.list_test_segments(rev)
        assert len(fwd_segs) == 1 and len(rev_segs) == 1
        assert fwd_segs[0]["segment_id"] != rev_segs[0]["segment_id"]
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
