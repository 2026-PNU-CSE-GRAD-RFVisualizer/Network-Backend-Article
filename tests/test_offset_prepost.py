"""사전/사후 Device Offset 분리 검증 (팀장 추가 요청 1).

- 사전·사후 OffsetRun 이 서로 다른 ID 로 저장된다.
- 사후 계산이 사전 적용값을 덮어쓰지 않는다.
- corrected_rssi 는 Run 의 '사전' offset 을 쓴다.
- QC/Export 에 node별 drift(post-pre) 가 기록된다.
- device_offset 구(舊) 스키마 DB 도 마이그레이션 후 데이터 보존.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.experiment import ExperimentManager, compute_device_offsets  # noqa: E402
from backend.export import export_experiment  # noqa: E402
from backend.store import SCHEMA_VERSION, ExperimentStore  # noqa: E402

NODES = ["n1", "n2", "n3", "n4", "n5"]


def ingest_offset(store, mgr, node, ts, rssi):
    ctx = mgr.offset_context(node)
    store.insert_measurements([{
        "experiment_id": mgr.experiment_id, "session_id": ctx["run_id"],
        "run_id": ctx["run_id"], "segment_id": None,
        "point_id": ctx["point_id"], "point_role": "offset",
        "node_id": node, "node_ts_ms": ts, "server_ts_ms": ts,
        "rssi_filtered_dbm": rssi, "sample_count": 5, "error_flags": 0, "valid": True,
    }])


def _measure_offset(store, mgr, phase, bias):
    off = mgr.start_offset_run(phase)
    t = int(time.time() * 1000)
    for i in range(20):
        for n in NODES:
            ingest_offset(store, mgr, n, t + i * 50, -60.0 + bias[n])
    mgr.stop_offset_run()
    res = compute_device_offsets(store, mgr.experiment_id, off["offset_run_id"])
    return off["offset_run_id"], res


def test_pre_post_stored_separately_no_overwrite():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = ExperimentStore(Path(tmp) / "data")
        mgr = ExperimentManager(store)
        mgr.start_experiment("exp1", "aa", 6)
        for n in NODES:
            mgr.assign(n, "offset-00", "offset")

        pre_bias = {"n1": 1.8, "n2": -2.4, "n3": 0.6, "n4": -1.1, "n5": 3.2}
        post_bias = {"n1": 2.3, "n2": -2.0, "n3": 0.6, "n4": -0.5, "n5": 3.9}  # 달라짐
        pre_id, pre_res = _measure_offset(store, mgr, "pre", pre_bias)
        post_id, post_res = _measure_offset(store, mgr, "post", post_bias)

        assert pre_id != post_id
        pre_rows = {o["node_id"]: o["device_offset_db"] for o in store.list_device_offsets("exp1", pre_id)}
        post_rows = {o["node_id"]: o["device_offset_db"] for o in store.list_device_offsets("exp1", post_id)}
        assert len(pre_rows) == 5 and len(post_rows) == 5
        # 사후 계산이 사전값을 덮어쓰지 않았다
        assert pre_rows != post_rows
        # 두 offset run 의 행이 device_offset 에 공존한다 (5+5)
        assert len(store.list_device_offsets("exp1")) == 10
        store.close()


def test_corrected_uses_pre_offset_and_export_drift():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = ExperimentStore(Path(tmp) / "data")
        mgr = ExperimentManager(store)
        mgr.start_experiment("exp1", "aa", 6)
        for n in NODES:
            mgr.assign(n, "offset-00", "offset")
        pre_id, _ = _measure_offset(store, mgr, "pre", {"n1": 1.8, "n2": -2.4, "n3": 0.6, "n4": -1.1, "n5": 3.2})
        post_id, _ = _measure_offset(store, mgr, "post", {"n1": 5.0, "n2": -5.0, "n3": 0.6, "n4": -1.1, "n5": 3.2})

        # 본 실험 Run: 사전 offset 자동 적용
        for n, pt in [("n1", "C1"), ("n2", "C2"), ("n3", "C3"), ("n4", "C4")]:
            mgr.assign(n, pt, "calibration")
            store.upsert_point("exp1", pt, "calibration", 1.0, 1.0, 0.8, None, 0)
        mgr.assign("n5", "T-move", "test")
        store.upsert_point("exp1", "T1", "test", 2.0, 2.0, 0.8, None, 0)
        store.upsert_tx("exp1", "tx-01", 7.0, 1.5, 1.2, 2_400_000_000, "aa", 6, None)

        run = mgr.start_run("forward", 1)
        assert store.get_run(run["run_id"])["pre_offset_run_id"] == pre_id  # 사전 적용됨
        seg = mgr.prepare_test_segment("T1", 1, 0, 2)
        rs = seg["recording_started_at_ms"]
        for k in range(3):
            for n in NODES:
                ctx = mgr.context_at(n, rs + k * 300)
                if ctx:
                    store.insert_measurements([{
                        "experiment_id": "exp1", "session_id": ctx["run_id"],
                        "run_id": ctx["run_id"], "segment_id": ctx["segment_id"],
                        "point_id": ctx["point_id"], "point_role": ctx["point_role"],
                        "node_id": n, "server_ts_ms": rs + k * 300, "node_ts_ms": rs + k * 300,
                        "rssi_filtered_dbm": -55.0, "valid": True}])
        mgr.finish_test_segment()
        mgr.attach_post_offset(post_id)

        # export 행의 device_offset_db 는 '사전' 값이어야 한다
        rows = store.measurements_for_export("exp1")
        pre_map = {o["node_id"]: o["device_offset_db"] for o in store.list_device_offsets("exp1", pre_id)}
        run_rows = [r for r in rows if r["run_id"] == run["run_id"] and r["point_role"] == "calibration"]
        assert run_rows
        for r in run_rows:
            assert r["device_offset_db"] == pre_map[r["node_id"]], "corrected 가 사전 offset 을 안 씀"

        out = export_experiment(store, "exp1", Path(tmp) / "out", expected_samples=3, expected_test_points=1)
        dev = json.loads((Path(out["path"]) / "config" / "device_offsets.json").read_text())
        assert dev["pre_offset_run_id"] == pre_id and dev["post_offset_run_id"] == post_id
        drift = {d["node_id"]: d["device_offset_drift_db"] for d in dev["drift"]}
        assert all(v is not None for v in drift.values())   # pre·post 모두 있으니 drift 계산됨
        assert "offset_drift" in out["qc"]
        store.close()


def test_legacy_device_offset_migrates():
    """구 device_offset((experiment_id,node_id) 키) DB 를 열어도 데이터가 보존된다."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        data = Path(tmp) / "data"; data.mkdir(parents=True)
        db = data / "experiment.db"
        con = sqlite3.connect(db)
        con.executescript(
            """CREATE TABLE measurement(id INTEGER PRIMARY KEY, experiment_id TEXT NOT NULL,
                   session_id TEXT NOT NULL, point_id TEXT NOT NULL, point_role TEXT NOT NULL,
                   node_id TEXT NOT NULL, server_ts_ms INTEGER NOT NULL);
               CREATE TABLE device_offset(experiment_id TEXT NOT NULL, node_id TEXT NOT NULL,
                   offset_median_dbm REAL, device_offset_db REAL, sample_count INTEGER,
                   std_db REAL, calibrated_at_ms INTEGER, PRIMARY KEY(experiment_id,node_id));
               INSERT INTO device_offset VALUES('oldexp','node1',-61.0,1.5,100,0.3,1700000000000);""")
        con.commit(); con.close()

        store = ExperimentStore(data)
        con = sqlite3.connect(db)
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        cols = {r[1] for r in con.execute("PRAGMA table_info(device_offset)")}
        assert "offset_run_id" in cols
        row = con.execute("SELECT offset_run_id, device_offset_db FROM device_offset "
                          "WHERE node_id='node1'").fetchone()
        assert row[0] == "legacy:oldexp" and row[1] == 1.5   # 보존 + legacy 키
        con.close(); store.close()


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
