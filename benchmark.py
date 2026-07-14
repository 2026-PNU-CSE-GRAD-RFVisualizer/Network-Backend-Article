"""성능 측정 스크립트.

노드 수를 늘려가며 부하를 주고, 각 단계마다 백엔드 /metrics 를 읽어
수집 지연·손실률·프레임 생성 시간을 표(Markdown)로 출력한다.

사용법 (브로커 + 백엔드가 켜져 있어야 함):
    python benchmark.py
    python benchmark.py --counts 2,4,8,12,16 --rate 10 --duration 20

결과는 화면에 Markdown 표로 출력되고 benchmark_result.md 로도 저장된다.
"""
import argparse
import json
import random
import threading
import time
import urllib.request

import paho.mqtt.client as mqtt

counts_sent: dict[str, int] = {}

def get_metrics(api_base: str) -> dict:
    with urllib.request.urlopen(api_base + "/metrics", timeout=5) as r:
        return json.load(r)

def run_node(node_id: str, host: str, port: int, rate: float, duration: float, loss: float) -> None:
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"bench-{node_id}")
    client.connect(host, port, keepalive=30)
    client.loop_start()
    seq = 0
    sent = 0
    interval = 1.0 / rate
    end = time.time() + duration
    while time.time() < end:
        seq += 1
        if random.random() >= loss:
            payload = {
                "node_id": node_id,
                "timestamp": int(time.time() * 1000),
                "ap_bssid": "AA:BB:CC:DD:EE:FF",
                "rssi": random.randint(-85, -40),
                "seq": seq,
                "status": 0,
            }
            client.publish(f"rssi/{node_id}", json.dumps(payload), qos=0)
            sent += 1
        time.sleep(interval)
    client.loop_stop()
    client.disconnect()
    counts_sent[node_id] = sent

def bench_step(n: int, host: str, port: int, rate: float, duration: float, loss: float, api: str) -> dict:
    global counts_sent
    counts_sent = {}
    before = get_metrics(api)
    threads = [
        threading.Thread(target=run_node, args=(f"node-{i:02d}", host, port, rate, duration, loss), daemon=True)
        for i in range(1, n + 1)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    time.sleep(1.5)
    after = get_metrics(api)

    sent = sum(counts_sent.values())
    recv = after["messages_received"] - before["messages_received"]
    loss_rate = (sent - recv) / sent if sent else 0.0
    cl = after["collect_latency_ms"]
    fl = after["window_flush_ms"]
    return {
        "nodes": n, "sent": sent, "recv": recv, "loss_rate": loss_rate,
        "cl_avg": cl["avg"], "cl_p95": cl["p95"], "cl_max": cl["max"],
        "fl_avg": fl["avg"], "fl_max": fl["max"],
    }

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1", help="MQTT 브로커 IP")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--api", default="http://127.0.0.1:8000", help="백엔드 주소")
    ap.add_argument("--counts", default="2,4,8,12", help="측정할 노드 수들 (콤마)")
    ap.add_argument("--rate", type=float, default=10.0, help="노드당 초당 발행 수")
    ap.add_argument("--duration", type=float, default=20.0, help="단계별 측정 시간(초)")
    ap.add_argument("--loss", type=float, default=0.0, help="시뮬레이션 손실 확률")
    a = ap.parse_args()

    node_counts = [int(x) for x in a.counts.split(",")]
    print(f"측정 시작: counts={node_counts} rate={a.rate}Hz duration={a.duration}s\n")

    rows = []
    for n in node_counts:
        print(f"  [{n} 노드] 측정 중... ({a.duration}s)")
        rows.append(bench_step(n, a.host, a.port, a.rate, a.duration, a.loss, a.api))

    header = (
        "| 노드 수 | 발행(개) | 수신(개) | 손실률 | 수집지연 평균/p95/최대 (ms) | 프레임생성 평균/최대 (ms) |\n"
        "|---|---|---|---|---|---|\n"
    )
    lines = ""
    for r in rows:
        lines += (
            f"| {r['nodes']} | {r['sent']} | {r['recv']} | {r['loss_rate']*100:.2f}% "
            f"| {r['cl_avg']} / {r['cl_p95']} / {r['cl_max']} "
            f"| {r['fl_avg']} / {r['fl_max']} |\n"
        )
    table = header + lines

    print("\n===== 결과 표 (Markdown) =====\n")
    print(table)
    with open("benchmark_result.md", "w", encoding="utf-8") as f:
        f.write("# 성능 측정 결과\n\n")
        f.write(f"- 조건: 노드당 {a.rate}Hz, 단계별 {a.duration}초, 시뮬 손실 {a.loss*100:.0f}%\n\n")
        f.write(table)
    print("benchmark_result.md 로 저장됨")

if __name__ == "__main__":
    main()
