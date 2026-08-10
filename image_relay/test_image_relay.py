"""중계 서버 검증 (실제 TCP 소켓, 외부 의존성 없음).

    pytest image_relay/test_image_relay.py -q
    python  image_relay/test_image_relay.py     # pytest 없이도 실행 가능
"""

from __future__ import annotations

import io
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from image_relay.protocol import (  # noqa: E402
    HEADER_SIZE, MAGIC, ProtocolError, encode_frame, read_frame,
)
from image_relay.server import RelayServer, Stats, Viewer  # noqa: E402


# ----------------------------------------------------------------------
# 프로토콜 단위 테스트
# ----------------------------------------------------------------------
def test_frame_roundtrip():
    payload = b"\xff\xd8hello-jpeg\xff\xd9"
    wire = encode_frame(payload, seq=42, ts_ms=1785720000000)
    assert len(wire) == HEADER_SIZE + len(payload)

    stream = io.BytesIO(wire)
    frame = read_frame(stream.read)
    assert frame is not None
    assert frame.seq == 42
    assert frame.ts_ms == 1785720000000
    assert frame.payload == payload


def test_fragmented_recv_is_reassembled():
    """TCP 조각화(한 번에 7B씩)에도 프레임이 온전해야 한다."""
    wire = encode_frame(b"A" * 5000, seq=1, ts_ms=1)
    box = {"data": wire}

    def choppy_recv(n: int) -> bytes:
        take = min(n, 7, len(box["data"]))
        out, box["data"] = box["data"][:take], box["data"][take:]
        return out

    frame = read_frame(choppy_recv)
    assert frame is not None and frame.seq == 1 and len(frame.payload) == 5000


def test_eof_returns_none():
    assert read_frame(io.BytesIO(b"").read) is None


def test_corrupt_magic_raises():
    bad = bytes([0, 0, 0, 0]) + encode_frame(b"x", 0, 0)[4:]
    try:
        read_frame(io.BytesIO(bad).read)
    except ProtocolError:
        return
    raise AssertionError("손상 magic 인데 ProtocolError 가 안 났다")


# ----------------------------------------------------------------------
# 서버 종단 테스트
# ----------------------------------------------------------------------
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _start_server() -> RelayServer:
    srv = RelayServer(ingest_port=_free_port(), viewer_port=_free_port(),
                      host="127.0.0.1", stats_interval=999)
    srv.start()
    time.sleep(0.3)  # listen 소켓이 뜰 시간
    return srv


def _connect(port: int) -> socket.socket:
    return socket.create_connection(("127.0.0.1", port), timeout=5)


def test_end_to_end_delivery():
    srv = _start_server()
    try:
        viewer = _connect(srv.viewer_port)
        time.sleep(0.2)  # 뷰어 등록 대기
        producer = _connect(srv.ingest_port)

        for seq in range(5):
            producer.sendall(encode_frame(f"jpeg-{seq}".encode(), seq, ts_ms=seq))
            time.sleep(0.05)

        got = []
        viewer.settimeout(3)
        for _ in range(5):
            frame = read_frame(viewer.recv)
            assert frame is not None
            got.append(frame.payload)
        assert got == [f"jpeg-{i}".encode() for i in range(5)]

        producer.close()
        viewer.close()
    finally:
        srv.stop()


def test_multi_viewer_fanout():
    srv = _start_server()
    try:
        v1, v2 = _connect(srv.viewer_port), _connect(srv.viewer_port)
        time.sleep(0.2)
        producer = _connect(srv.ingest_port)
        producer.sendall(encode_frame(b"broadcast", 7, ts_ms=7))

        for v in (v1, v2):
            v.settimeout(3)
            frame = read_frame(v.recv)
            assert frame is not None and frame.payload == b"broadcast" and frame.seq == 7

        producer.close(); v1.close(); v2.close()
    finally:
        srv.stop()


def test_viewer_buffer_keeps_only_latest():
    """1칸 큐가 stale 프레임을 버리고 최신만 유지 (결정론적, flaky 없음)."""
    stats = Stats()
    v = Viewer(("test", 0), stats)

    for seq in range(5):
        v.offer(encode_frame(f"f{seq}".encode(), seq, ts_ms=seq))

    latest = v.get(timeout=1.0)
    assert latest is not None
    frame = read_frame(io.BytesIO(latest).read)
    assert frame.seq == 4, f"최신이 아님: seq={frame.seq}"
    assert stats.frames_dropped == 4, f"dropped={stats.frames_dropped} (기대 4)"
    assert v.get(timeout=0.1) is None  # 남은 프레임 없음


def test_flood_drops_stale_frames_end_to_end():
    """안 읽는 뷰어에 대량 플러딩 → OS 버퍼 초과분은 폐기, seq 건너뜀 발생."""
    srv = _start_server()
    try:
        viewer = socket.socket()
        viewer.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        viewer.connect(("127.0.0.1", srv.viewer_port))
        time.sleep(0.2)
        producer = _connect(srv.ingest_port)

        total = 500
        for seq in range(total):  # 뷰어가 안 읽는 동안 빠르게 flood
            producer.sendall(encode_frame(b"x" * 8192, seq, ts_ms=seq))
        time.sleep(0.5)

        viewer.settimeout(3)
        received, last, max_gap = 0, -1, 0
        try:
            while True:
                frame = read_frame(viewer.recv)
                if frame is None:
                    break
                received += 1
                if last >= 0:
                    max_gap = max(max_gap, frame.seq - last)
                last = frame.seq
                if last >= total - 1:
                    break
        except socket.timeout:
            pass

        assert received > 0, "아무 프레임도 못 받음"
        assert received < total, f"폐기가 없었음: {received}/{total} 전부 수신"
        assert max_gap > 1, f"seq 건너뜀이 없음 (폐기 미발생): max_gap={max_gap}"
        assert last >= total - 1, f"최신 프레임에 도달 못 함: last seq={last}"

        producer.close(); viewer.close()
    finally:
        srv.stop()


# ----------------------------------------------------------------------
if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
