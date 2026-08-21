from __future__ import annotations

import argparse
import io
import socket
import struct
import sys
import time
from pathlib import Path

MAGIC = 0x52464A46  # 'RFJF'
VERSION = 1
_HEADER = struct.Struct(">IBBIQI")  # magic, version, flags, seq, ts_ms, length

try:
    from PIL import Image  # type: ignore
    _HAS_PIL = True
except Exception:  # noqa: BLE001
    _HAS_PIL = False


def encode_frame(payload: bytes, seq: int) -> bytes:
    ts_ms = int(time.time() * 1000)
    return _HEADER.pack(MAGIC, VERSION, 0, seq, ts_ms, len(payload)) + payload


def load_jpeg(path: Path, width: int, height: int, resize: bool,
              quality: int, subsampling: int, gray: bool) -> bytes:
    # ESP32 esp_jpeg 디코더는 baseline JPEG 만 처리한다(progressive 불가).
    # 원본이 어떤 형식이든 항상 baseline 으로 다시 인코딩해서 보낸다.
    if not _HAS_PIL:
        print("[error] Pillow 필요: baseline JPEG 로 변환하려면 설치해야 함.\n"
              "        pip install pillow")
        sys.exit(1)
    img = Image.open(path).convert("L" if gray else "RGB")
    if resize:
        img = img.resize((width, height))
    buf = io.BytesIO()
    save_kw = dict(format="JPEG", quality=quality,
                   progressive=False,  # baseline (ESP32 esp_jpeg 호환)
                   optimize=False)
    if not gray:
        save_kw["subsampling"] = subsampling  # 0=4:4:4, 1=4:2:2, 2=4:2:0
    img.save(buf, **save_kw)
    return buf.getvalue()


def main() -> None:
    p = argparse.ArgumentParser(description="JPEG 파일 하나를 중계로 전송")
    p.add_argument("image", help="보낼 JPEG 파일 경로 (예: 1234.jpg)")
    p.add_argument("--host", default="127.0.0.1", help="중계 서버 주소 (같은 PC면 127.0.0.1)")
    p.add_argument("--port", type=int, default=9101, help="ingest 포트 (기본 9101)")
    p.add_argument("--fps", type=float, default=5.0, help="초당 재전송 횟수 (기본 5)")
    p.add_argument("--width", type=int, default=800)
    p.add_argument("--height", type=int, default=480)
    p.add_argument("--no-resize", action="store_true", help="800x480 리사이즈 끄기")
    p.add_argument("--quality", type=int, default=70, help="JPEG 품질 (기본 70)")
    p.add_argument("--subsampling", type=int, default=2, choices=[0, 1, 2],
                   help="크로마 서브샘플링 0=4:4:4 1=4:2:2 2=4:2:0(기본)")
    p.add_argument("--gray", action="store_true", help="그레이스케일 baseline JPEG 로 전송")
    p.add_argument("--once", action="store_true", help="한 장만 보내고 종료")
    args = p.parse_args()

    path = Path(args.image)
    if not path.exists():
        print(f"[error] 파일 없음: {path.resolve()}")
        sys.exit(1)

    payload = load_jpeg(path, args.width, args.height, resize=not args.no_resize,
                        quality=args.quality, subsampling=args.subsampling, gray=args.gray)
    size = "원본크기" if args.no_resize else f"{args.width}x{args.height}"
    sub = "gray" if args.gray else {0: "4:4:4", 1: "4:2:2", 2: "4:2:0"}[args.subsampling]
    print(f"[send] {path.name} -> {args.host}:{args.port} | {len(payload)/1024:.0f} KiB | "
          f"baseline JPEG, {size}, q{args.quality}, {sub}")

    sock = socket.create_connection((args.host, args.port))
    print(f"[send] connected. Ctrl+C 로 종료.")
    interval = 1.0 / args.fps if args.fps > 0 else 0.0
    seq = 0
    try:
        while True:
            sock.sendall(encode_frame(payload, seq))
            seq += 1
            if args.once:
                print("[send] 한 장 전송 완료 (--once)")
                break
            if seq % max(int(args.fps), 1) == 0:
                print(f"[send] {seq} frames sent")
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        print(f"[send] done: {seq} frames")


if __name__ == "__main__":
    main()