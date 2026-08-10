"""python -m image_relay 진입점."""

from __future__ import annotations

import argparse
import logging

from .server import RelayServer


def main() -> None:
    p = argparse.ArgumentParser(description="JPEG 프레임 중계 서버")
    p.add_argument("--host", default="0.0.0.0",
                   help="바인드 주소 (기본 0.0.0.0, 모든 인터페이스)")
    p.add_argument("--ingest-port", type=int, default=9101,
                   help="그래픽스가 접속해 프레임을 보내는 포트 (기본 9101)")
    p.add_argument("--viewer-port", type=int, default=9102,
                   help="뷰어/핸드헬드가 접속해 프레임을 받는 포트 (기본 9102)")
    p.add_argument("--stats-interval", type=float, default=5.0,
                   help="통계 로그 주기(초) (기본 5)")
    p.add_argument("-v", "--verbose", action="store_true", help="디버그 로그")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    server = RelayServer(
        ingest_port=args.ingest_port,
        viewer_port=args.viewer_port,
        host=args.host,
        stats_interval=args.stats_interval,
    )
    logging.getLogger("image_relay").info(
        "중계 서버 시작 — ingest=%d, viewer=%d. Ctrl+C 로 종료.",
        args.ingest_port, args.viewer_port,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
