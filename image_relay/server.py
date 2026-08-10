"""JPEG 프레임 중계 서버 (측정 백엔드와 별개 프로세스).

    Graphics ─TCP(ingest)─▶ [ RelayServer ] ─TCP(viewer)─▶ Handheld / Viewer(들)

핵심: 뷰어별 큐는 1칸. 느린 뷰어가 밀리면 오래된 프레임을 버리고 최신만 유지한다
(지연 무한 누적·타 뷰어 블로킹 방지). producer/viewer 재접속 자유.
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
import time
from dataclasses import dataclass, field

from .protocol import ProtocolError, read_frame

logger = logging.getLogger("image_relay")


@dataclass
class Stats:
    frames_in: int = 0
    bytes_in: int = 0
    frames_out: int = 0
    frames_dropped: int = 0
    producers: int = 0
    viewers: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add_in(self, n_bytes: int) -> None:
        with self._lock:
            self.frames_in += 1
            self.bytes_in += n_bytes

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "frames_in": self.frames_in, "bytes_in": self.bytes_in,
                "frames_out": self.frames_out, "frames_dropped": self.frames_dropped,
                "producers": self.producers, "viewers": self.viewers,
            }


class Viewer:
    """뷰어 한 명 = 최신 프레임 1개만 담는 큐."""

    def __init__(self, addr: tuple[str, int], stats: Stats) -> None:
        self.addr = addr
        self.stats = stats
        self._q: queue.Queue[bytes] = queue.Queue(maxsize=1)

    def offer(self, frame_bytes: bytes) -> None:
        """최신 프레임으로 교체. 큐가 차 있으면 오래된 것을 버린다 (stale drop)."""
        try:
            self._q.put_nowait(frame_bytes)
        except queue.Full:
            try:
                self._q.get_nowait()
                with self.stats._lock:
                    self.stats.frames_dropped += 1
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(frame_bytes)
            except queue.Full:
                pass

    def get(self, timeout: float) -> bytes | None:
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None


class RelayServer:
    def __init__(self, ingest_port: int = 9101, viewer_port: int = 9102,
                 host: str = "0.0.0.0", stats_interval: float = 5.0,
                 viewer_sndbuf: int = 64 * 1024) -> None:
        self.ingest_port = ingest_port
        self.viewer_port = viewer_port
        self.host = host
        self.stats_interval = stats_interval
        # 송신 버퍼가 작을수록 느린 뷰어의 폐기가 일찍 걸려 지연이 덜 쌓인다. 0=OS 기본.
        self.viewer_sndbuf = viewer_sndbuf

        self.stats = Stats()
        self._viewers: set[Viewer] = set()
        self._viewers_lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._ingest_sock: socket.socket | None = None
        self._viewer_sock: socket.socket | None = None

    def publish(self, frame_bytes: bytes) -> None:
        with self._viewers_lock:
            targets = list(self._viewers)
        for v in targets:
            v.offer(frame_bytes)

    # -- ingest: 그래픽스 수신 -------------------------------------------
    def _serve_ingest(self) -> None:
        srv = _listen(self.host, self.ingest_port)
        self._ingest_sock = srv
        logger.info("ingest listening on %s:%d (graphics → here)", self.host, self.ingest_port)
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_producer, args=(conn, addr),
                             name=f"producer-{addr[1]}", daemon=True).start()

    def _handle_producer(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        logger.info("producer connected: %s", addr)
        with self.stats._lock:
            self.stats.producers += 1
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        conn.settimeout(10.0)
        try:
            recv = conn.recv
            while not self._stop.is_set():
                try:
                    frame = read_frame(recv)
                except (ProtocolError, socket.timeout) as exc:
                    logger.warning("producer %s 스트림 오류: %s → 연결 종료", addr, exc)
                    break
                except OSError:
                    break
                if frame is None:
                    break
                self.stats.add_in(len(frame.payload))
                self.publish(frame.raw)
        finally:
            conn.close()
            with self.stats._lock:
                self.stats.producers -= 1
            logger.info("producer disconnected: %s", addr)

    # -- viewer: 뷰어 fanout --------------------------------------------
    def _serve_viewers(self) -> None:
        srv = _listen(self.host, self.viewer_port)
        self._viewer_sock = srv
        logger.info("viewer listening on %s:%d (here → viewers)", self.host, self.viewer_port)
        while not self._stop.is_set():
            try:
                conn, addr = srv.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_viewer, args=(conn, addr),
                             name=f"viewer-{addr[1]}", daemon=True).start()

    def _handle_viewer(self, conn: socket.socket, addr: tuple[str, int]) -> None:
        logger.info("viewer connected: %s", addr)
        # 지연 최소화: Nagle off + 송신 버퍼 축소
        try:
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            if self.viewer_sndbuf > 0:
                conn.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, self.viewer_sndbuf)
        except OSError:
            pass
        viewer = Viewer(addr, self.stats)
        with self._viewers_lock:
            self._viewers.add(viewer)
        with self.stats._lock:
            self.stats.viewers += 1
        try:
            while not self._stop.is_set():
                frame_bytes = viewer.get(timeout=1.0)
                if frame_bytes is None:
                    continue  # 새 프레임 대기 (끊김 감지 위해 주기적으로 루프)
                try:
                    conn.sendall(frame_bytes)
                except OSError:
                    break
                with self.stats._lock:
                    self.stats.frames_out += 1
        finally:
            with self._viewers_lock:
                self._viewers.discard(viewer)
            with self.stats._lock:
                self.stats.viewers -= 1
            conn.close()
            logger.info("viewer disconnected: %s", addr)

    def _serve_stats(self) -> None:
        last = self.stats.snapshot()
        last_t = time.time()
        while not self._stop.wait(self.stats_interval):
            now = self.stats.snapshot()
            dt = time.time() - last_t
            fps_in = (now["frames_in"] - last["frames_in"]) / dt if dt else 0
            fps_out = (now["frames_out"] - last["frames_out"]) / dt if dt else 0
            logger.info(
                "stats | in %.1f fps, out %.1f fps | viewers=%d producers=%d "
                "| dropped(total)=%d | in_total=%d",
                fps_in, fps_out, now["viewers"], now["producers"],
                now["frames_dropped"], now["frames_in"],
            )
            last, last_t = now, time.time()

    def start(self) -> None:
        for target, name in ((self._serve_ingest, "ingest"),
                             (self._serve_viewers, "viewers"),
                             (self._serve_stats, "stats")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        for s in (self._ingest_sock, self._viewer_sock):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass

    def serve_forever(self) -> None:
        self.start()
        try:
            while not self._stop.is_set():
                time.sleep(0.5)
        except KeyboardInterrupt:
            logger.info("종료 요청 (Ctrl+C)")
        finally:
            self.stop()


def _listen(host: str, port: int) -> socket.socket:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(8)
    return srv
