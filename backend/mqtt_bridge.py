import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt

from .config import Settings
from .database import Database
from .metrics import Metrics
from .state import NodeRegistry, WindowBuffer, now_ms
from .ws import WebSocketHub

logger = logging.getLogger(__name__)

class MqttBridge:
    def __init__(
        self,
        settings: Settings,
        loop: asyncio.AbstractEventLoop,
        db: Database,
        registry: NodeRegistry,
        window_buffer: WindowBuffer,
        hub: WebSocketHub,
        metrics: Metrics,
    ) -> None:
        self.settings = settings
        self.loop = loop
        self.db = db
        self.registry = registry
        self.window_buffer = window_buffer
        self.hub = hub
        self.metrics = metrics
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="jhkang-backend")
        if settings.mqtt_username:
            self.client.username_pw_set(settings.mqtt_username, settings.mqtt_password)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect

    def start(self) -> None:
        self.client.connect_async(self.settings.mqtt_host, self.settings.mqtt_port, keepalive=30)
        self.client.loop_start()
        logger.info("mqtt bridge connecting to %s:%s", self.settings.mqtt_host, self.settings.mqtt_port)

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        logger.info("mqtt connected: %s", reason_code)
        client.subscribe("rssi/#")
        client.subscribe("gateway/#")
        client.subscribe("status/+/lwt")

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        logger.warning("mqtt disconnected: %s", reason_code)

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        asyncio.run_coroutine_threadsafe(self.handle_message(msg.topic, msg.payload), self.loop)

    async def handle_message(self, topic: str, payload: bytes) -> None:
        receive_ms = now_ms()
        try:
            data = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("drop invalid json topic=%s payload=%r", topic, payload[:200])
            self.metrics.inc_dropped()
            return

        if topic.endswith("/lwt"):
            node_id = data.get("node_id") or topic.split("/")[1]
            status = self.registry.mark_online(str(node_id), bool(data.get("online", False)))
            await self.db.upsert_node_status(status)
            await self.hub.broadcast({"type": "node_status", "node": status})
            return

        server_dt = datetime.fromtimestamp(receive_ms / 1000, tz=timezone.utc)

        if topic.startswith("gateway/"):
            await self._handle_gateway_batch(data, receive_ms, server_dt)
            return

        self.metrics.inc_received()
        measurement = self._validate_measurement(data, receive_ms)
        if measurement is None:
            self.metrics.inc_dropped()
            return
        await self._ingest(measurement, receive_ms, server_dt)

    async def _handle_gateway_batch(self, data: dict[str, Any], receive_ms: int, server_dt: datetime) -> None:
        batch_ts = data.get("timestamp")
        readings = data.get("readings")
        if not isinstance(readings, list):
            logger.warning("drop gateway batch without 'readings' list: %s", data)
            self.metrics.inc_dropped()
            return
        for reading in readings:
            if not isinstance(reading, dict):
                self.metrics.inc_dropped()
                continue
            if reading.get("timestamp") is None and batch_ts is not None:
                reading = {**reading, "timestamp": batch_ts}
            self.metrics.inc_received()
            measurement = self._validate_measurement(reading, receive_ms)
            if measurement is None:
                self.metrics.inc_dropped()
                continue
            await self._ingest(measurement, receive_ms, server_dt)

    async def _ingest(self, measurement: dict[str, Any], receive_ms: int, server_dt: datetime) -> None:
        """검증된 측정값 1건을 상태갱신 + 동기화버퍼 + 저장 파이프라인에 투입."""
        status, became_online = self.registry.mark_seen(measurement["node_id"], measurement.get("seq"))
        self.window_buffer.add(measurement, receive_ms)
        self.db.enqueue_raw(measurement, server_dt)
        await self.db.upsert_node_status(status)
        if became_online:
            await self.hub.broadcast({"type": "node_status", "node": status})

    def _validate_measurement(self, data: dict[str, Any], receive_ms: int) -> dict[str, Any] | None:
        required = {"node_id", "timestamp", "rssi"}
        if not required <= data.keys():
            logger.warning("drop missing fields: %s", sorted(required - set(data.keys())))
            return None

        try:
            node_id = str(data["node_id"])
            timestamp = int(data["timestamp"])
            rssi = int(data["rssi"])
            seq = int(data["seq"]) if data.get("seq") is not None else None
            status_code = int(data.get("status", 0))
        except (TypeError, ValueError):
            logger.warning("drop invalid field types: %s", data)
            return None

        if not (self.settings.rssi_min <= rssi <= self.settings.rssi_max):
            logger.warning("drop out-of-range rssi node=%s rssi=%s", node_id, rssi)
            return None

        if abs(receive_ms - timestamp) > self.settings.timestamp_max_skew_ms:
            logger.warning("replace skewed node timestamp node=%s timestamp=%s", node_id, timestamp)
            timestamp = receive_ms

        def _f(key: str) -> float | None:
            v = data.get(key)
            try:
                return float(v) if v is not None else None
            except (TypeError, ValueError):
                return None

        return {
            "node_id": node_id,
            "timestamp": timestamp,
            "ap_bssid": data.get("ap_bssid"),
            "rssi": rssi,
            "rssi_raw": data.get("rssi_raw"),
            "seq": seq,
            "status": status_code,
            "pos_x": _f("pos_x"), "pos_y": _f("pos_y"), "pos_z": _f("pos_z"),
            "rot_w": _f("rot_w"), "rot_x": _f("rot_x"), "rot_y": _f("rot_y"), "rot_z": _f("rot_z"),
        }
