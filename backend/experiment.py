"""실험 세션 제어와 장치 offset 계산 (계획서 §3.3 위치당 30초, §4 장치 편차 보정)."""

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


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class ActiveSession:
    session_id: str
    experiment_id: str
    point_id: str
    point_role: str
    started_at_ms: int
    planned_seconds: int

    @property
    def deadline_ms(self) -> int:
        return self.started_at_ms + self.planned_seconds * 1000

    def remaining_ms(self, current_ms: int | None = None) -> int:
        return max(0, self.deadline_ms - (current_ms or now_ms()))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["deadline_ms"] = self.deadline_ms
        data["remaining_ms"] = self.remaining_ms()
        return data


class SessionManager:
    """현재 측정 중인 위치를 들고 있는 단일 상태 객체.

    MQTT 수신 스레드와 HTTP 핸들러가 동시에 읽으므로 락으로 보호한다.
    """

    def __init__(self, store: ExperimentStore) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._active: ActiveSession | None = None
        self._experiment_id: str | None = None
        self._ap_bssid: str | None = None
        self._ap_channel: int | None = None
        # node_id -> (point_id, point_role): 각 노드가 지금 놓인 위치
        self._assignments: dict[str, tuple[str, str]] = {}

    @property
    def experiment_id(self) -> str | None:
        return self._experiment_id

    @property
    def ap_bssid(self) -> str | None:
        return self._ap_bssid

    @property
    def ap_channel(self) -> int | None:
        return self._ap_channel

    def active(self) -> ActiveSession | None:
        with self._lock:
            return self._active

    # -- 노드 배치 ------------------------------------------------------
    def assign(self, node_id: str, point_id: str, point_role: str) -> dict[str, Any]:
        """노드를 위치에 배치. 고정 센서는 1회, 이동 센서는 Test 위치마다 재배치."""
        if self._experiment_id is None:
            raise ValueError("실험이 시작되지 않았습니다.")
        if point_role not in VALID_ROLES:
            raise ValueError(f"point_role 은 {VALID_ROLES} 중 하나여야 합니다.")
        node_id, point_id = node_id.strip(), point_id.strip()
        if not node_id or not point_id:
            raise ValueError("node_id 와 point_id 는 비어 있을 수 없습니다.")
        with self._lock:
            self._assignments[node_id] = (point_id, point_role)
        self.store.upsert_assignment(self._experiment_id, node_id, point_id,
                                     point_role, now_ms())
        logger.info("assign %s -> %s (%s)", node_id, point_id, point_role)
        return {"node_id": node_id, "point_id": point_id, "point_role": point_role}

    def assignments(self) -> dict[str, tuple[str, str]]:
        with self._lock:
            return dict(self._assignments)

    def resolve(self, node_id: str, session: ActiveSession) -> tuple[str, str]:
        """샘플이 속한 위치 결정. 배치 없는 노드는 세션 라벨로 두고 나중에 CSV에서 거른다."""
        with self._lock:
            found = self._assignments.get(node_id)
        return found if found is not None else (session.point_id, session.point_role)

    def start_experiment(self, experiment_id: str, ap_bssid: str | None,
                         ap_channel: int | None, note: str | None = None) -> dict[str, Any]:
        self.store.create_experiment(experiment_id, now_ms(), ap_bssid, ap_channel, note)
        # 백엔드 재시작에도 기존 배치를 이어받는다.
        restored = {
            a["node_id"]: (a["point_id"], a["point_role"])
            for a in self.store.list_assignments(experiment_id)
        }
        with self._lock:
            self._experiment_id = experiment_id
            self._ap_bssid = ap_bssid
            self._ap_channel = ap_channel
            self._active = None
            self._assignments = restored
        logger.info("experiment started: %s (bssid=%s ch=%s, %d assignments restored)",
                    experiment_id, ap_bssid, ap_channel, len(restored))
        return {"experiment_id": experiment_id, "ap_bssid": ap_bssid,
                "ap_channel": ap_channel, "assignments_restored": len(restored)}

    def end_experiment(self) -> dict[str, Any]:
        with self._lock:
            experiment_id = self._experiment_id
            self._active = None
        if experiment_id:
            self.store.end_experiment(experiment_id, now_ms())
        return {"experiment_id": experiment_id, "ended": bool(experiment_id)}

    def start_session(self, point_id: str, point_role: str, planned_seconds: int,
                      note: str | None = None,
                      moving_node_id: str | None = None) -> ActiveSession:
        if self._experiment_id is None:
            raise ValueError("실험이 시작되지 않았습니다. 먼저 실험을 시작하세요.")
        if point_role not in VALID_ROLES:
            raise ValueError(f"point_role 은 {VALID_ROLES} 중 하나여야 합니다.")
        point_id = point_id.strip()
        if not point_id:
            raise ValueError("point_id 가 비어 있습니다.")

        # 이동 센서만 이번 위치로 재배치. 고정 센서 배치는 유지.
        if moving_node_id:
            self.assign(moving_node_id, point_id, point_role)

        started = now_ms()
        session_id = f"{self._experiment_id}__{point_id}__{started}"
        self.store.create_session(session_id, self._experiment_id, point_id,
                                  point_role, started, planned_seconds, note)
        session = ActiveSession(session_id, self._experiment_id, point_id,
                                point_role, started, planned_seconds)
        with self._lock:
            self._active = session
        logger.info("session start: %s (%s, %ds)", point_id, point_role, planned_seconds)
        return session

    def stop_session(self, discard: bool = False) -> dict[str, Any]:
        with self._lock:
            session = self._active
            self._active = None
        if session is None:
            return {"stopped": False}
        self.store.end_session(session.session_id, now_ms())
        if discard:
            self.store.discard_session(session.session_id)
        logger.info("session stop: %s (discard=%s)", session.point_id, discard)
        return {"stopped": True, "discarded": discard, "session": session.to_dict()}

    def auto_stop_if_expired(self) -> dict[str, Any] | None:
        """백그라운드 루프가 호출. 계획 시간이 지나면 세션 자동 종료."""
        with self._lock:
            session = self._active
            if session is None or session.remaining_ms() > 0:
                return None
            self._active = None
        self.store.end_session(session.session_id, now_ms())
        logger.info("session auto-stop: %s", session.point_id)
        return session.to_dict()


# 장치별 offset (계획서 §4.2):
#   Δ_d = m_ref - m_d,  m_ref = median(모든 장치의 중앙값),  RSSI_corrected = RSSI + Δ_d
def compute_device_offsets(store: ExperimentStore, experiment_id: str) -> dict[str, Any]:
    # offset 은 5대를 한 자리에 모아 측정한 세션 데이터만 사용한다. 세션 라벨과 위치가
    # 일치하는 행만 채택해, 이동 센서 재배치 전후 데이터가 섞이는 것을 막는다.
    rows = [
        r for r in store.measurements_for_export(experiment_id)
        if r["point_role"] == "offset" and r["valid"] == 1
        and r["rssi_filtered_dbm"] is not None
        and r["point_id"] == r["session_point_id"]
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
        store.upsert_device_offset(experiment_id, node, round(median_dbm, 3),
                                   round(delta, 3), len(vals), round(std, 3), calibrated_at)
        result.append({
            "node_id": node,
            "offset_median_dbm": round(median_dbm, 3),
            "device_offset_db": round(delta, 3),
            "sample_count": len(vals),
            "std_db": round(std, 3),
            "calibrated_at_ms": calibrated_at,
        })

    return {
        "ok": True,
        "experiment_id": experiment_id,
        "reference_median_dbm": round(reference, 3),
        "nodes": result,
    }
