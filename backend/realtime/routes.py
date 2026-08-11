"""실시간 경로의 라우트와 백그라운드 루프 — 9월 졸업작품 범위.

ENABLE_REALTIME=true 일 때만 main.py 가 붙인다. 논문 실험 경로는 이 파일에 의존하지 않는다.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..state import NodeRegistry, now_ms
from .window import WindowBuffer
from .ws import WebSocketHub


def build_router(hub: WebSocketHub, registry: NodeRegistry) -> APIRouter:
    router = APIRouter()

    @router.websocket("/frames")
    async def frames(websocket: WebSocket) -> None:
        await hub.connect(websocket)
        await hub.send(websocket, {"type": "node_status_snapshot", "nodes": registry.snapshot()})
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            hub.disconnect(websocket)

    @router.get("/position/latest")
    async def latest_position() -> dict[str, Any]:
        return {
            "timestamp": now_ms(),
            "position_x": None,
            "position_y": None,
            "position_z": None,
            "confidence": 0.0,
            "status": "interface_ready_algorithm_pending",
        }

    return router


async def flush_windows(window_buffer: WindowBuffer, registry: NodeRegistry,
                        hub: WebSocketHub, metrics: Any,
                        insert_frame: Callable, flush_interval_ms: int) -> None:
    """마감된 버킷을 프레임으로 만들어 저장·전송한다."""
    while True:
        start = time.perf_counter()
        frames = window_buffer.pop_ready(now_ms(), registry.known_nodes())
        if frames:
            emit_ms = now_ms()
            for frame in frames:
                for node in frame["nodes"].values():
                    srv = node.get("server_receive_ms")
                    if srv is not None:
                        metrics.observe_collect_latency(emit_ms - srv)
                await insert_frame(frame)
                await hub.broadcast(frame)
            metrics.inc_frames(len(frames))
            metrics.observe_flush_ms((time.perf_counter() - start) * 1000)
        await asyncio.sleep(flush_interval_ms / 1000)
