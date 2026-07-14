import json
import time

import paho.mqtt.client as mqtt

payload = {
    "node_id": "node-01",
    "timestamp": int(time.time() * 1000),
    "ap_bssid": "AA:BB:CC:DD:EE:FF",
    "rssi": -54,
    "rssi_raw": -57,
    "seq": int(time.time()) % 100000,
    "status": 0,
}

client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="jhkang-test-publisher")
client.connect("127.0.0.1", 1883, keepalive=30)
client.loop_start()
result = client.publish("rssi/node-01", json.dumps(payload), qos=1)
result.wait_for_publish(timeout=5)
client.loop_stop()
client.disconnect()
print(json.dumps(payload, indent=2))
