CREATE TABLE IF NOT EXISTS rssi_raw (
    id          BIGSERIAL PRIMARY KEY,
    node_id     TEXT        NOT NULL,
    ap_bssid    TEXT,
    rssi        SMALLINT    NOT NULL,
    rssi_raw    SMALLINT,
    seq         BIGINT,
    node_ts     BIGINT,
    status      SMALLINT    NOT NULL DEFAULT 0,
    pos_x       REAL,        -- 측정 시점 위치 (m)
    pos_y       REAL,
    pos_z       REAL,
    rot_w       REAL,        -- 측정 시점 회전 쿼터니언
    rot_x       REAL,
    rot_y       REAL,
    rot_z       REAL,
    server_ts   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rssi_raw_node_ts
    ON rssi_raw (node_id, server_ts);

CREATE TABLE IF NOT EXISTS frame (
    id          BIGSERIAL PRIMARY KEY,
    window_ts   TIMESTAMPTZ NOT NULL,
    payload     JSONB       NOT NULL,
    node_count  SMALLINT
);

CREATE TABLE IF NOT EXISTS node_status (
    node_id        TEXT PRIMARY KEY,
    online         BOOLEAN     NOT NULL DEFAULT false,
    last_seen      TIMESTAMPTZ,
    msg_rate_hz    REAL,
    lost_packets   BIGINT      DEFAULT 0,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 정적 메타(선택): 고정 노드의 위치/회전. 이동 노드는 rssi_raw에 매 측정 저장됨.
CREATE TABLE IF NOT EXISTS node_meta (
    node_id     TEXT PRIMARY KEY,
    pos_x REAL, pos_y REAL, pos_z REAL,
    rot_w REAL, rot_x REAL, rot_y REAL, rot_z REAL,
    description TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
