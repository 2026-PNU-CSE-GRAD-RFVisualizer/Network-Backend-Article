"""논문 실험 데이터 저장소 (계획서 §12 저장 실패 대응).

- 수신 JSON은 즉시 JSONL 에 append (어떤 단계가 실패해도 원본 보존)
- 실험 데이터의 기준(source of truth)은 SQLite 파일 (Postgres 없이도 현장 측정 가능)
- 기존 Postgres 실시간 경로는 병행하되 기준 저장소는 아니다
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "db" / "schema_experiment.sql"


class ExperimentStore:
    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.base_dir / "experiment.db"
        self.jsonl_path = self.base_dir / "ingest_raw.jsonl"

        self._lock = threading.Lock()
        self._jsonl_lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._conn.commit()
        logger.info("experiment store ready: %s", self.db_path)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def append_jsonl(self, topic: str, payload: str, receive_ms: int,
                     session_id: str | None, point_id: str | None) -> None:
        """원본 백업: 세션 활성 여부와 무관하게 모든 수신 메시지를 남긴다."""
        line = json.dumps(
            {
                "recv_ms": receive_ms,
                "topic": topic,
                "session_id": session_id,
                "point_id": point_id,
                "payload": payload,
            },
            ensure_ascii=False,
        )
        try:
            with self._jsonl_lock:
                with self.jsonl_path.open("a", encoding="utf-8") as fp:
                    fp.write(line + "\n")
        except OSError:
            logger.exception("jsonl append failed")

    # -- experiment / session ------------------------------------------
    def create_experiment(self, experiment_id: str, started_at_ms: int,
                          ap_bssid: str | None, ap_channel: int | None,
                          note: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO experiment (experiment_id, started_at_ms, ap_bssid, ap_channel, note)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(experiment_id) DO UPDATE SET
                       ap_bssid=excluded.ap_bssid,
                       ap_channel=excluded.ap_channel,
                       note=excluded.note""",
                (experiment_id, started_at_ms, ap_bssid, ap_channel, note),
            )
            self._conn.commit()

    def end_experiment(self, experiment_id: str, ended_at_ms: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE experiment SET ended_at_ms = ? WHERE experiment_id = ?",
                (ended_at_ms, experiment_id),
            )
            self._conn.commit()

    def list_experiments(self) -> list[dict[str, Any]]:
        return self._query("SELECT * FROM experiment ORDER BY started_at_ms DESC")

    def create_session(self, session_id: str, experiment_id: str, point_id: str,
                       point_role: str, started_at_ms: int, planned_seconds: int,
                       note: str | None = None) -> None:
        with self._lock:
            # 같은 위치 재측정 시 이전 세션을 superseded 로 표시(원본은 유지, 대표값만 최신 세션)
            self._conn.execute(
                """UPDATE session SET superseded = 1
                   WHERE experiment_id = ? AND point_id = ? AND superseded = 0""",
                (experiment_id, point_id),
            )
            self._conn.execute(
                """INSERT INTO session
                       (session_id, experiment_id, point_id, point_role,
                        started_at_ms, planned_seconds, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, experiment_id, point_id, point_role,
                 started_at_ms, planned_seconds, note),
            )
            self._conn.commit()

    def end_session(self, session_id: str, ended_at_ms: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE session SET ended_at_ms = ? WHERE session_id = ?",
                (ended_at_ms, session_id),
            )
            self._conn.commit()

    def discard_session(self, session_id: str) -> None:
        """측정 실패 시: 세션을 superseded 로 내리고 이전 세션을 되살린다."""
        with self._lock:
            row = self._conn.execute(
                "SELECT experiment_id, point_id FROM session WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return
            self._conn.execute(
                "UPDATE session SET superseded = 1 WHERE session_id = ?", (session_id,)
            )
            prev = self._conn.execute(
                """SELECT session_id FROM session
                   WHERE experiment_id = ? AND point_id = ? AND session_id != ?
                   ORDER BY started_at_ms DESC LIMIT 1""",
                (row["experiment_id"], row["point_id"], session_id),
            ).fetchone()
            if prev is not None:
                self._conn.execute(
                    "UPDATE session SET superseded = 0 WHERE session_id = ?",
                    (prev["session_id"],),
                )
            self._conn.commit()

    def list_sessions(self, experiment_id: str) -> list[dict[str, Any]]:
        return self._query(
            """SELECT s.*,
                      (SELECT COUNT(*) FROM measurement m
                        WHERE m.session_id = s.session_id AND m.valid = 1
                          AND m.point_id = s.point_id) AS valid_samples,
                      (SELECT COUNT(*) FROM measurement m
                        WHERE m.session_id = s.session_id) AS total_samples
                 FROM session s
                WHERE s.experiment_id = ?
                ORDER BY s.started_at_ms""",
            (experiment_id,),
        )

    # -- measurement ---------------------------------------------------
    def insert_measurements(self, rows: Iterable[dict[str, Any]]) -> int:
        records = [
            (
                r["experiment_id"], r["session_id"], r["point_id"], r["point_role"],
                r["node_id"], r.get("node_ts_ms"), r["server_ts_ms"], r.get("seq"),
                r.get("rssi_raw_dbm"), r.get("rssi_filtered_dbm"), r.get("sample_count"),
                int(r.get("error_flags") or 0), r.get("ap_bssid"), r.get("ap_channel"),
                1 if r.get("valid", True) else 0, r.get("invalid_reason"),
            )
            for r in rows
        ]
        if not records:
            return 0
        with self._lock:
            self._conn.executemany(
                """INSERT INTO measurement
                       (experiment_id, session_id, point_id, point_role, node_id,
                        node_ts_ms, server_ts_ms, seq, rssi_raw_dbm, rssi_filtered_dbm,
                        sample_count, error_flags, ap_bssid, ap_channel, valid, invalid_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                records,
            )
            self._conn.commit()
        return len(records)

    def session_progress(self, session_id: str) -> list[dict[str, Any]]:
        """현장에서 '이 노드가 지금 몇 개 들어왔나' 집계."""
        return self._query(
            """SELECT node_id,
                      COUNT(*)                                   AS total,
                      SUM(CASE WHEN valid = 1 THEN 1 ELSE 0 END) AS valid,
                      MAX(server_ts_ms)                          AS last_ms,
                      AVG(rssi_filtered_dbm)                     AS avg_filtered
                 FROM measurement
                WHERE session_id = ?
                GROUP BY node_id
                ORDER BY node_id""",
            (session_id,),
        )

    def measurements_for_export(self, experiment_id: str,
                                only_current: bool = True) -> list[dict[str, Any]]:
        # 폐기된 세션에서는 '그 세션의 대상 위치' 행만 제외한다. 같은 시간에 들어온
        # 고정 보정 센서 데이터까지 버리지 않는다.
        clause = "AND NOT (s.superseded = 1 AND m.point_id = s.point_id)" if only_current else ""
        return self._query(
            f"""SELECT m.*, s.superseded, s.started_at_ms AS session_started_ms,
                       s.point_id AS session_point_id,
                       p.pos_x, p.pos_y, p.pos_z,
                       d.device_offset_db
                  FROM measurement m
                  JOIN session s ON s.session_id = m.session_id
             LEFT JOIN point p ON p.experiment_id = m.experiment_id AND p.point_id = m.point_id
             LEFT JOIN device_offset d ON d.experiment_id = m.experiment_id AND d.node_id = m.node_id
                 WHERE m.experiment_id = ? {clause}
                 ORDER BY m.server_ts_ms, m.node_id""",
            (experiment_id,),
        )

    # -- node_assignment -----------------------------------------------
    def upsert_assignment(self, experiment_id: str, node_id: str, point_id: str,
                          point_role: str, updated_at_ms: int) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO node_assignment
                       (experiment_id, node_id, point_id, point_role, updated_at_ms)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(experiment_id, node_id) DO UPDATE SET
                       point_id=excluded.point_id, point_role=excluded.point_role,
                       updated_at_ms=excluded.updated_at_ms""",
                (experiment_id, node_id, point_id, point_role, updated_at_ms),
            )
            self._conn.commit()

    def list_assignments(self, experiment_id: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM node_assignment WHERE experiment_id = ? ORDER BY node_id",
            (experiment_id,),
        )

    # -- point / offset / tx -------------------------------------------
    def upsert_point(self, experiment_id: str, point_id: str, point_role: str | None,
                     pos_x: float | None, pos_y: float | None, pos_z: float | None,
                     note: str | None, updated_at_ms: int) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO point
                       (experiment_id, point_id, point_role, pos_x, pos_y, pos_z, note, updated_at_ms)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(experiment_id, point_id) DO UPDATE SET
                       point_role=excluded.point_role, pos_x=excluded.pos_x,
                       pos_y=excluded.pos_y, pos_z=excluded.pos_z,
                       note=excluded.note, updated_at_ms=excluded.updated_at_ms""",
                (experiment_id, point_id, point_role, pos_x, pos_y, pos_z, note, updated_at_ms),
            )
            self._conn.commit()

    def list_points(self, experiment_id: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM point WHERE experiment_id = ? ORDER BY point_id", (experiment_id,)
        )

    def upsert_device_offset(self, experiment_id: str, node_id: str,
                             offset_median_dbm: float | None, device_offset_db: float | None,
                             sample_count: int, std_db: float | None,
                             calibrated_at_ms: int) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO device_offset
                       (experiment_id, node_id, offset_median_dbm, device_offset_db,
                        sample_count, std_db, calibrated_at_ms)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(experiment_id, node_id) DO UPDATE SET
                       offset_median_dbm=excluded.offset_median_dbm,
                       device_offset_db=excluded.device_offset_db,
                       sample_count=excluded.sample_count,
                       std_db=excluded.std_db,
                       calibrated_at_ms=excluded.calibrated_at_ms""",
                (experiment_id, node_id, offset_median_dbm, device_offset_db,
                 sample_count, std_db, calibrated_at_ms),
            )
            self._conn.commit()

    def list_device_offsets(self, experiment_id: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM device_offset WHERE experiment_id = ? ORDER BY node_id",
            (experiment_id,),
        )

    def upsert_tx(self, experiment_id: str, tx_id: str, pos_x: float | None,
                  pos_y: float | None, pos_z: float | None, frequency_hz: int | None,
                  ap_bssid: str | None, ap_channel: int | None, note: str | None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO tx
                       (experiment_id, tx_id, pos_x, pos_y, pos_z, frequency_hz,
                        ap_bssid, ap_channel, note)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(experiment_id, tx_id) DO UPDATE SET
                       pos_x=excluded.pos_x, pos_y=excluded.pos_y, pos_z=excluded.pos_z,
                       frequency_hz=excluded.frequency_hz, ap_bssid=excluded.ap_bssid,
                       ap_channel=excluded.ap_channel, note=excluded.note""",
                (experiment_id, tx_id, pos_x, pos_y, pos_z, frequency_hz,
                 ap_bssid, ap_channel, note),
            )
            self._conn.commit()

    def list_tx(self, experiment_id: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM tx WHERE experiment_id = ? ORDER BY tx_id", (experiment_id,)
        )

    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
