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

-- 사전·사후 장비 편차(Offset) 측정 회차. 본 실험 Run 과 분리해 저장한다.
-- pre = 본 실험 보정에 사용. post = 실험 전후 편차 변화(drift) 확인용(재보정 안 함).
CREATE TABLE IF NOT EXISTS offset_run (
    offset_run_id  TEXT PRIMARY KEY,
    experiment_id  TEXT NOT NULL,
    phase          TEXT NOT NULL,           -- pre | post
    status         TEXT NOT NULL,           -- running | completed | interrupted | discarded
    started_at_ms  INTEGER NOT NULL,
    ended_at_ms    INTEGER,
    note           TEXT
);

-- 본 실험 회차. C1~C4 가 끊기지 않고 기록되는 한 회차(정/역방향).
-- 사용자가 명시적으로 종료할 때까지 활성(자동 종료 없음).
-- pre_offset_run_id: 이 Run 에 실제 적용한 사전 Offset 의 출처. post 는 사후 측정(없으면 NULL).
CREATE TABLE IF NOT EXISTS experiment_run (
    run_id              TEXT PRIMARY KEY,
    experiment_id       TEXT NOT NULL,
    direction           TEXT NOT NULL,      -- forward | reverse
    pass_index          INTEGER NOT NULL,
    status              TEXT NOT NULL,      -- running | completed | interrupted | discarded
    started_at_ms       INTEGER NOT NULL,
    ended_at_ms         INTEGER,
    pre_offset_run_id   TEXT,
    post_offset_run_id  TEXT,
    note                TEXT
);

-- 이동 센서 T 가 특정 위치에서 실제로 기록되는 약 2분 구간.
-- 시간 범위(recording_started_at_ms ~ recording_ended_at_ms)로 동시간 C1~C4 를 매칭한다.
CREATE TABLE IF NOT EXISTS test_segment (
    segment_id              TEXT PRIMARY KEY,
    run_id                  TEXT NOT NULL,
    point_id                TEXT NOT NULL,   -- T1~T10
    order_index             INTEGER NOT NULL,
    attempt_index           INTEGER NOT NULL DEFAULT 1,
    status                  TEXT NOT NULL,   -- stabilizing | recording | completed | interrupted | discarded
    prepared_at_ms          INTEGER NOT NULL,
    recording_started_at_ms INTEGER NOT NULL,
    recording_ended_at_ms   INTEGER NOT NULL,
    actual_ended_at_ms      INTEGER,
    superseded              INTEGER NOT NULL DEFAULT 0,
    note                    TEXT
);

-- 원본 시계열. 비정상 값도 버리지 않고 valid=0 으로 보존한다. (계획서 §7.2 "Raw 데이터 보존")
-- run_id / segment_id 는 신규(user_version 1). 기존 DB 는 store.py 마이그레이션이 ALTER 로 추가.
--   C1~C4 이동 구간: run_id 있음, segment_id=NULL / Test 동시간: 둘 다 있음 / Legacy: 둘 다 NULL
--   (session_id 는 하위호환 유지. 새 Run 기반 저장은 session_id=run_id 로 채운다.)
CREATE TABLE IF NOT EXISTS measurement (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    experiment_id      TEXT    NOT NULL,
    session_id         TEXT    NOT NULL,
    run_id             TEXT,
    segment_id         TEXT,
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

-- 장치별 RSSI 편차 보정 (계획서 §4.3).
-- 사전·사후를 동시에 보존하려면 offset_run_id 로 구분해야 한다.
-- (experiment_id, node_id) 만으로 키를 잡으면 사후 계산이 사전값을 덮어쓰므로 안 됨.
CREATE TABLE IF NOT EXISTS device_offset (
    offset_run_id      TEXT NOT NULL,
    node_id            TEXT NOT NULL,
    experiment_id      TEXT NOT NULL,
    offset_median_dbm  REAL,
    device_offset_db   REAL,
    sample_count       INTEGER,
    std_db             REAL,
    calibrated_at_ms   INTEGER,
    PRIMARY KEY (offset_run_id, node_id)
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
