"""브로커(127.0.0.1:1883)에 도착하는 모든 메시지를 그대로 출력.
백엔드와 무관하게 '브로커에 실제로 뭐가 들어오는지'를 직접 확인하는 도구.
서버 노트북에서 venv 켜고 실행: python sniff.py
"""
import paho.mqtt.client as mqtt

def on_connect(client, userdata, flags, reason_code, properties):
    print(f"[sniffer] connected rc={reason_code} -> subscribe '#'")
    client.subscribe("#")

def on_message(client, userdata, msg):
    print(f"[recv] topic={msg.topic}  payload={msg.payload[:150]!r}")

c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="sniffer")
c.on_connect = on_connect
c.on_message = on_message
c.connect("127.0.0.1", 1883, 30)
print("listening on 127.0.0.1:1883 ... (Ctrl+C to stop)")
c.loop_forever()
