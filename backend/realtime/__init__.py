"""실시간 시각화 경로 — 9월 졸업작품 범위.

논문 실험(7/23 강의실 30초 정지 측정)에는 사용하지 않는다.
`ENABLE_REALTIME=true` 일 때만 활성화된다.

포함:
  - 200ms Time Window 동기화 (WindowBuffer)
  - WebSocket 프레임 push (WebSocketHub, WS /frames)
  - frame 테이블 저장
  - PositionEstimate 인터페이스 (/position/latest)

논문 제출 후 이 패키지를 다시 켜고 그래픽스 파트의 실시간 뷰어와 연결한다.

주의: WebSocketHub 는 fastapi 에 의존하므로 지연 로딩한다.
WindowBuffer 는 순수 로직이라 fastapi 없이도 단위 테스트할 수 있어야 한다.
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
