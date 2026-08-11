import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt

from .config import Settings
from .database import Database
from .experiment import SessionManager
from .metrics import Metrics
from .parsing import ParseConfig, parse_measurement
from .realtime import WebSocketHub, WindowBuffer
from .state import NodeRegistry, now_ms
from .store import ExperimentStore

logger = logging.getLogger(__name__)

class MqttBridge:
    def __init__(
        self,
        settings: Settings,
        loop: asyncio.AbstractEventLoop,
        db: Database,
        registry: NodeRegistry,
        window_buffer: WindowBuffer | None,
        hub: WebSocketHub,
        metrics: Metrics,
        store: ExperimentStore | None = None,
        sessions: SessionManager | None = None,
    ) -> None:
        self.settings = settings
        self.loop = loop
        self.db = db
        self.registry = registry
        self.window_buffer = window_buffer
        self.hub = hub
        self.metrics = metrics
        self.store = store
        self.sessions = sessions
        # 연결 상태: "샘플이 안 들어오는 이유"(브로커 죽음 vs ESP32 죽음)를 구분하려면 필요
        self.connected = False
        self.connect_count = 0
        self.disconnect_count = 0
        self.last_connected_ms: int | None = None
        self.last_disconnected_ms: int | None = None
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
        self.connected = True
        self.connect_count += 1
        self.last_connected_ms = now_ms()
        # 재연결 시 구독을 다시 걸어야 한다(안 걸면 연결은 살아있는데 메시지가 안 옴).
        client.subscribe("rssi/#")
        client.subscribe("gateway/#")
        client.subscribe("status/+/lwt")
        if self.connect_count > 1:
            logger.warning("mqtt RECONNECTED (%d번째): %s — 구독 재설정 완료",
                           self.connect_count, reason_code)
        else:
            logger.info("mqtt connected: %s", reason_code)

    def _on_disconnect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        self.connected = False
        self.disconnect_count += 1
        self.last_disconnected_ms = now_ms()
        active = self.sessions.active() if self.sessions else None
        if active is not None:
            logger.error("mqtt DISCONNECTED 측정 중! point=%s — 이 위치는 재측정이 필요할 수 있다: %s",
                         active.point_id, reason_code)
        else:
            logger.warning("mqtt disconnected: %s", reason_code)

    def status(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "connect_count": self.connect_count,
            "disconnect_count": self.disconnect_count,
            "last_connected_ms": self.last_connected_ms,
            "last_disconnected_ms": self.last_disconnected_ms,
        }

    def _on_message(self, client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
        asyncio.run_coroutine_threadsafe(self.handle_message(msg.topic, msg.payload), self.loop)

    async def handle_message(self, topic: str, payload: bytes) -> None:
        receive_ms = now_ms()

        # 비상 경로(§12): 파싱 성공 여부와 무관하게 원본을 먼저 JSONL 에 남긴다.
        if self.store is not None and not topic.endswith("/lwt"):
            active = self.sessions.active() if self.sessions else None
            self.store.append_jsonl(
                topic,
                payload.decode("utf-8", errors="replace"),
                receive_ms,
                active.session_id if active else None,
                active.point_id if active else None,
            )

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
        """측정 1건을 실험 저장소 + 실시간 파이프라인에 투입. 범위 밖 값도 valid=0 으로 기록(§7.2)."""
        self._record_experiment(measurement, receive_ms)

        if not measurement["valid"]:
            self.metrics.inc_dropped()
            return

        status, became_online = self.registry.mark_seen(measurement["node_id"], measurement.get("seq"))
        if self.window_buffer is not None:   # 실시간 경로가 켜져 있을 때만
            self.window_buffer.add(measurement, receive_ms)
        self.db.enqueue_raw(measurement, server_dt)
        await self.db.upsert_node_status(status)
        if became_online:
            await self.hub.broadcast({"type": "node_status", "node": status})

    def _record_experiment(self, measurement: dict[str, Any], receive_ms: int) -> None:
        """세션이 활성일 때만 실험 테이블에 기록한다."""
        if self.store is None or self.sessions is None:
            return
        active = self.sessions.active()
        if active is None:
            return
        # 세션 마감 시각을 넘겨 도착한 샘플은 그 위치의 창에 속하지 않는다.
        if receive_ms > active.deadline_ms:
            return
        # 위치는 세션 라벨이 아니라 노드의 현재 배치로 정한다(고정 센서는 매 세션 누적).
        point_id, point_role = self.sessions.resolve(measurement["node_id"], active)
        self.store.insert_measurements([{
            "experiment_id": active.experiment_id,
            "session_id": active.session_id,
            "point_id": point_id,
            "point_role": point_role,
            "node_id": measurement["node_id"],
            "node_ts_ms": measurement.get("timestamp"),
            "server_ts_ms": receive_ms,
            "seq": measurement.get("seq"),
            "rssi_raw_dbm": measurement.get("rssi_raw"),
            "rssi_filtered_dbm": measurement.get("rssi_filtered"),
            "sample_count": measurement.get("sample_count"),
            "error_flags": measurement.get("status", 0),
            "ap_bssid": measurement.get("ap_bssid"),
            "ap_channel": measurement.get("ap_channel"),
            "valid": measurement["valid"],
            "invalid_reason": measurement.get("invalid_reason"),
        }])

    def _validate_measurement(self, data: dict[str, Any], receive_ms: int) -> dict[str, Any] | None:
        """파싱은 parsing.py 순수 함수에 위임(브로커 없이 테스트 가능)."""
        return parse_measurement(data, receive_ms, ParseConfig(
            rssi_min=self.settings.rssi_min,
            rssi_max=self.settings.rssi_max,
            timestamp_max_skew_ms=self.settings.timestamp_max_skew_ms,
            rssi_filtered_scale=self.settings.rssi_filtered_scale,
        ))
