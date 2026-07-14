import asyncio
import json
import time

import httpx
import paho.mqtt.client as mqtt
import websockets

def publish_sample(seq: int) -> None:
    payload = {
        "node_id": "node-01",
        "timestamp": int(time.time() * 1000),
        "ap_bssid": "AA:BB:CC:DD:EE:FF",
        "rssi": -54,
        "rssi_raw": -57,
        "seq": seq,
        "status": 0,
    }
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"smoke-publisher-{seq}")
    client.connect("127.0.0.1", 1883, keepalive=30)
    client.loop_start()
    result = client.publish("rssi/node-01", json.dumps(payload), qos=1)
    result.wait_for_publish(timeout=5)
    client.loop_stop()
    client.disconnect()

async def main() -> None:
    seq = int(time.time()) % 100000
    async with websockets.connect("ws://127.0.0.1:8000/frames") as ws:
        publish_sample(seq)
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))

    async with httpx.AsyncClient() as client:
        health = (await client.get("http://127.0.0.1:8000/health")).json()
        nodes = (await client.get("http://127.0.0.1:8000/nodes/status")).json()

    print(json.dumps({"health": health, "nodes": nodes, "frame": frame}, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
