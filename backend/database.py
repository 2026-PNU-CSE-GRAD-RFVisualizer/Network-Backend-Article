import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

class Database:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._raw_queue: asyncio.Queue | None = None

    @property
    def connected(self) -> bool:
        return self._pool is not None

    async def connect(self) -> None:
        self._raw_queue = asyncio.Queue(maxsize=20000)
        try:
            self._pool = await asyncpg.create_pool(self._dsn, min_size=1, max_size=5)
            await self.init_schema()
            logger.info("postgres connected")
        except Exception:
            self._pool = None
            logger.exception("postgres unavailable; running without persistence")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def init_schema(self) -> None:
        sql = """
        CREATE TABLE IF NOT EXISTS rssi_raw (
            id          BIGSERIAL PRIMARY KEY,
            node_id     TEXT        NOT NULL,
            ap_bssid    TEXT,
            rssi        SMALLINT    NOT NULL,
            rssi_raw    SMALLINT,
            seq         BIGINT,
            node_ts     BIGINT,
            status      SMALLINT    NOT NULL DEFAULT 0,
            pos_x       REAL,
            pos_y       REAL,
            pos_z       REAL,
            rot_w       REAL,
            rot_x       REAL,
            rot_y       REAL,
            rot_z       REAL,
            server_ts   TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        ALTER TABLE rssi_raw ADD COLUMN IF NOT EXISTS pos_x REAL;
        ALTER TABLE rssi_raw ADD COLUMN IF NOT EXISTS pos_y REAL;
        ALTER TABLE rssi_raw ADD COLUMN IF NOT EXISTS pos_z REAL;
        ALTER TABLE rssi_raw ADD COLUMN IF NOT EXISTS rot_w REAL;
        ALTER TABLE rssi_raw ADD COLUMN IF NOT EXISTS rot_x REAL;
        ALTER TABLE rssi_raw ADD COLUMN IF NOT EXISTS rot_y REAL;
        ALTER TABLE rssi_raw ADD COLUMN IF NOT EXISTS rot_z REAL;
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

        CREATE TABLE IF NOT EXISTS node_meta (
            node_id     TEXT PRIMARY KEY,
            pos_x       REAL,
            pos_y       REAL,
            pos_z       REAL,
            rot_w       REAL,
            rot_x       REAL,
            rot_y       REAL,
            rot_z       REAL,
            description TEXT,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        """
        if self._pool is not None:
            async with self._pool.acquire() as conn:
                await conn.execute(sql)

    def enqueue_raw(self, measurement: dict[str, Any], server_ts: datetime) -> None:
        if self._raw_queue is None:
            return
        try:
            self._raw_queue.put_nowait((measurement, server_ts))
        except asyncio.QueueFull:
            logger.warning("raw queue full; dropping measurement")

    async def flush_raw_loop(self, metrics: Any, interval_s: float = 0.5, max_batch: int = 500) -> None:
        while True:
            await asyncio.sleep(interval_s)
            if self._raw_queue is None:
                continue
            batch: list[tuple[dict[str, Any], datetime]] = []
            while not self._raw_queue.empty() and len(batch) < max_batch:
                batch.append(self._raw_queue.get_nowait())
            if not batch:
                continue
            if self._pool is None:
                continue
            records = [
                (
                    m["node_id"], m.get("ap_bssid"), m["rssi"], m.get("rssi_raw"),
                    m.get("seq"), m.get("timestamp"), int(m.get("status", 0)),
                    m.get("pos_x"), m.get("pos_y"), m.get("pos_z"),
                    m.get("rot_w"), m.get("rot_x"), m.get("rot_y"), m.get("rot_z"),
                    ts,
                )
                for m, ts in batch
            ]
            try:
                async with self._pool.acquire() as conn:
                    await conn.executemany(
                        """
                        INSERT INTO rssi_raw
                            (node_id, ap_bssid, rssi, rssi_raw, seq, node_ts, status,
                             pos_x, pos_y, pos_z, rot_w, rot_x, rot_y, rot_z, server_ts)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                        """,
                        records,
                    )
                metrics.add_rows(len(records))
            except Exception:
                logger.exception("batch insert failed (%d rows)", len(records))

    async def insert_frame(self, frame: dict[str, Any]) -> None:
        """실시간 경로 전용 (9월 졸업작품). 논문 실험에서는 호출되지 않는다."""
        if self._pool is None:
            return
        window_dt = datetime.fromtimestamp(frame["window_ts"] / 1000, tz=timezone.utc)
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO frame (window_ts, payload, node_count)
                VALUES ($1, $2::jsonb, $3)
                """,
                window_dt,
                json.dumps(frame),
                len(frame["nodes"]),
            )

    async def upsert_node_status(self, status: dict[str, Any]) -> None:
        if self._pool is None:
            return
        last_seen = status.get("last_seen")
        last_seen_dt = (
            datetime.fromtimestamp(last_seen / 1000, tz=timezone.utc)
            if last_seen is not None
            else None
        )
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO node_status
                    (node_id, online, last_seen, msg_rate_hz, lost_packets, updated_at)
                VALUES ($1, $2, $3, $4, $5, now())
                ON CONFLICT (node_id) DO UPDATE SET
                    online = EXCLUDED.online,
                    last_seen = EXCLUDED.last_seen,
                    msg_rate_hz = EXCLUDED.msg_rate_hz,
                    lost_packets = EXCLUDED.lost_packets,
                    updated_at = now()
                """,
                status["node_id"],
                status["online"],
                last_seen_dt,
                status["msg_rate_hz"],
                status["lost_packets"],
            )

    async def upsert_node_meta(self, meta: dict[str, Any]) -> None:
        if self._pool is None:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO node_meta
                    (node_id, pos_x, pos_y, pos_z, rot_w, rot_x, rot_y, rot_z, description, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
                ON CONFLICT (node_id) DO UPDATE SET
                    pos_x = EXCLUDED.pos_x, pos_y = EXCLUDED.pos_y, pos_z = EXCLUDED.pos_z,
                    rot_w = EXCLUDED.rot_w, rot_x = EXCLUDED.rot_x,
                    rot_y = EXCLUDED.rot_y, rot_z = EXCLUDED.rot_z,
                    description = EXCLUDED.description, updated_at = now()
                """,
                meta["node_id"],
                meta.get("pos_x"), meta.get("pos_y"), meta.get("pos_z"),
                meta.get("rot_w"), meta.get("rot_x"), meta.get("rot_y"), meta.get("rot_z"),
                meta.get("description"),
            )

    async def get_node_meta(self) -> list[dict[str, Any]]:
        if self._pool is None:
            return []
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT node_id, pos_x, pos_y, pos_z, rot_w, rot_x, rot_y, rot_z, description FROM node_meta ORDER BY node_id"
            )
            return [dict(r) for r in rows]
