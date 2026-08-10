"""테스트용 가짜 그래픽스 producer. Pillow 있으면 실제 JPEG, 없으면 합성 바이트.

    python -m image_relay.fake_producer --fps 10 --count 40
"""

from __future__ import annotations

import argparse
import io
import socket
import time

from .protocol import encode_frame

try:
    from PIL import Image, ImageDraw  # type: ignore
    _HAS_PIL = True
except Exception:  # noqa: BLE001
    _HAS_PIL = False


def make_jpeg(seq: int, w: int = 800, h: int = 480) -> bytes:
    # 기본 800x480 = 임베디드 디스플레이 패널 해상도
    if _HAS_PIL:
        img = Image.new("RGB", (w, h), (20, 30 + (seq * 7) % 200, 60))
        d = ImageDraw.Draw(img)
        d.text((20, 20), f"frame #{seq}", fill=(255, 255, 255))
        d.text((20, 40), f"{time.strftime('%H:%M:%S')}", fill=(255, 255, 0))
        d.text((20, 60), f"{w}x{h}", fill=(0, 255, 255))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        return buf.getvalue()
    body = bytes((seq * 13 + i) % 256 for i in range(8000 + (seq % 40) * 100))
    return b"\xff\xd8\xff\xe0" + body + b"\xff\xd9"  # 더미 JPEG 마커 + 바디


def main() -> None:
    p = argparse.ArgumentParser(description="가짜 그래픽스 producer")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9101)
    p.add_argument("--fps", type=float, default=10.0)
    p.add_argument("--count", type=int, default=0, help="0이면 무한")
    p.add_argument("--width", type=int, default=800)
    p.add_argument("--height", type=int, default=480)
    args = p.parse_args()

    interval = 1.0 / args.fps if args.fps > 0 else 0.0
    sock = socket.create_connection((args.host, args.port))
    print(f"[producer] connected {args.host}:{args.port}, {args.fps} fps, "
          f"{args.width}x{args.height}, jpeg={'PIL' if _HAS_PIL else 'synthetic'}")
    seq, sent_bytes, t0 = 0, 0, time.time()
    try:
        while args.count == 0 or seq < args.count:
            payload = make_jpeg(seq, args.width, args.height)
            sock.sendall(encode_frame(payload, seq))
            sent_bytes += len(payload)
            seq += 1
            if seq % args.fps == 0:
                print(f"[producer] sent {seq} frames, {sent_bytes/1024:.0f} KiB")
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        dt = time.time() - t0
        print(f"[producer] done: {seq} frames in {dt:.1f}s ({seq/dt:.1f} fps)")


if __name__ == "__main__":
    main()
