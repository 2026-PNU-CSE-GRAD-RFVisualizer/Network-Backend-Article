"""10 FPS 경로: 800x480 RGB332 원시 프레임(384,000B)을 zlib 압축해 flags=1 로 전송.

ESP32 가 JPEG 디코딩(약 190ms)을 건너뛰도록, 이미지를 미리 RGB332 로 변환한다.
ESP32 는 payload 를 zlib 해제 → 정확히 384,000B(=800*480, 1B/픽셀) → 그대로 LCD 출력.

    py send_rgb332.py 1234.jpg --host 127.0.0.1 --port 9101 --fps 10
    py send_rgb332.py 1234.jpg --once

와이어 포맷(RFJF, big-endian) — JPEG 경로와 헤더 동일, flags 만 1:
    magic     uint32  0x52464A46 ('RFJF')
    version   uint8   1
    flags     uint8   1          # 1 = RGB332 raw + zlib (JPEG 는 0)
    seq       uint32
    ts_ms     uint64
    length    uint32             # zlib 압축된 바이트 수
    payload   byte[length]       # zlib.compress(RGB332 384,000B)

RGB332 픽셀 배열(1바이트/픽셀, 행 우선 y→x, 800*480):
    bit  7 6 5 | 4 3 2 | 1 0
         R R R   G G G   B B
    byte = (R & 0xE0) | ((G >> 3) & 0x1C) | (B >> 6)
해제 후 크기가 정확히 384,000B 가 아니면 프레임을 버리면 된다.
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
import time
import zlib
from pathlib import Path

MAGIC = 0x52464A46  # 'RFJF'
VERSION = 1
FLAG_RGB332_ZLIB = 1
_HEADER = struct.Struct(">IBBIQI")  # magic, version, flags, seq, ts_ms, length

W, H = 800, 480
RAW_SIZE = W * H  # 384000 (1 byte/pixel)

try:
    import numpy as np  # type: ignore
    from PIL import Image  # type: ignore
    _HAS_DEPS = True
except Exception:  # noqa: BLE001
    _HAS_DEPS = False


def _compress(raw: bytes, level: int, raw_deflate: bool) -> bytes:
    if raw_deflate:
        # RFC1951 raw deflate: zlib 헤더/adler32 없음 (miniz/tinfl 기대 시).
        co = zlib.compressobj(level, zlib.DEFLATED, -15)
        return co.compress(raw) + co.flush()
    # RFC1950 zlib 스트림: 2바이트 헤더 + deflate + adler32 (기본).
    return zlib.compress(raw, level)


def _pack_rgb332(r, g, b, order: str):
    # 3-3-2 패킹. 색이 밴드로 깨지면 패널이 기대하는 비트 순서로 바꿔 시험.
    if order == "rgb":   # RRRGGGBB (상위=R)
        return (r & 0xE0) | ((g >> 3) & 0x1C) | (b >> 6)
    if order == "bgr":   # BBBGGGRR (상위=B)
        return (b & 0xE0) | ((g >> 3) & 0x1C) | (r >> 6)
    if order == "brg":   # BBBRRRGG
        return (b & 0xE0) | ((r >> 3) & 0x1C) | (g >> 6)
    raise ValueError(order)


def to_rgb332_zlib(path: Path, level: int, raw_deflate: bool, order: str) -> bytes:
    if not _HAS_DEPS:
        print("[error] numpy + Pillow 필요:  pip install numpy pillow")
        sys.exit(1)
    img = Image.open(path).convert("RGB").resize((W, H))
    arr = np.asarray(img, dtype=np.uint8)          # (480, 800, 3)
    r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
    rgb332 = _pack_rgb332(r, g, b, order).astype(np.uint8)
    raw = rgb332.tobytes()                          # 384000 B, 행 우선
    assert len(raw) == RAW_SIZE, len(raw)
    return _compress(raw, level, raw_deflate)


def encode_frame(payload: bytes, seq: int) -> bytes:
    ts_ms = int(time.time() * 1000)
    return _HEADER.pack(MAGIC, VERSION, FLAG_RGB332_ZLIB, seq, ts_ms, len(payload)) + payload


def main() -> None:
    p = argparse.ArgumentParser(description="RGB332+zlib(flags=1) 프레임 전송 — 10 FPS 경로")
    p.add_argument("image", help="보낼 이미지 파일 경로")
    p.add_argument("--host", default="127.0.0.1", help="중계 서버 주소 (같은 PC면 127.0.0.1)")
    p.add_argument("--port", type=int, default=9101, help="ingest 포트 (기본 9101)")
    p.add_argument("--fps", type=float, default=10.0, help="초당 전송 프레임 (기본 10)")
    p.add_argument("--level", type=int, default=6, choices=range(0, 10),
                   help="zlib 압축 레벨 0~9 (기본 6)")
    p.add_argument("--raw-deflate", action="store_true",
                   help="zlib 헤더/adler32 없는 raw deflate(RFC1951)로 압축 (기본 zlib 권장)")
    p.add_argument("--bit-order", default="rgb", choices=["rgb", "bgr", "brg"],
                   help="RGB332 비트 배치: rgb=RRRGGGBB(기본) bgr=BBBGGGRR brg=BBBRRRGG")
    p.add_argument("--once", action="store_true", help="한 프레임만 보내고 종료")
    args = p.parse_args()

    path = Path(args.image)
    if not path.exists():
        print(f"[error] 파일 없음: {path.resolve()}")
        sys.exit(1)

    # 정지 이미지이므로 압축 프레임을 한 번만 만들어 재사용 (실시간 영상이면 프레임마다 변환).
    payload = to_rgb332_zlib(path, args.level, args.raw_deflate, args.bit_order)
    ratio = RAW_SIZE / len(payload) if payload else 0
    fmt = "raw deflate(RFC1951)" if args.raw_deflate else "zlib(RFC1950)"
    print(f"[rgb332] {path.name} -> {args.host}:{args.port} | flags=1 | "
          f"raw {RAW_SIZE//1000}KB → {fmt} {len(payload)/1024:.1f} KiB "
          f"(x{ratio:.0f} 압축, level {args.level}) | {W}x{H} | bits={args.bit_order}")

    # 자기검증: 실제로 보내는 바이트가 RFC1950 zlib 인지, 해제하면 384000B 인지 확인.
    head = payload[:2].hex()
    is_zlib = payload[:1] == b"\x78"           # RFC1950 은 0x78 로 시작 (78 9c / 78 da 등)
    print(f"[check] 압축 헤더 첫 2바이트 = {head}  → "
          f"{'RFC1950 zlib ✅' if is_zlib else 'zlib 헤더 아님 ❌ (ESP32 는 zlib 를 기대함!)'}")
    try:
        dec = zlib.decompress(payload) if not args.raw_deflate else zlib.decompress(payload, -15)
        print(f"[check] 해제 크기 = {len(dec)} B "
              f"{'(정확히 384000 ✅)' if len(dec)==RAW_SIZE else '(384000 아님 ❌)'}")
    except Exception as e:  # noqa: BLE001
        print(f"[check] 해제 실패: {e}")
    if args.raw_deflate:
        print("[check] ⚠ raw-deflate 모드입니다. ESP32 는 RFC1950 zlib 를 기대하므로 "
              "이 옵션을 빼고 실행하세요.")

    sock = socket.create_connection((args.host, args.port))
    print("[rgb332] connected. Ctrl+C 로 종료.")
    interval = 1.0 / args.fps if args.fps > 0 else 0.0
    seq, t0 = 0, time.time()
    try:
        while True:
            sock.sendall(encode_frame(payload, seq))
            seq += 1
            if args.once:
                print("[rgb332] 한 프레임 전송 완료 (--once)")
                break
            if seq % max(int(args.fps), 1) == 0:
                dt = time.time() - t0
                print(f"[rgb332] {seq} frames, {seq/dt:.1f} fps")
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        dt = time.time() - t0
        print(f"[rgb332] done: {seq} frames in {dt:.1f}s ({seq/dt:.1f} fps)")


if __name__ == "__main__":
    main()
