"""실시간 시각화 경로 — 9월 졸업작품 범위 (ENABLE_REALTIME=true 일 때만).

포함: 200ms Window(WindowBuffer), WebSocket push(WS /frames), frame 저장,
PositionEstimate(/position/latest). 논문 30초 정지 측정에는 사용하지 않는다.

WindowBuffer 는 순수 로직(fastapi 없이 테스트 가능), WebSocketHub 는 fastapi 의존이라 지연 로딩.
"""

from typing import TYPE_CHECKING

from .window import WindowBuffer

if TYPE_CHECKING:  # pragma: no cover
    from .ws import WebSocketHub

__all__ = ["WindowBuffer", "WebSocketHub"]


def __getattr__(name: str):
    if name == "WebSocketHub":
        from .ws import WebSocketHub as _hub
        return _hub
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
