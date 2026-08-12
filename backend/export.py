"""CSV Export 및 그래픽스 전달 산출물 (최종 실험: Run/TestSegment 모델).

핵심: MQTT 저장 단계에서 기록창 안의 C1~C4 에 이미 그 Test 의 segment_id 가 붙어 있다.
따라서 '각 Test 와 동일 시간대의 C1~C4'(§3.4)는 segment_id 로 그룹핑하면 그대로 얻어진다.

산출물:
  raw/measurements_raw.csv                전체 시계열 (+ run_id/segment_id/direction/pass_index)
  processed/test_points.csv               TestSegment 대표값 (정/역방향·재측정 별도 행)
  processed/calibration_by_test_window.csv 각 Test 와 동일 시간대 C1~C4
  processed/calibration_points.csv        Run 전체 Calibration 진단용
  processed/measurements_summary.csv       위치·노드별 대표값(전체)
  config/runs.json, test_segments.json, device_offsets.json, points.csv, tx_rx.json
  qc_report.json, README.md
"""

from __future__ import annotations

import csv
import json
import logging
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .store import ExperimentStore

logger = logging.getLogger(__name__)

RAW_COLUMNS = [
    "experiment_id", "run_id", "segment_id", "direction", "pass_index",
    "session_id", "point_id", "point_role", "node_id",
    "timestamp", "server_ts_ms", "seq",
    "rssi_raw_dbm", "rssi_filtered_dbm", "sample_count", "error_flags",
    "device_offset_db", "pos_x", "pos_y", "pos_z",
    "ap_bssid", "ap_channel", "valid", "invalid_reason",
]

SUMMARY_COLUMNS = [
    "point_id", "point_role", "node_id", "x", "y", "z",
    "sample_count", "median_raw", "median_filtered", "mean_filtered",
    "std_filtered", "min_filtered", "max_filtered", "iqr_filtered",
    "device_offset_db", "corrected_rssi",
]

TEST_POINT_COLUMNS = [
    "run_id", "direction", "pass_index", "segment_id", "point_id", "attempt_index",
    "recording_started_at_ms", "recording_ended_at_ms",
    "node_id", "sample_count", "median_filtered", "device_offset_db", "corrected_rssi",
    "x", "y", "z",
]

CAL_WINDOW_COLUMNS = [
    "run_id", "direction", "pass_index", "segment_id", "test_point_id",
    "calibration_point_id", "node_id", "window_started_at_ms", "window_ended_at_ms",
    "sample_count", "median_filtered", "device_offset_db", "corrected_rssi", "x", "y", "z",
]


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = int(pos)
    high = min(low + 1, len(ordered) - 1)
    frac = pos - low
    return ordered[low] * (1 - frac) + ordered[high] * frac


def _valid_filtered(items: list[dict[str, Any]]) -> list[float]:
    return [float(i["rssi_filtered_dbm"]) for i in items
            if i["valid"] == 1 and i["rssi_filtered_dbm"] is not None]


