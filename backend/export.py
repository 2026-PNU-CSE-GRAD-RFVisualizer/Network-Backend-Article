"""CSV Export 및 그래픽스 전달 산출물 생성 (계획서 §7.3, §10 디렉터리 구조).

그래픽스 파트는 이 디렉터리만 받아 바로 IDW / Residual IDW 를 돌릴 수 있어야 한다.
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
    "experiment_id", "session_id", "point_id", "point_role", "node_id",
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


def _iso(ms: int | None) -> str:
    if ms is None:
        return ""
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _quantile(values: list[float], q: float) -> float:
    """선형 보간 분위수 (numpy 의존 없음)."""
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


def summarize(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """(point_id, node_id) 대표값. corrected_rssi = median_filtered + device_offset_db (§3.3)."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        if r["valid"] != 1:
            continue
        groups.setdefault((r["point_id"], r["node_id"]), []).append(r)

    summary: list[dict[str, Any]] = []
    for (point_id, node_id), items in sorted(groups.items()):
        filtered = [float(i["rssi_filtered_dbm"]) for i in items
                    if i["rssi_filtered_dbm"] is not None]
        raw = [float(i["rssi_raw_dbm"]) for i in items if i["rssi_raw_dbm"] is not None]
        if not filtered:
            continue
        first = items[0]
        offset = first.get("device_offset_db")
        median_filtered = statistics.median(filtered)
        corrected = median_filtered + float(offset) if offset is not None else None

        summary.append({
            "point_id": point_id,
            "point_role": first["point_role"],
            "node_id": node_id,
            "x": first.get("pos_x"),
            "y": first.get("pos_y"),
            "z": first.get("pos_z"),
            "sample_count": len(filtered),
            "median_raw": round(statistics.median(raw), 3) if raw else None,
            "median_filtered": round(median_filtered, 3),
            "mean_filtered": round(statistics.fmean(filtered), 3),
            "std_filtered": round(statistics.stdev(filtered), 3) if len(filtered) > 1 else 0.0,
            "min_filtered": round(min(filtered), 3),
            "max_filtered": round(max(filtered), 3),
            "iqr_filtered": round(_quantile(filtered, 0.75) - _quantile(filtered, 0.25), 3),
            "device_offset_db": round(float(offset), 3) if offset is not None else None,
            "corrected_rssi": round(corrected, 3) if corrected is not None else None,
        })
    return summary


