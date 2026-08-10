"""테스트용 가짜 뷰어 (핸드헬드 대역). 수신 fps·지연·seq 간격 출력.

    python -m image_relay.fake_viewer --save-dir frames_out
    python -m image_relay.fake_viewer --slow 0.5      # stale-drop 확인
"""

from __future__ import annotations

import argparse
import os
import socket
import time

from .protocol import now_ms, read_frame


def main() -> None:
    p = argparse.ArgumentParser(description="가짜 뷰어")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9102)
    p.add_argument("--slow", type=float, default=0.0, help="프레임당 인위 지연(초)")
    p.add_argument("--save-dir", default="", help="받은 JPEG 저장 폴더")
    p.add_argument("--count", type=int, default=0, help="0이면 무한")
    args = p.parse_args()

    if args.save_dir:
        os.makedirs(args.save_dir, exist_ok=True)

    sock = socket.create_connection((args.host, args.port))
    print(f"[viewer] connected {args.host}:{args.port}"
          + (f", slow={args.slow}s" if args.slow else ""))
    recv = sock.recv
    n, last_seq, gaps, t0 = 0, None, 0, time.time()
    try:
        while args.count == 0 or n < args.count:
            frame = read_frame(recv)
            if frame is None:
                print("[viewer] 서버가 연결을 닫음")
                break
            n += 1
            latency = now_ms() - frame.ts_ms
            if last_seq is not None and frame.seq != last_seq + 1:
                gaps += 1  # seq 건너뜀 = 서버가 버린 프레임
            last_seq = frame.seq
            if args.save_dir:
                with open(os.path.join(args.save_dir, f"frame_{frame.seq:06d}.jpg"), "wb") as f:
                    f.write(frame.payload)
            if n % 10 == 0:
                dt = time.time() - t0
                print(f"[viewer] {n} frames, {n/dt:.1f} fps, "
                      f"last seq={frame.seq}, latency~{latency}ms, seq_gaps={gaps}")
            if args.slow:
                time.sleep(args.slow)
    except KeyboardInterrupt:
        pass
    finally:
        sock.close()
        dt = time.time() - t0
        print(f"[viewer] done: {n} frames in {dt:.1f}s ({n/dt:.1f} fps), "
              f"seq_gaps(=dropped by server)={gaps}")


if __name__ == "__main__":
    main()