def _repr(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """유효 Filtered RSSI 대표값. corrected = median_filtered + device_offset_db."""
    filtered = _valid_filtered(items)
    if not filtered:
        return None
    first = items[0]
    offset = first.get("device_offset_db")
    median_filtered = statistics.median(filtered)
    corrected = median_filtered + float(offset) if offset is not None else None
    return {
        "sample_count": len(filtered),
        "median_filtered": round(median_filtered, 3),
        "device_offset_db": round(float(offset), 3) if offset is not None else None,
        "corrected_rssi": round(corrected, 3) if corrected is not None else None,
        "x": first.get("pos_x"), "y": first.get("pos_y"), "z": first.get("pos_z"),
    }


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """(point_id, node_id) 대표값(전체). 진단·호환용 measurements_summary."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        if r["valid"] != 1:
            continue
        groups.setdefault((r["point_id"], r["node_id"]), []).append(r)

    out: list[dict[str, Any]] = []
    for (point_id, node_id), items in sorted(groups.items()):
        filtered = _valid_filtered(items)
        raw = [float(i["rssi_raw_dbm"]) for i in items if i["rssi_raw_dbm"] is not None]
        if not filtered:
            continue
        first = items[0]
        offset = first.get("device_offset_db")
        median_filtered = statistics.median(filtered)
        corrected = median_filtered + float(offset) if offset is not None else None
        out.append({
            "point_id": point_id, "point_role": first["point_role"], "node_id": node_id,
            "x": first.get("pos_x"), "y": first.get("pos_y"), "z": first.get("pos_z"),
            "sample_count": len(filtered),
            "median_raw": round(statistics.median(raw), 3) if raw else None,
            "median_filtered": round(median_filtered, 3),
            "mean_filtered": round(statistics.fmean(filtered), 3),
            "std_filtered": round(statistics.stdev(filtered), 3) if len(filtered) > 1 else 0.0,
            "min_filtered": round(min(filtered), 3), "max_filtered": round(max(filtered), 3),
            "iqr_filtered": round(_quantile(filtered, 0.75) - _quantile(filtered, 0.25), 3),
            "device_offset_db": round(float(offset), 3) if offset is not None else None,
            "corrected_rssi": round(corrected, 3) if corrected is not None else None,
        })
    return out


def _completed_segment_ids(segments: list[dict[str, Any]]) -> set[str]:
    return {s["segment_id"] for s in segments
            if s["status"] == "completed" and not s["superseded"]}


def summarize_test_points(rows, segments) -> list[dict[str, Any]]:
    """TestSegment 대표값. 폐기·미완료 Segment 는 제외. 정/역방향·재측정은 별도 행."""
    keep = _completed_segment_ids(segments)
    seg_meta = {s["segment_id"]: s for s in segments}
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if r["point_role"] == "test" and r["segment_id"] in keep:
            groups.setdefault(r["segment_id"], []).append(r)

    out = []
    for seg_id, items in groups.items():
        rep = _repr(items)
        if rep is None:
            continue
        s = seg_meta[seg_id]
        first = items[0]
        out.append({
            "run_id": s["run_id"], "direction": first.get("direction"),
            "pass_index": first.get("pass_index"), "segment_id": seg_id,
            "point_id": s["point_id"], "attempt_index": s["attempt_index"],
            "recording_started_at_ms": s["recording_started_at_ms"],
            "recording_ended_at_ms": s["recording_ended_at_ms"],
            "node_id": first["node_id"], **rep,
        })
    return sorted(out, key=lambda d: (str(d["run_id"]), d["recording_started_at_ms"]))


def summarize_calibration_by_window(rows, segments) -> list[dict[str, Any]]:
    """각 정상 TestSegment 와 동일 시간대의 C1~C4 (segment_id 로 그룹핑)."""
    keep = _completed_segment_ids(segments)
    seg_meta = {s["segment_id"]: s for s in segments}
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        if r["point_role"] == "calibration" and r["segment_id"] in keep:
            groups.setdefault((r["segment_id"], r["node_id"]), []).append(r)

    out = []
    for (seg_id, node_id), items in groups.items():
        rep = _repr(items)
        if rep is None:
            continue
        s = seg_meta[seg_id]
        first = items[0]
        out.append({
            "run_id": s["run_id"], "direction": first.get("direction"),
            "pass_index": first.get("pass_index"), "segment_id": seg_id,
            "test_point_id": s["point_id"], "calibration_point_id": first["point_id"],
            "node_id": node_id,
            "window_started_at_ms": s["recording_started_at_ms"],
            "window_ended_at_ms": s["recording_ended_at_ms"], **rep,
        })
    return sorted(out, key=lambda d: (str(d["run_id"]), d["window_started_at_ms"],
                                      str(d["calibration_point_id"])))


def summarize_calibration_points(rows) -> list[dict[str, Any]]:
    """Run 전체 Calibration 통계(진단용, Test별 동시간 아님). 이동+기록 구간 모두 포함."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        if r["point_role"] == "calibration":
            groups.setdefault((r.get("run_id"), r["node_id"]), []).append(r)
    out = []
    for (run_id, node_id), items in sorted(groups.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])):
        rep = _repr(items)
        if rep is None:
            continue
        out.append({"run_id": run_id, "calibration_point_id": items[0]["point_id"],
                    "node_id": node_id, **rep})
    return out


def offset_drift(pre_offsets, post_offsets) -> list[dict[str, Any]]:
    """node 별 pre/post device_offset_db 와 drift(post-pre). 임의 임계값을 만들지 않는다."""
    pre = {o["node_id"]: o["device_offset_db"] for o in pre_offsets}
    post = {o["node_id"]: o["device_offset_db"] for o in post_offsets}
    out = []
    for node in sorted(set(pre) | set(post)):
        pv, qv = pre.get(node), post.get(node)
        drift = round(qv - pv, 3) if (pv is not None and qv is not None) else None
        out.append({"node_id": node, "pre_device_offset_db": pv,
                    "post_device_offset_db": qv, "device_offset_drift_db": drift})
    return out


