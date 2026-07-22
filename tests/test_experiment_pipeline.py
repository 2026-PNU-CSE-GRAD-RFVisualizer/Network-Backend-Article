"""실험 파이프라인 종단 검증 (브로커·DB·네트워크 없이 실행).

7/23 절차를 그대로 재현한다: 장치 편차 측정 → 고정 센서 배치 →
Test 15개 이동 측정 → 좌표 등록 → offset 계산 → CSV Export.

가상 노드에 '정답' 편차를 심어 두고 백엔드가 그 값을 복원하는지 확인하므로,
계산이 틀리면 실패한다. 단순 스모크 테스트가 아니다.

    pytest tests/test_experiment_pipeline.py -q
    python  tests/test_experiment_pipeline.py        # pytest 없이도 실행 가능
"""

from __future__ import annotations

import csv
import json
import math
import random
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.experiment import SessionManager, compute_device_offsets  # noqa: E402
from backend.export import export_experiment, import_points_csv, summarize  # noqa: E402
from backend.parsing import ParseConfig, parse_measurement  # noqa: E402
from backend.store import ExperimentStore  # noqa: E402

ROOM_W, ROOM_D = 15.4, 10.8
AP_POS = (7.7, 1.5, 1.20)
TRUE_BIAS = {"node1": +1.8, "node2": -2.4, "node3": +0.6, "node4": -1.1, "node5": +3.2}
CAL_POINTS = {
    "cal-01": (2.4, 2.0, 0.78), "cal-02": (12.8, 2.6, 0.85),
    "cal-03": (3.1, 8.4, 1.32), "cal-04": (13.0, 9.2, 1.40),
}
FIXED_NODES = {"node1": "cal-01", "node2": "cal-02", "node3": "cal-03", "node5": "cal-04"}
MOVING_NODE = "node4"
OFFSET_SPOT = (7.0, 5.0, 0.80)
TEST_POINTS = {
    f"test-{i * 5 + j + 1:02d}": (
        round(2.0 + j * (ROOM_W - 4.0) / 4, 2),
        round(1.6 + i * (ROOM_D - 3.2) / 2, 2),
        round(0.78 + (1.6 + i * (ROOM_D - 3.2) / 2) * 0.069, 2),
    )
    for i in range(3) for j in range(5)
}
CFG = ParseConfig()
SAMPLES_PER_POINT = 30


def true_rssi(pos: tuple[float, float, float]) -> float:
    d = max(math.dist(pos, AP_POS), 0.5)
    return 20.0 - (20 * math.log10(2400) - 27.55 + 26.0 * math.log10(d))


