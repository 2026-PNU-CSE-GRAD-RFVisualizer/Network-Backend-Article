"""장시간 내구 시험 모니터 (전문가 자문 반영).

2분대(708건) 시험에서는 손실률 추세나 장시간 장애가 드러나지 않는다는 지적에 따라,
실행 중인 백엔드의 /metrics · /nodes/status · /health 를 주기적으로 폴링해
시계열 CSV(순번 손실·재연결·수집지연·노드 상태)를 기록한다.

30분 이상 연속 측정하면서 실행:
    python endurance_monitor.py --duration 1800 --interval 10

브로커·백엔드·부하 시뮬레이터를 띄운 뒤 함께 돌리면 된다:
    python load_test.py --nodes 5 --rate 1 --duration 1800

주의: ESP32 heap 잔량은 임베디드 파트가 기록해야 한다(백엔드에서 관측 불가).
백엔드 쪽 장시간 장애 지표(손실률 추세·재연결·수집지연 발산)는 이 도구가 담당한다.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
import urllib.request
from typing import Any

FIELDS = [
    "elapsed_s", "timestamp", "ok",
    "messages_received", "messages_dropped", "drop_rate",
    "frames_emitted", "raw_rows_written",
    "collect_latency_avg_ms", "collect_latency_p95_ms", "collect_latency_max_ms",
    "mqtt_connected", "mqtt_connect_count", "mqtt_disconnect_count",
    "nodes_total", "nodes_online", "total_lost_packets", "min_msg_rate_hz",
]


def build_row(elapsed_s: int, metrics: dict[str, Any], nodes: list[dict[str, Any]],
              health: dict[str, Any], ok: bool = True) -> dict[str, Any]:
    """폴링 응답 3종을 시계열 한 행으로 정리한다 (순수 함수, 테스트 가능)."""
    cl = metrics.get("collect_latency_ms", {}) or {}
    mqtt = health.get("mqtt", {}) or {}
    online = sum(1 for n in nodes if n.get("online"))
    lost = sum(int(n.get("lost_packets", 0) or 0) for n in nodes)
    rates = [float(n.get("msg_rate_hz", 0) or 0) for n in nodes]
    return {
        "elapsed_s": elapsed_s,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "ok": int(bool(ok)),
        "messages_received": metrics.get("messages_received", 0),
        "messages_dropped": metrics.get("messages_dropped", 0),
        "drop_rate": metrics.get("drop_rate", 0.0),
        "frames_emitted": metrics.get("frames_emitted", 0),
        "raw_rows_written": metrics.get("raw_rows_written", 0),
        "collect_latency_avg_ms": cl.get("avg", 0.0),
        "collect_latency_p95_ms": cl.get("p95", 0.0),
        "collect_latency_max_ms": cl.get("max", 0.0),
        "mqtt_connected": int(bool(mqtt.get("connected"))),
        "mqtt_connect_count": mqtt.get("connect_count", 0),
        "mqtt_disconnect_count": mqtt.get("disconnect_count", 0),
        "nodes_total": len(nodes),
        "nodes_online": online,
        "total_lost_packets": lost,
        "min_msg_rate_hz": round(min(rates), 3) if rates else 0.0,
    }


def _get(url: str, timeout: float = 5.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def poll_once(base: str, elapsed_s: int) -> dict[str, Any]:
    """엔드포인트 3종을 읽어 한 행을 만든다. 실패하면 ok=0 행을 남긴다."""
    try:
        metrics = _get(f"{base}/metrics")
        nodes = _get(f"{base}/nodes/status").get("nodes", [])
        health = _get(f"{base}/health")
        return build_row(elapsed_s, metrics, nodes, health, ok=True)
    except Exception as exc:  # noqa: BLE001 — 장시간 시험 중 순단은 기록하고 계속
        row = build_row(elapsed_s, {}, [], {}, ok=False)
        row["timestamp"] = f"{time.strftime('%Y-%m-%d %H:%M:%S')} (fetch 실패: {exc})"
        return row


def main() -> None:
    p = argparse.ArgumentParser(description="장시간 내구 시험 모니터")
    p.add_argument("--url", default="http://127.0.0.1:8000", help="백엔드 주소")
    p.add_argument("--interval", type=float, default=10.0, help="폴링 주기(초)")
    p.add_argument("--duration", type=float, default=1800.0, help="총 시험 시간(초, 기본 30분)")
    p.add_argument("--out", default="", help="CSV 경로 (미지정 시 자동 생성)")
    args = p.parse_args()

    base = args.url.rstrip("/")
    out = args.out or f"endurance_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    print(f"[endurance] {base} 를 {args.interval}s 마다 {args.duration/60:.0f}분간 기록 → {out}")

    start = time.time()
    first_disc = None
    max_drop = 0.0
    max_p95 = 0.0
    fetch_fails = 0
    with open(out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        while time.time() - start < args.duration:
            elapsed = int(time.time() - start)
            row = poll_once(base, elapsed)
            writer.writerow(row)
            f.flush()  # 중간에 죽어도 기록이 남게

            if not row["ok"]:
                fetch_fails += 1
            else:
                first_disc = row["mqtt_disconnect_count"] if first_disc is None else first_disc
                max_drop = max(max_drop, float(row["drop_rate"]))
                max_p95 = max(max_p95, float(row["collect_latency_p95_ms"]))
                print(f"[{elapsed:5d}s] online={row['nodes_online']}/{row['nodes_total']} "
                      f"drop={row['drop_rate']} lost={row['total_lost_packets']} "
                      f"lat_p95={row['collect_latency_p95_ms']}ms "
                      f"reconn={row['mqtt_disconnect_count']}")
            time.sleep(max(0.0, args.interval - (time.time() - start) % args.interval))

    print("\n===== 내구 시험 요약 =====")
    print(f"기록 파일     : {out}")
    print(f"최대 드롭률    : {max_drop}")
    print(f"최대 수집지연 p95: {max_p95} ms")
    print(f"폴링 실패 횟수  : {fetch_fails}")
    print("재연결/순번손실 추세는 CSV 의 mqtt_disconnect_count, total_lost_packets 열 참고.")


if __name__ == "__main__":
    main()
