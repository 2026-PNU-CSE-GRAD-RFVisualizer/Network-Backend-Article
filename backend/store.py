"""논문 실험 데이터 저장소.

설계 원칙 (계획서 §12 "Backend 저장 실패" 대응):
  1. 수신한 모든 JSON 줄을 즉시 JSONL 파일에 append 한다. 어떤 단계가 실패해도 원본은 남는다.
  2. 실험 데이터는 SQLite 파일에 저장한다. Docker/Postgres 가 없어도 7/23 현장 측정이 가능하다.
  3. 기존 Postgres 실시간 경로는 그대로 병행 동작하되, 실험 결과의 기준(source of truth)은 아니다.

SQLite 는 5노드 × 1Hz 수준의 부하에서 충분하며, 파일 하나를 그대로 백업/전달할 수 있다.
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

# 스키마 버전. 올릴 때마다 _migrate 에 v(N-1)->vN 절을 추가한다.
#   1: measurement 에 run_id / segment_id, experiment_run / test_segment 추가
#   2: offset_run 테이블, device_offset 을 (offset_run_id, node_id) 키로, experiment_run pre/post
SCHEMA_VERSION = 2


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
        # fresh DB: 스키마가 모든 테이블/열을 만든다. 기존 DB: IF NOT EXISTS 라 신규 열은 안 생기므로
        # _migrate 가 ALTER 로 채운다. 순서상 executescript(신규 테이블 생성) 후 마이그레이션.
        self._conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        self._conn.commit()
        self._migrate()
        logger.info("experiment store ready: %s (schema v%d)", self.db_path, SCHEMA_VERSION)

    def _migrate(self) -> None:
        """PRAGMA user_version 기반 최소 마이그레이션. 실패를 성공으로 숨기지 않는다.

        기존 Measurement 는 run_id/segment_id 가 NULL 인 Legacy 데이터로 그대로 유지된다.
        DB 삭제를 요구하지 않는다.
        """
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        c = self._conn

        def has_col(table: str, col: str) -> bool:
            return col in {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}

        if version < 1:
            # v0 -> v1: measurement 에 run_id / segment_id
            if not has_col("measurement", "run_id"):
                c.execute("ALTER TABLE measurement ADD COLUMN run_id TEXT")
            if not has_col("measurement", "segment_id"):
                c.execute("ALTER TABLE measurement ADD COLUMN segment_id TEXT")

        if version < 2:
            # v1 -> v2: 사전/사후 Offset 분리
            if has_col("experiment_run", "run_id") and not has_col("experiment_run", "pre_offset_run_id"):
                c.execute("ALTER TABLE experiment_run ADD COLUMN pre_offset_run_id TEXT")
                c.execute("ALTER TABLE experiment_run ADD COLUMN post_offset_run_id TEXT")
            # device_offset 을 (offset_run_id, node_id) 키로 재구성. 기존 행은 legacy 키로 보존.
            if not has_col("device_offset", "offset_run_id"):
                c.execute("ALTER TABLE device_offset RENAME TO device_offset_old")
                c.executescript(
                    """CREATE TABLE device_offset (
                           offset_run_id TEXT NOT NULL, node_id TEXT NOT NULL,
                           experiment_id TEXT NOT NULL, offset_median_dbm REAL,
                           device_offset_db REAL, sample_count INTEGER, std_db REAL,
                           calibrated_at_ms INTEGER, PRIMARY KEY (offset_run_id, node_id));""")
                c.execute(
                    """INSERT INTO device_offset
                           (offset_run_id, node_id, experiment_id, offset_median_dbm,
                            device_offset_db, sample_count, std_db, calibrated_at_ms)
                       SELECT 'legacy:'||experiment_id, node_id, experiment_id, offset_median_dbm,
                            device_offset_db, sample_count, std_db, calibrated_at_ms
                       FROM device_offset_old""")
                c.execute("DROP TABLE device_offset_old")

        # 열이 보장된 뒤 인덱스 (fresh/기존 공통)
        c.execute("CREATE INDEX IF NOT EXISTS idx_measurement_run_time "
                  "ON measurement(run_id, server_ts_ms)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_measurement_segment_node "
                  "ON measurement(segment_id, node_id, server_ts_ms)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_test_segment_run "
                  "ON test_segment(run_id, order_index, status)")
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        c.commit()
        logger.info("migrated experiment DB: v%d -> v%d", version, SCHEMA_VERSION)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # 원본 백업: 세션 활성 여부와 무관하게 모든 수신 메시지를 남긴다.
    # ------------------------------------------------------------------
    def append_jsonl(self, topic: str, payload: str, receive_ms: int,
                     session_id: str | None, point_id: str | None) -> None:
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

    # ------------------------------------------------------------------
    # experiment / session
    # ------------------------------------------------------------------
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
            # 같은 위치를 다시 측정하면 이전 세션들을 superseded 로 표시한다.
            # 원본은 지우지 않고, 대표값 계산에서만 최신 세션을 쓴다.
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
        """측정을 망친 경우: 세션을 superseded 로 내리고 이전 세션을 되살린다."""
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

    # ------------------------------------------------------------------
    # measurement
    # ------------------------------------------------------------------
    def insert_measurements(self, rows: Iterable[dict[str, Any]]) -> int:
        # session_id 는 하위호환 유지. Run 기반 저장은 run_id 를 session_id 로도 채운다.
        records = [
            (
                r["experiment_id"], r.get("session_id") or r.get("run_id") or "",
                r.get("run_id"), r.get("segment_id"),
                r["point_id"], r["point_role"],
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
                       (experiment_id, session_id, run_id, segment_id, point_id, point_role, node_id,
                        node_ts_ms, server_ts_ms, seq, rssi_raw_dbm, rssi_filtered_dbm,
                        sample_count, error_flags, ap_bssid, ap_channel, valid, invalid_reason)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                records,
            )
            self._conn.commit()
        return len(records)

    # -- experiment_run --------------------------------------------------
    def create_run(self, run_id: str, experiment_id: str, direction: str,
                   pass_index: int, started_at_ms: int,
                   pre_offset_run_id: str | None = None, note: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO experiment_run
                       (run_id, experiment_id, direction, pass_index, status,
                        started_at_ms, pre_offset_run_id, note)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (run_id, experiment_id, direction, pass_index, "running",
                 started_at_ms, pre_offset_run_id, note),
            )
            self._conn.commit()

    def set_run_offset_refs(self, run_id: str, pre_offset_run_id: str | None = None,
                            post_offset_run_id: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE experiment_run SET pre_offset_run_id = COALESCE(?, pre_offset_run_id), "
                "post_offset_run_id = COALESCE(?, post_offset_run_id) WHERE run_id = ?",
                (pre_offset_run_id, post_offset_run_id, run_id),
            )
            self._conn.commit()

    # -- offset_run (사전/사후 장비 편차) --------------------------------
    def create_offset_run(self, offset_run_id: str, experiment_id: str, phase: str,
                          started_at_ms: int, note: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO offset_run (offset_run_id, experiment_id, phase, status,
                                           started_at_ms, note)
                   VALUES (?,?,?,?,?,?)""",
                (offset_run_id, experiment_id, phase, "running", started_at_ms, note),
            )
            self._conn.commit()

    def set_offset_run_status(self, offset_run_id: str, status: str,
                              ended_at_ms: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE offset_run SET status = ?, ended_at_ms = COALESCE(?, ended_at_ms) "
                "WHERE offset_run_id = ?", (status, ended_at_ms, offset_run_id),
            )
            self._conn.commit()

    def list_offset_runs(self, experiment_id: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM offset_run WHERE experiment_id = ? ORDER BY started_at_ms",
            (experiment_id,),
        )

    def latest_offset_run(self, experiment_id: str, phase: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT * FROM offset_run WHERE experiment_id = ? AND phase = ? AND status = 'completed' "
            "ORDER BY started_at_ms DESC LIMIT 1", (experiment_id, phase),
        )
        return rows[0] if rows else None

    def set_run_status(self, run_id: str, status: str, ended_at_ms: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE experiment_run SET status = ?, ended_at_ms = COALESCE(?, ended_at_ms) "
                "WHERE run_id = ?",
                (status, ended_at_ms, run_id),
            )
            self._conn.commit()

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM experiment_run WHERE run_id = ?", (run_id,))
        return rows[0] if rows else None

    def list_runs(self, experiment_id: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM experiment_run WHERE experiment_id = ? ORDER BY started_at_ms",
            (experiment_id,),
        )

    def list_open_runs(self, experiment_id: str) -> list[dict[str, Any]]:
        """아직 running 상태로 남은 Run (백엔드 재시작 시 interrupted 처리용)."""
        return self._query(
            "SELECT * FROM experiment_run WHERE experiment_id = ? AND status = 'running'",
            (experiment_id,),
        )

    def mark_all_open_interrupted(self, ended_at_ms: int) -> dict[str, int]:
        """백엔드 재시작 시(§15): running Run 과 stabilizing/recording Segment 를 interrupted 로.

        열린 구간을 정상 완료로 숨기지 않는다. 원본 데이터는 그대로 둔다.
        """
        with self._lock:
            runs = self._conn.execute(
                "UPDATE experiment_run SET status='interrupted', ended_at_ms=? "
                "WHERE status='running'", (ended_at_ms,)).rowcount
            segs = self._conn.execute(
                "UPDATE test_segment SET status='interrupted', actual_ended_at_ms=? "
                "WHERE status IN ('stabilizing','recording')", (ended_at_ms,)).rowcount
            self._conn.commit()
        return {"runs": runs, "segments": segs}

    # -- test_segment ----------------------------------------------------
    def create_test_segment(self, segment_id: str, run_id: str, point_id: str,
                            order_index: int, attempt_index: int, prepared_at_ms: int,
                            recording_started_at_ms: int, recording_ended_at_ms: int,
                            note: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                """INSERT INTO test_segment
                       (segment_id, run_id, point_id, order_index, attempt_index, status,
                        prepared_at_ms, recording_started_at_ms, recording_ended_at_ms, note)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (segment_id, run_id, point_id, order_index, attempt_index, "stabilizing",
                 prepared_at_ms, recording_started_at_ms, recording_ended_at_ms, note),
            )
            self._conn.commit()

    def set_segment_status(self, segment_id: str, status: str,
                           actual_ended_at_ms: int | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE test_segment SET status = ?, actual_ended_at_ms = COALESCE(?, actual_ended_at_ms) "
                "WHERE segment_id = ?",
                (status, actual_ended_at_ms, segment_id),
            )
            self._conn.commit()

    def supersede_segment(self, segment_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE test_segment SET superseded = 1 WHERE segment_id = ?", (segment_id,))
            self._conn.commit()

    def truncate_segment_recording(self, segment_id: str, ended_ms: int) -> None:
        """조기 종료 시 기록 구간 끝을 앞당긴다. 이후 도착 데이터가 이 Segment 에 오염되지 않게 함.

        (segment_for_timestamp 가 recording_ended_at_ms 로 판정하므로 창을 줄여야 안전.)
        이미 더 이른 값이면 유지한다.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE test_segment SET recording_ended_at_ms = MIN(recording_ended_at_ms, ?) "
                "WHERE segment_id = ?", (ended_ms, segment_id))
            self._conn.commit()

    def get_test_segment(self, segment_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM test_segment WHERE segment_id = ?", (segment_id,))
        return rows[0] if rows else None

    def latest_attempt_index(self, run_id: str, point_id: str) -> int:
        row = self._query(
            "SELECT MAX(attempt_index) AS m FROM test_segment WHERE run_id = ? AND point_id = ?",
            (run_id, point_id),
        )
        return (row[0]["m"] or 0) if row else 0

    def list_test_segments(self, run_id: str) -> list[dict[str, Any]]:
        return self._query(
            "SELECT * FROM test_segment WHERE run_id = ? ORDER BY order_index, attempt_index",
            (run_id,),
        )

    def run_node_stats(self, run_id: str) -> dict[str, dict[str, Any]]:
        """현재 Run 의 노드별 누적 샘플 수·마지막 수신 시각(상태 화면용)."""
        rows = self._query(
            """SELECT node_id, COUNT(*) AS samples, MAX(server_ts_ms) AS last_ms
                 FROM measurement WHERE run_id = ? GROUP BY node_id""",
            (run_id,),
        )
        return {r["node_id"]: {"samples": r["samples"], "last_ms": r["last_ms"]} for r in rows}

    def segment_for_timestamp(self, run_id: str, server_ts_ms: int) -> dict[str, Any] | None:
        """server_ts_ms 가 [recording_started, recording_ended) 안에 드는 기록 Segment.

        MQTT 처리가 지연돼 Segment 자동 종료 뒤 실행돼도, 시간 범위로 올바른 Segment 를 찾는다.
        폐기(superseded)·중단(discarded/interrupted) Segment 는 제외한다.
        """
        # 상태가 아니라 시간 범위로 판정한다. 안정화 중이어도 ts 가 기록창 안이면 그 Segment 소속.
        # 폐기/중단된 Segment 만 제외한다.
        rows = self._query(
            """SELECT * FROM test_segment
                WHERE run_id = ? AND superseded = 0
                  AND status NOT IN ('discarded', 'interrupted')
                  AND recording_started_at_ms <= ? AND ? < recording_ended_at_ms
                ORDER BY recording_started_at_ms DESC LIMIT 1""",
            (run_id, server_ts_ms, server_ts_ms),
        )
        return rows[0] if rows else None

    def session_progress(self, session_id: str) -> list[dict[str, Any]]:
        """현장에서 '이 노드가 지금 몇 개 들어왔나'를 보기 위한 집계."""
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

    def measurements_for_export(self, experiment_id: str) -> list[dict[str, Any]]:
        """모든 measurement 행 + segment/좌표/offset 메타. session 테이블에 의존하지 않는다.

        폐기(superseded) 여부는 segment_superseded 로 함께 반환하고, 필터링은 export 계층이
        판단한다(계획서 §12: 폐기 Segment 의 T 는 제외하되 C1~C4 연속 원본은 보존).
        Legacy 행(run_id/segment_id NULL)도 그대로 포함된다.
        """
        return self._query(
            """SELECT m.*,
                      seg.superseded              AS segment_superseded,
                      seg.status                  AS segment_status,
                      seg.point_id                AS segment_point_id,
                      seg.order_index             AS segment_order_index,
                      seg.attempt_index           AS segment_attempt_index,
                      seg.recording_started_at_ms AS segment_started_ms,
                      seg.recording_ended_at_ms   AS segment_ended_ms,
                      r.direction, r.pass_index,
                      p.pos_x, p.pos_y, p.pos_z,
                      d.device_offset_db
                  FROM measurement m
             LEFT JOIN test_segment  seg ON seg.segment_id = m.segment_id
             LEFT JOIN experiment_run r  ON r.run_id = m.run_id
             LEFT JOIN point p ON p.experiment_id = m.experiment_id AND p.point_id = m.point_id
             -- corrected_rssi 는 그 Run 에 적용한 '사전' Offset 을 쓴다(사후로 재보정 안 함)
             LEFT JOIN device_offset d ON d.offset_run_id = r.pre_offset_run_id AND d.node_id = m.node_id
                 WHERE m.experiment_id = ?
                 ORDER BY m.server_ts_ms, m.node_id""",
            (experiment_id,),
        )

    # ------------------------------------------------------------------
    # node_assignment
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # point / offset / tx
    # ------------------------------------------------------------------
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

    def upsert_device_offset(self, offset_run_id: str, node_id: str, experiment_id: str,
                             offset_median_dbm: float | None, device_offset_db: float | None,
                             sample_count: int, std_db: float | None,
                             calibrated_at_ms: int) -> None:
        # 키는 (offset_run_id, node_id): 사후 계산이 사전값을 덮어쓰지 않게 한다.
        with self._lock:
            self._conn.execute(
                """INSERT INTO device_offset
                       (offset_run_id, node_id, experiment_id, offset_median_dbm, device_offset_db,
                        sample_count, std_db, calibrated_at_ms)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(offset_run_id, node_id) DO UPDATE SET
                       offset_median_dbm=excluded.offset_median_dbm,
                       device_offset_db=excluded.device_offset_db,
                       sample_count=excluded.sample_count,
                       std_db=excluded.std_db,
                       calibrated_at_ms=excluded.calibrated_at_ms""",
                (offset_run_id, node_id, experiment_id, offset_median_dbm, device_offset_db,
                 sample_count, std_db, calibrated_at_ms),
            )
            self._conn.commit()

    def list_device_offsets(self, experiment_id: str,
                            offset_run_id: str | None = None) -> list[dict[str, Any]]:
        if offset_run_id is not None:
            return self._query(
                "SELECT * FROM device_offset WHERE offset_run_id = ? ORDER BY node_id",
                (offset_run_id,))
        return self._query(
            "SELECT * FROM device_offset WHERE experiment_id = ? ORDER BY offset_run_id, node_id",
            (experiment_id,))

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

    # ------------------------------------------------------------------
    def _query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