def run_pipeline(tmp: Path, seed: int = 7) -> tuple[ExperimentStore, str, dict]:
    random.seed(seed)
    store = ExperimentStore(tmp / "data")
    sessions = SessionManager(store)
    experiment_id = "pytest_classroom"
    sessions.start_experiment(experiment_id, "aa:bb:cc:dd:ee:01", 6)

    positions = {node: OFFSET_SPOT for node in TRUE_BIAS}
    clock = {"ms": 1_700_000_000_000}

    def publish_window(session, count: int = SAMPLES_PER_POINT) -> None:
        """세션 동안 5개 노드가 1초 간격으로 발행하는 상황을 재현한다."""
        for _ in range(count):
            clock["ms"] += 1000
            for node_id in TRUE_BIAS:
                payload = {
                    "node_id": node_id,
                    "timestamp": clock["ms"],
                    "ap_bssid": "aa:bb:cc:dd:ee:01",
                    "ap_channel": 6,
                    "rssi": round(true_rssi(positions[node_id]) + TRUE_BIAS[node_id]
                                  + random.gauss(0, 0.8), 1),
                    "rssi_raw": round(true_rssi(positions[node_id]) + TRUE_BIAS[node_id]
                                      + random.gauss(0, 2.0), 1),
                    "sample_count": 5,
                    "seq": clock["ms"] // 1000,
                    "status": 0,
                }
                m = parse_measurement(payload, clock["ms"], CFG)
                assert m is not None
                point_id, point_role = sessions.resolve(node_id, session)
                store.insert_measurements([{
                    "experiment_id": session.experiment_id,
                    "session_id": session.session_id,
                    "point_id": point_id, "point_role": point_role, "node_id": node_id,
                    "node_ts_ms": m["timestamp"], "server_ts_ms": clock["ms"],
                    "seq": m["seq"], "rssi_raw_dbm": m["rssi_raw"],
                    "rssi_filtered_dbm": m["rssi_filtered"],
                    "sample_count": m["sample_count"], "error_flags": m["status"],
                    "ap_bssid": m["ap_bssid"], "ap_channel": m["ap_channel"],
                    "valid": m["valid"], "invalid_reason": m["invalid_reason"],
                }])

    # 1. 장치 편차 측정 — 5대 모두 같은 자리
    for node_id in TRUE_BIAS:
        sessions.assign(node_id, "offset-00", "offset")
    publish_window(sessions.start_session("offset-00", "offset", 30))
    sessions.stop_session()

    # 2. 고정 보정 센서 4대 배치
    for node_id, point_id in FIXED_NODES.items():
        positions[node_id] = CAL_POINTS[point_id]
        sessions.assign(node_id, point_id, "calibration")
    publish_window(sessions.start_session("cal-01", "calibration", 30))
    sessions.stop_session()

    # 3. 이동 센서로 Test 15개
    for point_id, pos in TEST_POINTS.items():
        positions[MOVING_NODE] = pos
        publish_window(sessions.start_session(point_id, "test", 30,
                                              moving_node_id=MOVING_NODE))
        sessions.stop_session()

    # 4. 좌표 등록
    lines = ["point_id,point_role,pos_x,pos_y,pos_z",
             f"offset-00,offset,{OFFSET_SPOT[0]},{OFFSET_SPOT[1]},{OFFSET_SPOT[2]}"]
    lines += [f"{p},calibration,{v[0]},{v[1]},{v[2]}" for p, v in CAL_POINTS.items()]
    lines += [f"{p},test,{v[0]},{v[1]},{v[2]}" for p, v in TEST_POINTS.items()]
    result = import_points_csv(store, experiment_id, "\n".join(lines), clock["ms"])
    assert not result["errors"], result["errors"]
    assert result["count"] == 20

    store.upsert_tx(experiment_id, "tx-01", AP_POS[0], AP_POS[1], AP_POS[2],
                    2_400_000_000, "aa:bb:cc:dd:ee:01", 6, None)
    offsets = compute_device_offsets(store, experiment_id)
    return store, experiment_id, offsets


# ----------------------------------------------------------------------


def test_device_offsets_recover_known_bias():
    """백엔드가 계산한 offset 이 심어 둔 참값을 복원해야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        store, experiment_id, offsets = run_pipeline(Path(tmp))
        assert offsets["ok"], offsets.get("reason")

        true_ref = sorted(TRUE_BIAS.values())[len(TRUE_BIAS) // 2]
        for item in offsets["nodes"]:
            expected = true_ref - TRUE_BIAS[item["node_id"]]
            assert abs(item["device_offset_db"] - expected) < 1.0, (
                f'{item["node_id"]}: {item["device_offset_db"]} != {expected}')
        store.close()


def test_corrected_rssi_removes_device_bias():
    """보정 후에는 장치가 달라도 같은 위치에서 비슷한 값이 나와야 한다.

    이 실험의 핵심 가정이다. 여기가 깨지면 Residual IDW 가 장치 편차를 공간 편차로
    오인하게 된다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        store, experiment_id, _ = run_pipeline(Path(tmp))
        rows = store.measurements_for_export(experiment_id)
        summary = {(s["point_id"], s["node_id"]): s for s in summarize(rows)}

        offset_rows = [s for (p, _), s in summary.items() if p == "offset-00"]
        assert len(offset_rows) == 5

        before = [s["median_filtered"] for s in offset_rows]
        after = [s["corrected_rssi"] for s in offset_rows]
        spread_before = max(before) - min(before)
        spread_after = max(after) - min(after)
        assert spread_before > 4.0, "테스트 전제: 보정 전에는 장치 간 차이가 커야 함"
        assert spread_after < 1.0, f"보정 후 편차가 남아 있음: {spread_after:.2f} dB"
        store.close()