def quality_check(rows, test_points, cal_window, pre_offsets, post_offsets, points, tx_list,
                  runs, segments, expected_test_points, expected_calibration_nodes,
                  expected_samples) -> dict[str, Any]:
    """Run 별 검사(§14). Run 이 30분을 넘었다는 이유로 경고/실패시키지 않는다."""
    problems: list[str] = []
    warnings: list[str] = []

    if not pre_offsets:
        problems.append("사전(pre) 장치 offset 이 계산되지 않았습니다.")
    if not post_offsets:
        warnings.append("사후(post) 장치 offset 측정이 없습니다 — drift 를 확인할 수 없습니다.")
    # drift 는 기록만 하고 임의 합격 임계값으로 실패시키지 않는다.
    drift = offset_drift(pre_offsets, post_offsets)

    # 본 실험 Run 만 검사한다(Offset Run 은 direction='offset' 이라 제외).
    active_runs = [r for r in runs if r["direction"] in ("forward", "reverse")
                   and r["status"] in ("running", "completed")]
    for run in active_runs:
        rid = run["run_id"]
        segs = [s for s in segments if s["run_id"] == rid
                and s["status"] == "completed" and not s["superseded"]]
        pts = [s["point_id"] for s in sorted(segs, key=lambda s: s["order_index"])]
        if len(pts) != expected_test_points:
            warnings.append(f"[{rid}] Test 위치 {len(pts)}개 (기대 {expected_test_points})")
        # 방향 순서
        nums = [int(p[1:]) for p in pts if p[1:].isdigit()]
        if run["direction"] == "forward" and nums != sorted(nums):
            warnings.append(f"[{rid}] 정방향 순서가 오름차순이 아님: {pts}")
        if run["direction"] == "reverse" and nums != sorted(nums, reverse=True):
            warnings.append(f"[{rid}] 역방향 순서가 내림차순이 아님: {pts}")
        # 시간 겹침
        spans = sorted((s["recording_started_at_ms"], s["recording_ended_at_ms"]) for s in segs)
        for a, b in zip(spans, spans[1:]):
            if b[0] < a[1]:
                problems.append(f"[{rid}] TestSegment 시간이 겹칩니다.")
                break
        # 각 Segment 가 Run 시간 안에 있는가
        for s in segs:
            if run["ended_at_ms"] and s["recording_ended_at_ms"] > run["ended_at_ms"]:
                warnings.append(f"[{rid}] Segment {s['segment_id']} 가 Run 종료 후까지 걸침")

    # 각 완료 Segment 에 T 와 C1~C4 가 존재하는가
    tp_by_seg = {t["segment_id"] for t in test_points}
    cal_nodes_by_seg: dict[str, set[str]] = {}
    for c in cal_window:
        cal_nodes_by_seg.setdefault(c["segment_id"], set()).add(c["node_id"])
    for s in segments:
        if s["status"] != "completed" or s["superseded"]:
            continue
        if s["segment_id"] not in tp_by_seg:
            problems.append(f"Segment {s['segment_id']}({s['point_id']}) 에 T 데이터가 없습니다.")
        if len(cal_nodes_by_seg.get(s["segment_id"], set())) < expected_calibration_nodes:
            warnings.append(f"Segment {s['segment_id']}({s['point_id']}) 에 C1~C4 일부가 없습니다.")

    # 좌표·BSSID
    missing_coords = sorted({t["point_id"] for t in test_points if t["x"] is None})
    if missing_coords:
        problems.append(f"좌표 미등록 Test 위치: {', '.join(missing_coords)}")
    if not tx_list:
        problems.append("TX(AP) 좌표가 등록되지 않았습니다.")
    bssids = {r["ap_bssid"] for r in rows if r["ap_bssid"]}
    if len(bssids) > 1:
        problems.append(f"여러 BSSID 가 섞였습니다: {', '.join(sorted(bssids))}")

    # 샘플 부족
    low = [f'{t["point_id"]}={t["sample_count"]}' for t in test_points
           if t["sample_count"] < expected_samples * 0.6]
    if low:
        warnings.append(f"샘플 수 부족(기대 {expected_samples}의 60% 미만): {', '.join(low)}")

    return {
        "ok": not problems, "problems": problems, "warnings": warnings,
        "offset_drift": drift,
        "counts": {"raw_rows": len(rows), "runs": len(runs),
                   "completed_segments": len([s for s in segments
                                              if s["status"] == "completed" and not s["superseded"]]),
                   "test_points": len(test_points), "calibration_windows": len(cal_window)},
    }


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fp:
        w = csv.DictWriter(fp, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def export_experiment(store: ExperimentStore, experiment_id: str,
                      out_root: str | Path, expected_samples: int = 120,
                      expected_test_points: int = 10,
                      expected_calibration_nodes: int = 4) -> dict[str, Any]:
    root = Path(out_root) / experiment_id
    (root / "raw").mkdir(parents=True, exist_ok=True)
    (root / "processed").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)

    rows = store.measurements_for_export(experiment_id)
    points = store.list_points(experiment_id)
    tx_list = store.list_tx(experiment_id)
    runs = store.list_runs(experiment_id)
    segments = [s for r in runs for s in store.list_test_segments(r["run_id"])]

    # 사전/사후 Offset 분리 (사후는 재보정 안 함, drift 확인용)
    pre_run = store.latest_offset_run(experiment_id, "pre")
    post_run = store.latest_offset_run(experiment_id, "post")
    pre_offsets = store.list_device_offsets(experiment_id, pre_run["offset_run_id"]) if pre_run else []
    post_offsets = store.list_device_offsets(experiment_id, post_run["offset_run_id"]) if post_run else []

    summary = summarize(rows)
    test_points = summarize_test_points(rows, segments)
    cal_window = summarize_calibration_by_window(rows, segments)
    cal_points = summarize_calibration_points(rows)

    # raw
    raw_rows = []
    for r in rows:
        item = dict(r)
        item["timestamp"] = r.get("node_ts_ms")
        raw_rows.append(item)
    _write_csv(root / "raw" / "measurements_raw.csv", RAW_COLUMNS, raw_rows)

    # processed
    _write_csv(root / "processed" / "measurements_summary.csv", SUMMARY_COLUMNS, summary)
    _write_csv(root / "processed" / "test_points.csv", TEST_POINT_COLUMNS, test_points)
    _write_csv(root / "processed" / "calibration_by_test_window.csv", CAL_WINDOW_COLUMNS, cal_window)
    _write_csv(root / "processed" / "calibration_points.csv",
               ["run_id", "calibration_point_id", "node_id", "sample_count",
                "median_filtered", "device_offset_db", "corrected_rssi", "x", "y", "z"], cal_points)

    # config
    _write_csv(root / "config" / "points.csv",
               ["point_id", "point_role", "pos_x", "pos_y", "pos_z", "note"], points)
    (root / "config" / "device_offsets.json").write_text(
        json.dumps({"experiment_id": experiment_id,
                    "pre_offset_run_id": pre_run["offset_run_id"] if pre_run else None,
                    "post_offset_run_id": post_run["offset_run_id"] if post_run else None,
                    "pre": pre_offsets, "post": post_offsets,
                    "drift": offset_drift(pre_offsets, post_offsets)},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "config" / "runs.json").write_text(
        json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "config" / "test_segments.json").write_text(
        json.dumps(segments, ensure_ascii=False, indent=2), encoding="utf-8")
    tx_rx = {
        "tx": [{"id": t["tx_id"], "position": [t["pos_x"], t["pos_y"], t["pos_z"]],
                "frequency_hz": t["frequency_hz"], "ap_bssid": t["ap_bssid"],
                "ap_channel": t["ap_channel"]} for t in tx_list],
        "rx": [{"id": p["point_id"], "role": p["point_role"],
                "position": [p["pos_x"], p["pos_y"], p["pos_z"]]}
               for p in points if p["point_role"] in ("calibration", "test")],
    }
    (root / "config" / "tx_rx.json").write_text(
        json.dumps(tx_rx, ensure_ascii=False, indent=2), encoding="utf-8")

    qc = quality_check(rows, test_points, cal_window, pre_offsets, post_offsets, points, tx_list,
                       runs, segments, expected_test_points, expected_calibration_nodes, expected_samples)
    (root / "qc_report.json").write_text(
        json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8")
    (root / "README.md").write_text(_readme(experiment_id, qc, runs), encoding="utf-8")

    logger.info("exported experiment %s -> %s (ok=%s)", experiment_id, root, qc["ok"])
    return {
        "experiment_id": experiment_id, "path": str(root),
        "files": {
            "raw": str(root / "raw" / "measurements_raw.csv"),
            "summary": str(root / "processed" / "measurements_summary.csv"),
            "test": str(root / "processed" / "test_points.csv"),
            "calibration": str(root / "processed" / "calibration_points.csv"),
            "calibration_by_test_window": str(root / "processed" / "calibration_by_test_window.csv"),
            "tx_rx": str(root / "config" / "tx_rx.json"),
            "device_offsets": str(root / "config" / "device_offsets.json"),
        },
        "qc": qc,
    }


def _readme(experiment_id: str, qc: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    problems = "\n".join(f"- {p}" for p in qc["problems"]) or "- 없음"
    warnings = "\n".join(f"- {w}" for w in qc["warnings"]) or "- 없음"
    run_lines = "\n".join(
        f"- {r['run_id']} ({r['direction']}, pass {r['pass_index']}, {r['status']})" for r in runs
    ) or "- 없음"
    return f"""# {experiment_id}

네트워크·백엔드 파트가 생성한 최종 실험 산출물.
생성 시각: {datetime.now(timezone.utc).isoformat()}

## Run 목록
{run_lines}

## 그래픽스 파트가 쓸 파일

| 파일 | 용도 |
|---|---|
| `processed/test_points.csv` | TestSegment 대표값(정/역방향·재측정 별도 행). MAE·RMSE 평가 전용 |
| `processed/calibration_by_test_window.csv` | 각 Test 와 **동일 시간 범위**의 C1~C4. IDW/Residual 입력 |
| `processed/calibration_points.csv` | Run 전체 Calibration 진단용(Test별 동시간 비교용 아님) |
| `config/tx_rx.json` | TX(AP)·RX 좌표 |
| `config/device_offsets.json` | 장치별 RSSI 보정값 |
| `config/runs.json`, `test_segments.json` | Run·Segment 메타 |
| `raw/measurements_raw.csv` | 전체 시계열(run_id/segment_id 포함) |

## 사용할 RSSI 값
`corrected_rssi` = Filtered RSSI 중앙값 + `device_offset_db`

## 주의
- **`test_points.csv` 는 평가에만** 사용한다. 보정(IDW) 입력에 넣지 않는다.
- **calibration_by_test_window.csv** 를 Test별 동시간 비교에 쓴다.
  `calibration_points.csv`(전체 Run 평균)를 모든 Test 에 공통 적용하지 않는다.
- `segment_id` 로 `test_points.csv` 와 `calibration_by_test_window.csv` 를 연결한다.
- 정방향 T1 과 역방향 T1 은 서로 다른 `run_id` 의 별도 행이다.

## 품질 점검
상태: {"통과" if qc["ok"] else "문제 있음"}

### 문제
{problems}

### 경고
{warnings}

### 집계
```json
{json.dumps(qc["counts"], ensure_ascii=False, indent=2)}
```
"""


def import_points_csv(store: ExperimentStore, experiment_id: str,
                      csv_text: str, updated_at_ms: int) -> dict[str, Any]:
    """종이 좌표 일괄 등록. 헤더: point_id, point_role, pos_x/y/z, note (x/y/z 도 인식)."""
    reader = csv.DictReader(csv_text.lstrip("﻿").splitlines())
    imported, errors = [], []

    def pick(row: dict[str, str], *names: str) -> str | None:
        for n in names:
            if row.get(n) not in (None, ""):
                return row[n]
        return None

    for lineno, row in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in row.items()}
        point_id = pick(row, "point_id", "id")
        if not point_id:
            continue
        try:
            x = pick(row, "pos_x", "x"); y = pick(row, "pos_y", "y"); z = pick(row, "pos_z", "z")
            store.upsert_point(
                experiment_id, point_id, pick(row, "point_role", "role"),
                float(x) if x is not None else None,
                float(y) if y is not None else None,
                float(z) if z is not None else None,
                pick(row, "note"), updated_at_ms)
            imported.append(point_id)
        except ValueError as exc:
            errors.append(f"{lineno}행 ({point_id}): 좌표를 숫자로 읽을 수 없음 - {exc}")

    return {"imported": imported, "count": len(imported), "errors": errors}
