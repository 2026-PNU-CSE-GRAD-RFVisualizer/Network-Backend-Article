"""스키마 마이그레이션 검증 (2단계).

    python tests/test_migration.py        # pytest 없이도 실행 가능

- fresh DB: 신규 테이블/열/버전이 만들어진다.
- legacy DB: 기존 스키마 DB를 새 코드로 열어도 데이터가 유지되고 열이 추가된다.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.store import SCHEMA_VERSION, ExperimentStore  # noqa: E402


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}


def test_fresh_db_has_new_schema():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = ExperimentStore(Path(tmp) / "data")
        conn = sqlite3.connect(store.db_path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert {"experiment_run", "test_segment"} <= _tables(conn)
        assert {"run_id", "segment_id"} <= _cols(conn, "measurement")
        conn.close()
        store.close()


def test_legacy_db_migrates_and_preserves_rows():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        data_dir = Path(tmp) / "data"
        data_dir.mkdir(parents=True)
        db_path = data_dir / "experiment.db"

        # 1) 구 스키마 DB 를 손으로 만든다 (run_id/segment_id 없음, 신규 테이블 없음, user_version 0)
        legacy = sqlite3.connect(db_path)
        legacy.executescript(
            """
            CREATE TABLE measurement (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                experiment_id TEXT NOT NULL, session_id TEXT NOT NULL,
                point_id TEXT NOT NULL, point_role TEXT NOT NULL, node_id TEXT NOT NULL,
                node_ts_ms INTEGER, server_ts_ms INTEGER NOT NULL, seq INTEGER,
                rssi_raw_dbm REAL, rssi_filtered_dbm REAL, sample_count INTEGER,
                error_flags INTEGER NOT NULL DEFAULT 0, ap_bssid TEXT, ap_channel INTEGER,
                valid INTEGER NOT NULL DEFAULT 1, invalid_reason TEXT
            );
            INSERT INTO measurement
                (experiment_id, session_id, point_id, point_role, node_id, server_ts_ms, rssi_filtered_dbm)
            VALUES ('legacy_exp', 'legacy_sess', 'cal-01', 'calibration', 'node1', 1700000000000, -61.0);
            """
        )
        assert legacy.execute("PRAGMA user_version").fetchone()[0] == 0
        legacy.commit()
        legacy.close()

        # 2) 새 코드로 연다 → 마이그레이션
        store = ExperimentStore(data_dir)

        conn = sqlite3.connect(db_path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert {"run_id", "segment_id"} <= _cols(conn, "measurement")
        assert {"experiment_run", "test_segment"} <= _tables(conn)

        # 기존 행 보존 + Legacy 는 run_id/segment_id 가 NULL
        row = conn.execute(
            "SELECT experiment_id, node_id, rssi_filtered_dbm, run_id, segment_id "
            "FROM measurement WHERE session_id='legacy_sess'").fetchone()
        assert row is not None
        assert row[0] == "legacy_exp" and row[1] == "node1" and row[2] == -61.0
        assert row[3] is None and row[4] is None  # legacy → NULL

        # 마이그레이션 후 run_id/segment_id 있는 새 행 삽입도 가능해야 한다
        conn.execute(
            "INSERT INTO measurement "
            "(experiment_id, session_id, run_id, segment_id, point_id, point_role, node_id, server_ts_ms) "
            "VALUES ('legacy_exp','run-1','run-1','seg-1','T1','test','node4',1700000100000)")
        conn.commit()
        cnt = conn.execute("SELECT COUNT(*) FROM measurement").fetchone()[0]
        assert cnt == 2
        conn.close()
        store.close()


def test_migrate_is_idempotent():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        data_dir = Path(tmp) / "data"
        ExperimentStore(data_dir).close()
        # 두 번째로 열어도(이미 v1) 오류 없이 그대로
        store = ExperimentStore(data_dir)
        conn = sqlite3.connect(store.db_path)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        conn.close()
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