def test_calibration_nodes_accumulate_across_sessions():
    """고정 보정 센서는 모든 세션 동안 데이터를 쌓아야 한다.

    '세션 1개 = 위치 1개' 모델이었다면 보정점 샘플이 30개에 그친다.
    """
    with tempfile.TemporaryDirectory() as tmp:
        store, experiment_id, _ = run_pipeline(Path(tmp))
        summary = summarize(store.measurements_for_export(experiment_id))
        cal = [s for s in summary if s["point_role"] == "calibration"]
        assert len(cal) == 4
        for s in cal:
            assert s["sample_count"] > SAMPLES_PER_POINT, (
                f'{s["point_id"]} 샘플 {s["sample_count"]}개 — 세션 간 누적이 안 됨')
        store.close()


def test_test_points_are_measured_by_moving_node_only():
    """Test 위치는 이동 센서 1대만 기록해야 한다. 섞이면 평가가 오염된다."""
    with tempfile.TemporaryDirectory() as tmp:
        store, experiment_id, _ = run_pipeline(Path(tmp))
        summary = summarize(store.measurements_for_export(experiment_id))
        test_rows = [s for s in summary if s["point_role"] == "test"]
        assert len({s["point_id"] for s in test_rows}) == 15
        assert {s["node_id"] for s in test_rows} == {MOVING_NODE}
        store.close()


def test_export_produces_graphics_ready_files():
    """그래픽스 파트가 CSV 만 받아 바로 실험할 수 있어야 한다 (완료 기준)."""
    with tempfile.TemporaryDirectory() as tmp:
        store, experiment_id, _ = run_pipeline(Path(tmp))
        out = export_experiment(store, experiment_id, Path(tmp) / "experiments",
                                expected_samples=SAMPLES_PER_POINT)
        root = Path(out["path"])

        for rel in ["raw/measurements_raw.csv", "processed/measurements_summary.csv",
                    "processed/calibration_points.csv", "processed/test_points.csv",
                    "config/tx_rx.json", "config/device_offsets.json",
                    "config/points.csv", "qc_report.json", "README.md"]:
            assert (root / rel).exists(), f"{rel} 누락"

        with (root / "processed" / "test_points.csv").open(encoding="utf-8-sig") as fp:
            test_rows = list(csv.DictReader(fp))
        assert len(test_rows) == 15
        for row in test_rows:
            assert row["x"] and row["y"] and row["z"], f'{row["point_id"]} 좌표 없음'
            assert row["corrected_rssi"], f'{row["point_id"]} corrected_rssi 없음'
            assert -100 < float(row["corrected_rssi"]) < -10

        with (root / "processed" / "calibration_points.csv").open(encoding="utf-8-sig") as fp:
            cal_rows = list(csv.DictReader(fp))
        assert len(cal_rows) == 4

        cal_ids = {r["point_id"] for r in cal_rows}
        test_ids = {r["point_id"] for r in test_rows}
        assert not (cal_ids & test_ids), "보정점과 Test점이 겹침 — 데이터 누설"

        qc = json.loads((root / "qc_report.json").read_text(encoding="utf-8"))
        assert qc["ok"], qc["problems"]
        store.close()