def quality_check(rows: list[dict[str, Any]], summary: list[dict[str, Any]],
                  expected_samples: int, offsets: list[dict[str, Any]],
                  points: list[dict[str, Any]],
                  tx_list: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """현장을 떠나기 전 확인할 항목을 기계적으로 점검한다."""
    problems: list[str] = []
    warnings: list[str] = []

    low_sample = [
        f'{s["point_id"]}/{s["node_id"]}={s["sample_count"]}'
        for s in summary if s["sample_count"] < expected_samples * 0.6
    ]
    if low_sample:
        problems.append(f"샘플 수 부족(기대 {expected_samples}개의 60% 미만): {', '.join(low_sample)}")

    missing_coords = sorted({s["point_id"] for s in summary if s["x"] is None})
    if missing_coords:
        problems.append(f"좌표 미등록 위치: {', '.join(missing_coords)}")

    if not offsets:
        problems.append("장치 offset 이 계산되지 않았습니다. offset 세션을 확인하세요.")
    else:
        no_offset = sorted({s["node_id"] for s in summary if s["device_offset_db"] is None})
        if no_offset:
            problems.append(f"offset 값이 없는 노드: {', '.join(no_offset)}")
        # 한 자리에서 동시 측정했다면 노드별 샘플 수가 비슷해야 한다.
        counts = [o["sample_count"] for o in offsets if o["sample_count"]]
        if counts and max(counts) > min(counts) * 1.5:
            warnings.append(
                f"offset 측정 샘플 수가 노드마다 크게 다릅니다 {counts} — "
                "일부 노드가 다른 시간대의 데이터를 포함했을 수 있습니다.")

    if not tx_list:
        problems.append("TX(AP) 좌표가 등록되지 않았습니다. Sionna RT 를 실행할 수 없습니다.")
    elif any(t.get("pos_x") is None for t in tx_list):
        problems.append("TX 좌표에 빈 값이 있습니다.")

    roles = {}
    for s in summary:
        roles.setdefault(s["point_role"], set()).add(s["point_id"])
    n_cal = len(roles.get("calibration", ()))
    n_test = len(roles.get("test", ()))
    if n_cal != 4:
        warnings.append(f"보정 위치가 4개가 아닙니다 (현재 {n_cal}개)")
    if n_test != 15:
        warnings.append(f"Test 위치가 15개가 아닙니다 (현재 {n_test}개)")

    overlap = roles.get("calibration", set()) & roles.get("test", set())
    if overlap:
        problems.append(f"보정 위치와 Test 위치가 겹칩니다: {', '.join(sorted(overlap))}")

    bssids = {r["ap_bssid"] for r in rows if r["ap_bssid"]}
    if len(bssids) > 1:
        problems.append(f"여러 BSSID 가 섞였습니다: {', '.join(sorted(bssids))}")

    invalid = [r for r in rows if r["valid"] != 1]
    if invalid:
        warnings.append(f"유효하지 않은 샘플 {len(invalid)}건 (원본은 보존됨)")

    high_std = [f'{s["point_id"]}/{s["node_id"]}={s["std_filtered"]}'
                for s in summary if s["std_filtered"] > 5.0]
    if high_std:
        warnings.append(f"변동성이 큰 측정(std>5dB): {', '.join(high_std)}")

    return {
        "ok": not problems,
        "problems": problems,
        "warnings": warnings,
        "counts": {
            "raw_rows": len(rows),
            "summary_rows": len(summary),
            "calibration_points": n_cal,
            "test_points": n_test,
            "registered_points": len(points),
        },
    }


def export_experiment(store: ExperimentStore, experiment_id: str,
                      out_root: str | Path, expected_samples: int = 30) -> dict[str, Any]:
    """계획서 §10 구조로 실험 산출물 디렉터리를 생성한다."""
    root = Path(out_root) / experiment_id
    (root / "raw").mkdir(parents=True, exist_ok=True)
    (root / "processed").mkdir(parents=True, exist_ok=True)
    (root / "config").mkdir(parents=True, exist_ok=True)

    rows = store.measurements_for_export(experiment_id)
    points = store.list_points(experiment_id)
    offsets = store.list_device_offsets(experiment_id)
    tx_list = store.list_tx(experiment_id)
    sessions = store.list_sessions(experiment_id)
    summary = summarize(rows)

    raw_path = root / "raw" / "measurements_raw.csv"
    with raw_path.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=RAW_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            item = dict(r)
            item["timestamp"] = r.get("node_ts_ms")
            writer.writerow(item)

    summary_path = root / "processed" / "measurements_summary.csv"
    with summary_path.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=SUMMARY_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(summary)

    # 역할별 분리: 그래픽스가 필요한 역할만 골라 쓰게 한다.
    # calibration_points.csv, test_points.csv 는 기존 이름 유지(하위 호환).
    role_files = {
        "offset": "offset_points.csv",
        "calibration": "calibration_points.csv",
        "test": "test_points.csv",
    }
    for role, name in role_files.items():
        path = root / "processed" / name
        with path.open("w", newline="", encoding="utf-8-sig") as fp:
            writer = csv.DictWriter(fp, fieldnames=SUMMARY_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows([s for s in summary if s["point_role"] == role])

    # 원본 시계열도 역할별로 raw/by_role/ 에 분리 저장.
    by_role_dir = root / "raw" / "by_role"
    by_role_dir.mkdir(parents=True, exist_ok=True)
    for role in role_files:
        path = by_role_dir / f"measurements_raw_{role}.csv"
        with path.open("w", newline="", encoding="utf-8-sig") as fp:
            writer = csv.DictWriter(fp, fieldnames=RAW_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for r in rows:
                if r["point_role"] != role:
                    continue
                item = dict(r)
                item["timestamp"] = r.get("node_ts_ms")
                writer.writerow(item)

    points_path = root / "config" / "points.csv"
    with points_path.open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(
            fp, fieldnames=["point_id", "point_role", "pos_x", "pos_y", "pos_z", "note"],
            extrasaction="ignore")
        writer.writeheader()
        writer.writerows(points)

    (root / "config" / "device_offsets.json").write_text(
        json.dumps({"experiment_id": experiment_id, "nodes": offsets},
                   ensure_ascii=False, indent=2), encoding="utf-8")

    tx_rx = {
        "tx": [
            {
                "id": t["tx_id"],
                "position": [t["pos_x"], t["pos_y"], t["pos_z"]],
                "frequency_hz": t["frequency_hz"],
                "ap_bssid": t["ap_bssid"],
                "ap_channel": t["ap_channel"],
            }
            for t in tx_list
        ],
        "rx": [
            {
                "id": p["point_id"],
                "role": p["point_role"],
                "position": [p["pos_x"], p["pos_y"], p["pos_z"]],
            }
            for p in points if p["point_role"] in ("calibration", "test")
        ],
    }
    (root / "config" / "tx_rx.json").write_text(
        json.dumps(tx_rx, ensure_ascii=False, indent=2), encoding="utf-8")

    (root / "config" / "sessions.json").write_text(
        json.dumps(sessions, ensure_ascii=False, indent=2), encoding="utf-8")

    qc = quality_check(rows, summary, expected_samples, offsets, points, tx_list)
    (root / "qc_report.json").write_text(
        json.dumps(qc, ensure_ascii=False, indent=2), encoding="utf-8")

    (root / "README.md").write_text(_readme(experiment_id, qc, summary), encoding="utf-8")

    logger.info("exported experiment %s -> %s (ok=%s)", experiment_id, root, qc["ok"])
    return {
        "experiment_id": experiment_id,
        "path": str(root),
        "files": {
            "raw": str(raw_path),
            "summary": str(summary_path),
            "offset": str(root / "processed" / "offset_points.csv"),
            "calibration": str(root / "processed" / "calibration_points.csv"),
            "test": str(root / "processed" / "test_points.csv"),
            "tx_rx": str(root / "config" / "tx_rx.json"),
            "device_offsets": str(root / "config" / "device_offsets.json"),
        },
        "qc": qc,
    }


def _readme(experiment_id: str, qc: dict[str, Any], summary: list[dict[str, Any]]) -> str:
    cal = sorted({s["point_id"] for s in summary if s["point_role"] == "calibration"})
    test = sorted({s["point_id"] for s in summary if s["point_role"] == "test"})
    problems = "\n".join(f"- {p}" for p in qc["problems"]) or "- 없음"
    warnings = "\n".join(f"- {w}" for w in qc["warnings"]) or "- 없음"
    return f"""# {experiment_id}

네트워크·백엔드 파트가 생성한 측정 산출물.
생성 시각: {datetime.now(timezone.utc).isoformat()}

## 그래픽스 파트가 쓸 파일

| 파일 | 용도 |
|---|---|
| `processed/calibration_points.csv` | 보정 위치 {len(cal)}개. IDW / Residual IDW 입력 |
| `processed/test_points.csv` | Test 위치 {len(test)}개. MAE·RMSE 평가 전용 |
| `processed/measurements_summary.csv` | 전체 위치 대표값 |
| `config/tx_rx.json` | TX(AP) 및 RX 좌표 |
| `config/device_offsets.json` | 장치별 RSSI 보정값 |
| `raw/measurements_raw.csv` | 전체 시계열 원본 |

## 사용할 RSSI 값

`corrected_rssi` 열을 사용한다.
= Filtered RSSI 의 30초 중앙값 + 해당 장치의 `device_offset_db`

`median_raw` 는 검증용으로만 보존되어 있으며 기본 실험값이 아니다.

## 주의

**`test_points.csv` 는 평가에만 사용한다.**
IDW / Residual IDW 생성이나 파라미터(p 등) 선택에 절대 넣지 않는다.
p 를 고르려면 `calibration_points.csv` 안에서 Leave-One-Out 으로만 선택한다.

## 좌표계

출입문 왼쪽 아래 바닥점이 원점.
+X = 오른쪽 벽 방향, +Y = 강의실 안쪽 깊이, +Z = 위쪽. 단위는 m.

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
    """종이에 적어온 좌표를 일괄 등록. 헤더: point_id, point_role, pos_x/y/z, note (x/y/z 도 인식)."""
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
            x = pick(row, "pos_x", "x")
            y = pick(row, "pos_y", "y")
            z = pick(row, "pos_z", "z")
            store.upsert_point(
                experiment_id, point_id, pick(row, "point_role", "role"),
                float(x) if x is not None else None,
                float(y) if y is not None else None,
                float(z) if z is not None else None,
                pick(row, "note"), updated_at_ms,
            )
            imported.append(point_id)
        except ValueError as exc:
            errors.append(f"{lineno}행 ({point_id}): 좌표를 숫자로 읽을 수 없음 - {exc}")

    return {"imported": imported, "count": len(imported), "errors": errors}
