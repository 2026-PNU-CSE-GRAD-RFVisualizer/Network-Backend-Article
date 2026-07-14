"""RSSI 파이프라인 부하/동기화 시뮬레이터.

기본(개별 모드): 각 노드가 rssi/<node_id> 로 따로 발행.
    python load_test.py --nodes 4 --rate 5 --duration 30 --loss 0.05

게이트웨이 모드: 게이트웨이 1대가 (센서 N개 + 게이트웨이 자신)을
한 메시지(readings 배열)로 묶어 gateway/<gw_id> 로 발행.
    python load_test.py --gateway --nodes 4 --rate 5 --duration 30 --loss 0.05

브로커/백엔드를 먼저 띄운 뒤 실행하고 http://127.0.0.1:8000/ 대시보드에서 확인.
"""
import argparse
import json
import random
import threading
import time

import paho.mqtt.client as mqtt

def _reading(node_id: str, seq: int) -> dict:
    return {
        "node_id": node_id,
        "timestamp": int(time.time() * 1000),
        "ap_bssid": "AA:BB:CC:DD:EE:FF",
        "rssi": random.randint(-85, -40),
        "rssi_raw": random.randint(-90, -40),
        "seq": seq,
        "status": 0,
    }

def run_node(node_id, host, port, rate_hz, duration_s, loss_prob):
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{node_id}")
    lwt = f"status/{node_id}/lwt"
    client.will_set(lwt, json.dumps({"node_id": node_id, "online": False}), qos=1, retain=True)
    client.connect(host, port, keepalive=30)
    client.loop_start()
    client.publish(lwt, json.dumps({"node_id": node_id, "online": True}), qos=1, retain=True)

    seq, sent = 0, 0
    interval = 1.0 / rate_hz
    end = time.time() + duration_s
    while time.time() < end:
        seq += 1
        if random.random() >= loss_prob:
            client.publish(f"rssi/{node_id}", json.dumps(_reading(node_id, seq)), qos=0)
            sent += 1
        time.sleep(interval)

    client.publish(lwt, json.dumps({"node_id": node_id, "online": False}), qos=1, retain=True)
    client.loop_stop()
    client.disconnect()
    print(f"[{node_id}] sent={sent} last_seq={seq}")

def run_gateway(gw_id, sensor_ids, host, port, rate_hz, duration_s, loss_prob):
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{gw_id}")
    lwt = f"status/{gw_id}/lwt"
    client.will_set(lwt, json.dumps({"node_id": gw_id, "online": False}), qos=1, retain=True)
    client.connect(host, port, keepalive=30)
    client.loop_start()
    client.publish(lwt, json.dumps({"node_id": gw_id, "online": True}), qos=1, retain=True)

    all_ids = sensor_ids + [gw_id]
    seq, sent = 0, 0
    interval = 1.0 / rate_hz
    end = time.time() + duration_s
    while time.time() < end:
        seq += 1
        readings = [_reading(nid, seq) for nid in all_ids if random.random() >= loss_prob]
        if readings:
            batch = {"gateway_id": gw_id, "timestamp": int(time.time() * 1000), "readings": readings}
            client.publish(f"gateway/{gw_id}", json.dumps(batch), qos=0)
            sent += 1
        time.sleep(interval)

    client.publish(lwt, json.dumps({"node_id": gw_id, "online": False}), qos=1, retain=True)
    client.loop_stop()
    client.disconnect()
    print(f"[{gw_id}] batches={sent} last_seq={seq} nodes={all_ids}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--nodes", type=int, default=4, help="센서 노드 수")
    ap.add_argument("--rate", type=float, default=5.0, help="초당 발행 횟수")
    ap.add_argument("--duration", type=float, default=30.0, help="실행 시간(초)")
    ap.add_argument("--loss", type=float, default=0.05, help="패킷 손실 확률 0~1")
    ap.add_argument("--gateway", action="store_true", help="게이트웨이 배치 모드")
    ap.add_argument("--gw-id", default="gw-01", help="게이트웨이 노드 id")
    a = ap.parse_args()

    sensor_ids = [f"node-{i:02d}" for i in range(1, a.nodes + 1)]

    if a.gateway:
        print(f"[게이트웨이 모드] gw={a.gw_id} + sensors={sensor_ids} rate={a.rate}Hz dur={a.duration}s loss={a.loss}")
        run_gateway(a.gw_id, sensor_ids, a.host, a.port, a.rate, a.duration, a.loss)
    else:
        print(f"[개별 모드] nodes={sensor_ids} rate={a.rate}Hz dur={a.duration}s loss={a.loss}")
        threads = [
            threading.Thread(target=run_node,
                             args=(nid, a.host, a.port, a.rate, a.duration, a.loss),
                             daemon=True)
            for nid in sensor_ids
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    print("완료")

if __name__ == "__main__":
    main()
