import json
from typing import Any

from fastapi import WebSocket

class WebSocketHub:
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._clients.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self._clients.discard(websocket)

    async def send(self, websocket: WebSocket, payload: dict[str, Any]) -> None:
        await websocket.send_text(json.dumps(payload))

    async def broadcast(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload)
        disconnected: list[WebSocket] = []
        for client in list(self._clients):
            try:
                await client.send_text(data)
            except Exception:
                disconnected.append(client)
        for client in disconnected:
            self.disconnect(client)
