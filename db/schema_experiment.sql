-- 논문 실험용 스키마 (SQLite)
-- 계획서 §7.1 "반드시 저장할 필드"를 정규화한 형태.
-- 실시간 파이프라인(rssi_raw / frame / node_status)과 독립적으로 동작한다.

PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS experiment (
    experiment_id   TEXT PRIMARY KEY,
    started_at_ms   INTEGER NOT NULL,
    ended_at_ms     INTEGER,
    ap_bssid        TEXT,
    ap_channel      INTEGER,
    note            TEXT
);

CREATE TABLE IF NOT EXISTS session (
    session_id       TEXT PRIMARY KEY,
    experiment_id    TEXT NOT NULL,
    point_id         TEXT NOT NULL,
    point_role       TEXT NOT NULL,   -- offset | calibration | test
    started_at_ms    INTEGER NOT NULL,
    ended_at_ms      INTEGER,
    planned_seconds  INTEGER NOT NULL,
    superseded       INTEGER NOT NULL DEFAULT 0,  -- 재측정 시 이전 세션을 1로 표시
    note             TEXT
);

CREATE INDEX IF NOT EXISTS idx_session_point
    ON session (experiment_id, point_id, started_at_ms);

-- 원본 시계열. 비정상 값도 버리지 않고 valid=0 으로 보존한다. (계획서 §7.2 "Raw 데이터 보존")
CREATE TABLE IF NOT EXISTS measurement (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id      TEXT    NOT NULL,
    session_id         TEXT    NOT NULL,
    point_id           TEXT    NOT NULL,
    point_role         TEXT    NOT NULL,
    node_id            TEXT    NOT NULL,
    node_ts_ms         INTEGER,
    server_ts_ms       INTEGER NOT NULL,
    seq                INTEGER,
    rssi_raw_dbm       REAL,
    rssi_filtered_dbm  REAL,
    sample_count       INTEGER,
    error_flags        INTEGER NOT NULL DEFAULT 0,
    ap_bssid           TEXT,
    ap_channel         INTEGER,
    valid              INTEGER NOT NULL DEFAULT 1,
    invalid_reason     TEXT
);

CREATE INDEX IF NOT EXISTS idx_measurement_session
    ON measurement (session_id, node_id);
CREATE INDEX IF NOT EXISTS idx_measurement_point
    ON measurement (experiment_id, point_id, node_id);

-- 노드가 지금 어느 위치에 놓여 있는지.
-- 고정 보정 센서 4대는 실험 내내 각자의 위치에 있고, 이동 센서 1대만 Test 위치를 옮겨 다닌다.
-- 따라서 "세션 1개 = 위치 1개"가 아니라, 샘플의 위치는 그 노드의 배치로 결정된다.
CREATE TABLE IF NOT EXISTS node_assignment (
    experiment_id  TEXT NOT NULL,
    node_id        TEXT NOT NULL,
    point_id       TEXT NOT NULL,
    point_role     TEXT NOT NULL,
    updated_at_ms  INTEGER,
    PRIMARY KEY (experiment_id, node_id)
);

-- 위치 좌표. 현장에서는 종이에 적고, 측정 후 points.csv 로 일괄 등록한다.
CREATE TABLE IF NOT EXISTS point (
    experiment_id  TEXT NOT NULL,
    point_id       TEXT NOT NULL,
    point_role     TEXT,
    pos_x          REAL,
    pos_y          REAL,
    pos_z          REAL,
    note           TEXT,
    updated_at_ms  INTEGER,
    PRIMARY KEY (experiment_id, point_id)
);

-- 장치별 RSSI 편차 보정 (계획서 §4.3)
CREATE TABLE IF NOT EXISTS device_offset (
    experiment_id      TEXT NOT NULL,
    node_id            TEXT NOT NULL,
    offset_median_dbm  REAL,
    device_offset_db   REAL,
    sample_count       INTEGER,
    std_db             REAL,
    calibrated_at_ms   INTEGER,
    PRIMARY KEY (experiment_id, node_id)
);

-- TX(AP) 좌표. 그래픽스 파트로 넘길 config/tx_rx.json 생성에 사용.
CREATE TABLE IF NOT EXISTS tx (
    experiment_id  TEXT NOT NULL,
    tx_id          TEXT NOT NULL,
    pos_x          REAL,
    pos_y          REAL,
    pos_z          REAL,
    frequency_hz   INTEGER,
    ap_bssid       TEXT,
    ap_channel     INTEGER,
    note           TEXT,
    PRIMARY KEY (experiment_id, tx_id)
);