def test_invalid_samples_preserved_not_dropped():
    """범위 밖 RSSI 는 버리지 않고 valid=0 으로 남긴다 (계획서 §7.2 Raw 보존)."""
    m = parse_measurement(
        {"node_id": "node1", "timestamp": 1_700_000_000_000, "rssi": -3.0}, 1_700_000_000_000, CFG)
    assert m is not None and not m["valid"] and "out_of_range" in m["invalid_reason"]

    m = parse_measurement(
        {"node_id": "node1", "timestamp": 1_700_000_000_000, "rssi": -55, "status": 4},
        1_700_000_000_000, CFG)
    assert m is not None and not m["valid"] and "error_flags" in m["invalid_reason"]

    # 구조가 깨진 경우에만 완전히 버린다
    assert parse_measurement({"rssi": -55}, 1_700_000_000_000, CFG) is None


def test_x10_scale_and_field_aliases():
    """펌웨어가 ×10 정수로 보내도, 필드명이 달라도 같은 값으로 읽혀야 한다."""
    scaled = ParseConfig(rssi_filtered_scale=10.0)
    m = parse_measurement(
        {"node_id": "n1", "timestamp": 1_700_000_000_000, "rssi": -553, "rssi_raw": -561},
        1_700_000_000_000, scaled)
    assert m["rssi_filtered"] == -55.3 and m["rssi_raw"] == -56.1 and m["valid"]

    alias = parse_measurement(
        {"node_id": "n1", "timestamp": 1_700_000_000_000,
         "rssi_filtered_dbm": -61.2, "rssi_raw_dbm": -63.0, "channel": 6,
         "error_flags": 0}, 1_700_000_000_000, CFG)
    assert alias["rssi_filtered"] == -61.2 and alias["ap_channel"] == 6 and alias["valid"]


def test_remeasure_supersedes_only_that_point():
    """재측정 시 그 위치만 교체되고, 동시에 흘러든 보정점 데이터는 살아남아야 한다."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ExperimentStore(Path(tmp) / "data")
        sessions = SessionManager(store)
        sessions.start_experiment("exp", None, None)
        sessions.assign("node1", "cal-01", "calibration")

        def emit(session, node_id, point, role, value):
            store.insert_measurements([{
                "experiment_id": "exp", "session_id": session.session_id,
                "point_id": point, "point_role": role, "node_id": node_id,
                "server_ts_ms": 1, "rssi_filtered_dbm": value, "valid": True,
            }])

        s1 = sessions.start_session("test-01", "test", 30, moving_node_id="node4")
        emit(s1, "node4", "test-01", "test", -70.0)
        emit(s1, "node1", "cal-01", "calibration", -60.0)
        sessions.stop_session()

        s2 = sessions.start_session("test-01", "test", 30, moving_node_id="node4")
        emit(s2, "node4", "test-01", "test", -72.0)
        sessions.stop_session()

        rows = store.measurements_for_export("exp")
        test_vals = [r["rssi_filtered_dbm"] for r in rows if r["point_id"] == "test-01"]
        cal_vals = [r["rssi_filtered_dbm"] for r in rows if r["point_id"] == "cal-01"]
        assert test_vals == [-72.0], f"재측정이 이전 값을 대체하지 않음: {test_vals}"
        assert cal_vals == [-60.0], "폐기된 세션의 보정점 데이터까지 사라짐"
        store.close()


def test_qc_catches_missing_coordinates_and_mixed_bssid():
    """현장을 떠나기 전 걸러야 할 문제를 실제로 잡아내는지 확인한다."""
    from backend.export import quality_check
    rows = [{"ap_bssid": "aa", "valid": 1}, {"ap_bssid": "bb", "valid": 1}]
    summary = [{"point_id": "test-01", "point_role": "test", "node_id": "node4",
                "sample_count": 30, "x": None, "std_filtered": 0.5,
                "device_offset_db": None}]
    qc = quality_check(rows, summary, 30, offsets=[], points=[], tx_list=[])
    joined = " ".join(qc["problems"])
    assert not qc["ok"]
    assert "좌표 미등록" in joined and "BSSID" in joined and "offset" in joined
    assert "TX(AP) 좌표" in joined


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {name}\n      {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}\n      {type(exc).__name__}: {exc}")
    print(f"\n{'모두 통과' if not failures else f'{failures}건 실패'}")
    sys.exit(1 if failures else 0)
