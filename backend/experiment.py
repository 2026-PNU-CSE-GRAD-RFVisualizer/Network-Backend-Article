"""실험 상태 관리자 + 장치 offset 계산 (최종 실험 로직).

단일 세션이 아니라 두 상태를 분리해 관리한다:
    _active_run          : 본 실험 회차. C1~C4 가 끊기지 않고 기록된다(자동 종료 없음).
    _active_test_segment : 이동 센서 T 의 2분 기록 구간(안정화 20s → 기록 120s → 완료).

Offset 측정은 본 실험 Run 과 분리된 OffsetRun 으로 관리한다.
샘플이 어느 Segment 에 속하는지는 in-memory 포인터가 아니라 저장된 시간 범위로 판정한다
(MQTT 지연이 있어도 server_ts_ms 로 올바른 Segment 에 저장되도록).
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any

from .store import ExperimentStore

logger = logging.getLogger(__name__)

VALID_ROLES = ("offset", "calibration", "test")
DIRECTIONS = ("forward", "reverse")


class Conflict(Exception):
    """상태 위반 요청(중복 시작, 순서 위반 등). API 는 409 로 매핑한다."""


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class ActiveOffsetRun:
    offset_run_id: str
    experiment_id: str
    phase: str            # pre | post
    started_at_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ActiveRun:
    run_id: str
    experiment_id: str
    direction: str
    pass_index: int
    started_at_ms: int

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["elapsed_ms"] = now_ms() - self.started_at_ms
        return d


@dataclass
class ActiveTestSegment:
    segment_id: str
    run_id: str
    point_id: str
    order_index: int
    attempt_index: int
    prepared_at_ms: int
    recording_started_at_ms: int
    recording_ended_at_ms: int

    def state_at(self, current_ms: int) -> str:
        if current_ms < self.recording_started_at_ms:
            return "stabilizing"
        if current_ms < self.recording_ended_at_ms:
            return "recording"
        return "completed"

    def to_dict(self, current_ms: int | None = None) -> dict[str, Any]:
        cur = current_ms or now_ms()
        d = asdict(self)
        d["state"] = self.state_at(cur)
        d["remaining_stabilization_ms"] = max(0, self.recording_started_at_ms - cur)
        d["remaining_recording_ms"] = max(0, self.recording_ended_at_ms - cur)
        return d


class ExperimentManager:
    """Run + TestSegment + OffsetRun 상태. MQTT 스레드와 HTTP 핸들러가 공유하므로 락으로 보호."""

    def __init__(self, store: ExperimentStore) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._experiment_id: str | None = None
        self._ap_bssid: str | None = None
        self._ap_channel: int | None = None
        self._assignments: dict[str, tuple[str, str]] = {}  # node_id -> (point_id, role)
        self._active_run: ActiveRun | None = None
        self._active_test_segment: ActiveTestSegment | None = None
        self._active_offset: ActiveOffsetRun | None = None

    # -- experiment ----------------------------------------------------
    @property
    def experiment_id(self) -> str | None:
        return self._experiment_id

    @property
    def ap_bssid(self) -> str | None:
        return self._ap_bssid

    @property
    def ap_channel(self) -> int | None:
        return self._ap_channel

    def start_experiment(self, experiment_id: str, ap_bssid: str | None,
                         ap_channel: int | None, note: str | None = None) -> dict[str, Any]:
        self.store.create_experiment(experiment_id, now_ms(), ap_bssid, ap_channel, note)
        restored = {
            a["node_id"]: (a["point_id"], a["point_role"])
            for a in self.store.list_assignments(experiment_id)
        }
        with self._lock:
            self._experiment_id = experiment_id
            self._ap_bssid = ap_bssid
            self._ap_channel = ap_channel
            self._active_run = None
            self._active_test_segment = None
            self._active_offset = None
            self._assignments = restored
        logger.info("experiment started: %s (%d assignments restored)", experiment_id, len(restored))
        return {"experiment_id": experiment_id, "assignments_restored": len(restored)}

    def _require_experiment(self) -> str:
        if self._experiment_id is None:
            raise Conflict("실험이 시작되지 않았습니다.")
        return self._experiment_id

    # -- 노드 배치 ------------------------------------------------------
    def assign(self, node_id: str, point_id: str, point_role: str) -> dict[str, Any]:
        exp = self._require_experiment()
        if point_role not in VALID_ROLES:
            raise ValueError(f"point_role 은 {VALID_ROLES} 중 하나여야 합니다.")
        node_id, point_id = node_id.strip(), point_id.strip()
        if not node_id or not point_id:
            raise ValueError("node_id 와 point_id 는 비어 있을 수 없습니다.")
        with self._lock:
            self._assignments[node_id] = (point_id, point_role)
        self.store.upsert_assignment(exp, node_id, point_id, point_role, now_ms())
        logger.info("assign %s -> %s (%s)", node_id, point_id, point_role)
        return {"node_id": node_id, "point_id": point_id, "point_role": point_role}

    def assignments(self) -> dict[str, tuple[str, str]]:
        with self._lock:
            return dict(self._assignments)

    def role_of(self, node_id: str) -> str | None:
        with self._lock:
            found = self._assignments.get(node_id)
        return found[1] if found else None

    def point_of(self, node_id: str) -> str | None:
        with self._lock:
            found = self._assignments.get(node_id)
        return found[0] if found else None

    # -- OffsetRun (사전/사후) -----------------------------------------
    def start_offset_run(self, phase: str = "pre", note: str | None = None) -> dict[str, Any]:
        exp = self._require_experiment()
        if phase not in ("pre", "post"):
            raise ValueError("phase 는 pre 또는 post 여야 합니다.")
        with self._lock:
            if self._active_run is not None:
                raise Conflict("본 실험 Run 중에는 Offset 측정을 시작할 수 없습니다.")
            if self._active_offset is not None:
                raise Conflict("이미 Offset 측정이 진행 중입니다.")
            offset_run_id = f"offset_{phase}__{now_ms()}"
            self._active_offset = ActiveOffsetRun(offset_run_id, exp, phase, now_ms())
        self.store.create_offset_run(offset_run_id, exp, phase, now_ms(), note)
        logger.info("offset run start: %s (%s)", offset_run_id, phase)
        return self._active_offset.to_dict()

    def stop_offset_run(self) -> dict[str, Any]:
        with self._lock:
            offset = self._active_offset
            self._active_offset = None
        if offset is None:
            return {"stopped": False}
        self.store.set_offset_run_status(offset.offset_run_id, "completed", now_ms())
        logger.info("offset run stop: %s", offset.offset_run_id)
        return {"stopped": True, "offset_run": offset.to_dict()}

    def active_offset_run(self) -> ActiveOffsetRun | None:
        with self._lock:
            return self._active_offset

    # -- 본 실험 Run ----------------------------------------------------
    def start_run(self, direction: str, pass_index: int,
                  pre_offset_run_id: str | None = None, note: str | None = None) -> dict[str, Any]:
        exp = self._require_experiment()
        if direction not in DIRECTIONS:
            raise ValueError(f"direction 은 {DIRECTIONS} 중 하나여야 합니다.")
        # 사전 Offset 이 지정되지 않으면 이 실험의 최신 완료된 pre OffsetRun 을 적용한다.
        if pre_offset_run_id is None:
            pre = self.store.latest_offset_run(exp, "pre")
            pre_offset_run_id = pre["offset_run_id"] if pre else None
        with self._lock:
            if self._active_offset is not None:
                raise Conflict("Offset 측정 중에는 본 실험 Run 을 시작할 수 없습니다.")
            if self._active_run is not None:
                raise Conflict("이미 활성 Run 이 있습니다. 먼저 종료하세요.")
            started = now_ms()
            run_id = f"{exp}__{direction}{pass_index}__{started}"
            run = ActiveRun(run_id, exp, direction, pass_index, started)
            self._active_run = run
            self._active_test_segment = None
        self.store.create_run(run_id, exp, direction, pass_index, started, pre_offset_run_id, note)
        logger.info("run start: %s (%s pass %d, pre_offset=%s)",
                    run_id, direction, pass_index, pre_offset_run_id)
        return run.to_dict()

    def attach_post_offset(self, post_offset_run_id: str, run_id: str | None = None) -> dict[str, Any]:
        """사후 OffsetRun 을 본 실험 Run 에 연결(재보정 아님, drift 확인용)."""
        exp = self._require_experiment()
        if run_id is None:
            runs = [r for r in self.store.list_runs(exp) if r["direction"] in DIRECTIONS]
            if not runs:
                raise Conflict("연결할 본 실험 Run 이 없습니다.")
            run_id = runs[-1]["run_id"]
        self.store.set_run_offset_refs(run_id, post_offset_run_id=post_offset_run_id)
        return {"run_id": run_id, "post_offset_run_id": post_offset_run_id}

    def active_run(self) -> ActiveRun | None:
        with self._lock:
            return self._active_run

    def end_run(self) -> dict[str, Any]:
        with self._lock:
            run = self._active_run
            if run is None:
                raise Conflict("활성 Run 이 없습니다.")
            if self._active_test_segment is not None:
                raise Conflict("활성 TestSegment 가 있습니다. 먼저 종료/폐기하세요.")
            self._active_run = None
        self.store.set_run_status(run.run_id, "completed", now_ms())
        logger.info("run end: %s", run.run_id)
        return {"ended": True, "run": run.to_dict()}

    def mark_run_interrupted(self, run_id: str) -> None:
        self.store.set_run_status(run_id, "interrupted", now_ms())

    # -- TestSegment ---------------------------------------------------
    def prepare_test_segment(self, point_id: str, order_index: int,
                             stabilization_seconds: int, recording_seconds: int,
                             note: str | None = None) -> dict[str, Any]:
        point_id = point_id.strip()
        if not point_id:
            raise ValueError("point_id 가 비어 있습니다.")
        with self._lock:
            run = self._active_run
            if run is None:
                raise Conflict("본 실험 Run 이 없습니다. TestSegment 를 시작할 수 없습니다.")
            if self._active_test_segment is not None:
                raise Conflict("이미 안정화/기록 중인 TestSegment 가 있습니다.")
            prepared = now_ms()
            rec_start = prepared + stabilization_seconds * 1000
            rec_end = rec_start + recording_seconds * 1000
            attempt = self.store.latest_attempt_index(run.run_id, point_id) + 1
            segment_id = f"{run.run_id}__{point_id}__a{attempt}__{prepared}"
            seg = ActiveTestSegment(segment_id, run.run_id, point_id, order_index, attempt,
                                    prepared, rec_start, rec_end)
            self._active_test_segment = seg
        self.store.create_test_segment(segment_id, run.run_id, point_id, order_index, attempt,
                                       prepared, rec_start, rec_end, note)
        logger.info("segment prepare: %s (order %d, attempt %d)", point_id, order_index, attempt)
        return seg.to_dict()

    def active_test_segment(self) -> ActiveTestSegment | None:
        with self._lock:
            return self._active_test_segment

    def finish_test_segment(self) -> dict[str, Any]:
        """정상 완료(2분 도달 또는 즉시 종료). 저장된 시간 범위는 유지한다."""
        with self._lock:
            seg = self._active_test_segment
            self._active_test_segment = None
        if seg is None:
            return {"finished": False}
        self.store.set_segment_status(seg.segment_id, "completed", now_ms())
        logger.info("segment finish: %s", seg.point_id)
        return {"finished": True, "segment": seg.to_dict()}

    def discard_test_segment(self) -> dict[str, Any]:
        """현재 Segment 버리고 재측정 준비. superseded 로 내린다(C1~C4 원본은 보존)."""
        with self._lock:
            seg = self._active_test_segment
            self._active_test_segment = None
        if seg is None:
            return {"discarded": False}
        self.store.set_segment_status(seg.segment_id, "discarded", now_ms())
        self.store.supersede_segment(seg.segment_id)
        logger.info("segment discard: %s", seg.point_id)
        return {"discarded": True, "segment": seg.to_dict()}

    def auto_advance_segment(self) -> dict[str, Any] | None:
        """백그라운드 루프가 호출. 기록 종료 시각이 지나면 Segment 를 completed 로 자동 마감.

        Run 에는 자동 종료가 없다.
        """
        cur = now_ms()
        with self._lock:
            seg = self._active_test_segment
            if seg is None or cur < seg.recording_ended_at_ms:
                return None
            self._active_test_segment = None
        self.store.set_segment_status(seg.segment_id, "completed", cur)
        logger.info("segment auto-complete: %s", seg.point_id)
        return seg.to_dict()

    # -- 저장 컨텍스트 판정 (MQTT 경로가 사용) --------------------------
    def context_at(self, node_id: str, server_ts_ms: int) -> dict[str, Any] | None:
        """이 노드의 이 시각 샘플을 어디에 저장할지 결정. 저장 대상이 아니면 None.

        - Run 없음 → None (본 실험 measurement 에 저장 안 함; JSONL 원본은 별도 보존)
        - calibration 노드 → run_id 항상, segment_id 는 시간이 기록 구간이면 함께
        - test 노드 → 기록 Segment 안일 때만 저장(이동·안정화 중이면 None)
        """
        with self._lock:
            run = self._active_run
            found = self._assignments.get(node_id)
        if run is None or found is None:
            return None
        point_id, role = found
        segment = self.store.segment_for_timestamp(run.run_id, server_ts_ms)
        if role == "calibration":
            return {
                "run_id": run.run_id, "segment_id": segment["segment_id"] if segment else None,
                "point_id": point_id, "point_role": "calibration",
            }
        if role == "test":
            if segment is None:
                return None  # 이동·안정화 중 T 데이터는 Test 로 저장하지 않음
            return {
                "run_id": run.run_id, "segment_id": segment["segment_id"],
                "point_id": segment["point_id"], "point_role": "test",
            }
        # offset 역할 노드는 OffsetRun 경로에서 별도 처리
        return None

    def offset_context(self, node_id: str) -> dict[str, Any] | None:
        """Offset 측정 중일 때 이 노드 샘플의 저장 컨텍스트."""
        with self._lock:
            offset = self._active_offset
            found = self._assignments.get(node_id)
        if offset is None:
            return None
        point_id = found[0] if found else "offset-00"
        return {"run_id": offset.offset_run_id, "segment_id": None,
                "point_id": point_id, "point_role": "offset"}


# 하위호환 별칭 (기존 import 를 깨지 않기 위해; main/mqtt 는 새 이름으로 전환 예정)
SessionManager = ExperimentManager


# 장치별 offset (계획서 §4.2):
#   Δ_d = m_ref - m_d,  m_ref = median(모든 장치의 중앙값),  RSSI_corrected = RSSI + Δ_d
def compute_device_offsets(store: ExperimentStore, experiment_id: str,
                           offset_run_id: str) -> dict[str, Any]:
    """지정 OffsetRun 의 offset 측정으로 장치 편차를 계산해 (offset_run_id, node) 로 저장.

    사전/사후를 각각 그 offset_run_id 로 저장하므로 사후가 사전값을 덮어쓰지 않는다.
    """
    rows = [
        r for r in store.measurements_for_export(experiment_id)
        if r["point_role"] == "offset" and r["valid"] == 1
        and r["rssi_filtered_dbm"] is not None
        and r.get("run_id") == offset_run_id
    ]
    if not rows:
        return {"ok": False, "reason": "offset 역할의 유효 측정이 없습니다.", "nodes": []}

    by_node: dict[str, list[float]] = {}
    for r in rows:
        by_node.setdefault(r["node_id"], []).append(float(r["rssi_filtered_dbm"]))

    medians = {node: statistics.median(vals) for node, vals in by_node.items()}
    reference = statistics.median(medians.values())
    calibrated_at = now_ms()

    result = []
    for node, vals in sorted(by_node.items()):
        median_dbm = medians[node]
        delta = reference - median_dbm
        std = statistics.stdev(vals) if len(vals) > 1 else 0.0
        store.upsert_device_offset(offset_run_id, node, experiment_id, round(median_dbm, 3),
                                   round(delta, 3), len(vals), round(std, 3), calibrated_at)
        result.append({
            "node_id": node, "offset_median_dbm": round(median_dbm, 3),
            "device_offset_db": round(delta, 3), "sample_count": len(vals),
            "std_db": round(std, 3), "calibrated_at_ms": calibrated_at,
        })

    return {"ok": True, "experiment_id": experiment_id, "offset_run_id": offset_run_id,
            "reference_median_dbm": round(reference, 3), "nodes": result}
