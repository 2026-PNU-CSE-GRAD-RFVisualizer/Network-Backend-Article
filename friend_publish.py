"""친구 노트북에서 주호의 broker로 RSSI 데이터를 보내는 테스트 스크립트.

준비물:
  1) Python 설치
  2) pip install paho-mqtt
실행:
  python friend_publish.py
같은 WiFi(같은 공유기)에 있어야 하고, 주호 쪽 broker/백엔드가 켜져 있어야 함.
"""
import json
import time

import paho.mqtt.client as mqtt

SERVER_IP = "172.21.7.171"
PORT = 1883
NODE_ID = "node-09"

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="friend-01")

client.will_set(f"status/{NODE_ID}/lwt",
                json.dumps({"node_id": NODE_ID, "online": False}),
                qos=1, retain=True)

print(f"connecting to {SERVER_IP}:{PORT} ...")
client.connect(SERVER_IP, PORT, keepalive=30)
client.loop_start()
client.publish(f"status/{NODE_ID}/lwt",
               json.dumps({"node_id": NODE_ID, "online": True}),
               qos=1, retain=True)

for seq in range(1, 21):
    payload = {
        "node_id": NODE_ID,
        "timestamp": int(time.time() * 1000),
        "ap_bssid": "AA:BB:CC:DD:EE:FF",
        "rssi": -58 - (seq % 5),
        "seq": seq,
        "status": 0,
    }
    client.publish(f"rssi/{NODE_ID}", json.dumps(payload), qos=0)
    print("sent", seq, payload["rssi"], "dBm")
    time.sleep(1)

client.publish(f"status/{NODE_ID}/lwt",
               json.dumps({"node_id": NODE_ID, "online": False}),
               qos=1, retain=True)
client.loop_stop()
client.disconnect()
print("done")
