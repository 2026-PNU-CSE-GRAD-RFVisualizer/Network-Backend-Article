"""이 모듈은 backend/realtime/ws.py 로 이동했다.

기존 import 를 깨뜨리지 않기 위한 재수출 shim.
새 코드는 `from .realtime import WebSocketHub` 를 사용한다.
"""

from .realtime.ws import WebSocketHub

__all__ = ["WebSocketHub"]
