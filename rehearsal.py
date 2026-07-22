"""7/23 현장 측정 리허설.

ESP32 5대를 가상으로 띄워 실제 강의실 크기(15.4 × 10.8 m)의 경로손실 모델로
RSSI 를 발행하고, 7/23 절차를 그대로 실행한 뒤 CSV 산출물을 검증한다.

목적은 두 가지다.
  1. 현장에서 처음 겪을 문제를 오늘 겪는다.
  2. 정답을 아는 데이터로 offset 계산과 대표값 계산이 맞는지 확인한다.
     (가상 노드에 알려진 편차를 심어 놓고, 백엔드가 그 값을 복원하는지 본다.)

사용법:
    python rehearsal.py                  # 30초 × 20위치 = 실제와 동일 (약 10분)
    python rehearsal.py --seconds 2      # 압축 실행 (약 1분), 로직 검증용
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import threading
import time
from urllib import error, request

import paho.mqtt.client as mqtt

# --- 실제 강의실 규격 (계획서 §5.2) ---
ROOM_W, ROOM_D = 15.4, 10.8
AP_POS = (7.7, 1.5, 1.20)
FREQ_MHZ = 2400
TX_POWER_DBM = 20.0
PATH_LOSS_EXP = 2.6      # 실내 강의실 가정
SHADOW_STD_DB = 1.2      # 위치별 고정 편차
NOISE_STD_DB = 1.5       # 샘플별 변동

# 가상 장치에 심어 둔 '진짜' 편차. 백엔드가 이 값을 복원해야 한다.
TRUE_DEVICE_BIAS = {
    "node1": +1.8, "node2": -2.4, "node3": +0.6,
    "node4": -1.1, "node5": +3.2,
}

CAL_POINTS = {
    "cal-01": (2.4, 2.0, 0.78),
    "cal-02": (12.8, 2.6, 0.85),
    "cal-03": (3.1, 8.4, 1.32),
    "cal-04": (13.0, 9.2, 1.40),
}
FIXED_NODES = {"node1": "cal-01", "node2": "cal-02", "node3": "cal-03", "node5": "cal-04"}
MOVING_NODE = "node4"
OFFSET_SPOT = (7.0, 5.0, 0.80)

# Test 15개: 강의실 전체를 균일하게 덮는 3 × 5 격자 (계획서 §3.2)
TEST_POINTS = {
    f"test-{i * 5 + j + 1:02d}": (
        round(2.0 + j * (ROOM_W - 4.0) / 4, 2),
        round(1.6 + i * (ROOM_D - 3.2) / 2, 2),
        round(0.78 + (1.6 + i * (ROOM_D - 3.2) / 2) * 0.069, 2),  # 계단 경사 0.75m/10.8m
    )
    for i in range(3)
    for j in range(5)
}


def free_space_rssi(pos: tuple[float, float, float], shadow: float) -> float:
    dx, dy, dz = pos[0] - AP_POS[0], pos[1] - AP_POS[1], pos[2] - AP_POS[2]
    d = max(math.sqrt(dx * dx + dy * dy + dz * dz), 0.5)
    fspl_1m = 20 * math.log10(1.0) + 20 * math.log10(FREQ_MHZ) - 27.55
    return TX_POWER_DBM - (fspl_1m + 10 * PATH_LOSS_EXP * math.log10(d)) + shadow


class VirtualNode(threading.Thread):
    """ESP32 1대. 200ms 마다 샘플링하고 최근 5개의 이동평균을 1초마다 발행한다."""

    def __init__(self, node_id: str, host: str, port: int, bssid: str, channel: int) -> None:
        super().__init__(daemon=True)
        self.node_id = node_id
        self.bias = TRUE_DEVICE_BIAS[node_id]
        self.pos = OFFSET_SPOT
        self.shadow = random.gauss(0, SHADOW_STD_DB)
        self.bssid, self.channel = bssid, channel
        self.seq = 0
        self.running = True
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{node_id}")
        self.client.connect(host, port, keepalive=30)
        self.client.loop_start()

    def move_to(self, pos: tuple[float, float, float]) -> None:
        self.pos = pos
        self.shadow = random.gauss(0, SHADOW_STD_DB)  # 위치가 바뀌면 음영도 바뀐다

    def run(self) -> None:
        window: list[float] = []
        last_publish = 0.0
        while self.running:
            true_rssi = free_space_rssi(self.pos, self.shadow) + self.bias
            window.append(true_rssi + random.gauss(0, NOISE_STD_DB))
            window = window[-5:]
            now = time.time()
            if now - last_publish >= 1.0:
                last_publish = now
                self.seq += 1
                payload = {
                    "node_id": self.node_id,
                    "timestamp": int(now * 1000),
                    "ap_bssid": self.bssid,
                    "ap_channel": self.channel,
                    "rssi": round(sum(window) / len(window), 1),  # Filtered
                    "rssi_raw": round(window[-1], 1),             # Raw
                    "sample_count": len(window),
                    "seq": self.seq,
                    "status": 0,
                }
                self.client.publish(f"rssi/{self.node_id}", json.dumps(payload), qos=0)
            time.sleep(0.2)

    def stop(self) -> None:
        self.running = False
        self.client.loop_stop()
        self.client.disconnect()


class Api:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")

    def call(self, path: str, body: dict | None = None) -> dict:
        url = f"{self.base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = request.Request(url, data=data, method="POST" if body is not None else "GET",
                              headers={"Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except error.HTTPError as exc:
            raise RuntimeError(f"{path} -> {exc.code} {exc.read().decode()}") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mqtt-host", default="127.0.0.1")
    parser.add_argument("--mqtt-port", type=int, default=1883)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--seconds", type=int, default=30, help="위치당 측정 시간")
    parser.add_argument("--experiment-id", default=None)
    parser.add_argument("--bssid", default="aa:bb:cc:dd:ee:01")
    parser.add_argument("--channel", type=int, default=6)
    args = parser.parse_args()

    experiment_id = args.experiment_id or f"rehearsal_{time.strftime('%Y%m%d_%H%M%S')}"
    api = Api(args.api)

    print(f"== 리허설 시작: {experiment_id} (위치당 {args.seconds}초) ==")
    api.call("/experiment/start", {
        "experiment_id": experiment_id, "ap_bssid": args.bssid,
        "ap_channel": args.channel, "note": "가상 노드 리허설",
    })
    api.call("/experiment/tx", {
        "tx_id": "tx-01", "pos_x": AP_POS[0], "pos_y": AP_POS[1], "pos_z": AP_POS[2],
        "frequency_hz": FREQ_MHZ * 1_000_000,
    })

    nodes = {
        node_id: VirtualNode(node_id, args.mqtt_host, args.mqtt_port, args.bssid, args.channel)
        for node_id in TRUE_DEVICE_BIAS
    }
    for node in nodes.values():
        node.start()
    time.sleep(2)

    def measure(point_id: str, role: str, moving: str | None) -> None:
        api.call("/session/start", {
            "point_id": point_id, "point_role": role,
            "seconds": args.seconds, "moving_node_id": moving,
        })
        time.sleep(args.seconds + 1.0)
        state = api.call("/session/current")
        done = {d["point_id"]: d["valid_samples"] for d in state["done_points"]}
        print(f"  {point_id:<10} 유효샘플 {done.get(point_id, 0):>3}개")

    try:
        # 1단계: 5대를 한 자리에 모아 놓고 장치 편차 측정 (계획서 §4.1)
        print("\n[1] 장치 편차 측정 (5대 동일 위치)")
        for node in nodes.values():
            node.move_to(OFFSET_SPOT)
        for node_id in nodes:
            api.call("/experiment/assign",
                     {"node_id": node_id, "point_id": "offset-00", "point_role": "offset"})
        measure("offset-00", "offset", None)

        # 2단계: 고정 보정 센서 4대를 각자의 위치에 배치
        print("\n[2] 고정 보정 센서 배치")
        for node_id, point_id in FIXED_NODES.items():
            nodes[node_id].move_to(CAL_POINTS[point_id])
            api.call("/experiment/assign",
                     {"node_id": node_id, "point_id": point_id, "point_role": "calibration"})
            print(f"  {node_id} -> {point_id} {CAL_POINTS[point_id]}")
        measure("cal-01", "calibration", None)  # 안정화 겸 보정점 데이터 확보

        # 3단계: 이동 센서로 Test 15개 순차 측정
        print("\n[3] Test 위치 15개 측정")
        for point_id, pos in TEST_POINTS.items():
            nodes[MOVING_NODE].move_to(pos)
            time.sleep(0.4)
            measure(point_id, "test", MOVING_NODE)

        # 4단계: 좌표 등록 (현장에서 종이에 적어온 값)
        print("\n[4] 좌표 등록")
        lines = ["point_id,point_role,pos_x,pos_y,pos_z"]
        lines.append(f"offset-00,offset,{OFFSET_SPOT[0]},{OFFSET_SPOT[1]},{OFFSET_SPOT[2]}")
        for pid, p in CAL_POINTS.items():
            lines.append(f"{pid},calibration,{p[0]},{p[1]},{p[2]}")
        for pid, p in TEST_POINTS.items():
            lines.append(f"{pid},test,{p[0]},{p[1]},{p[2]}")
        result = api.call("/experiment/points/import", {"csv": "\n".join(lines)})
        print(f"  {result['count']}개 등록, 오류 {len(result['errors'])}건")

        # 5단계: offset 계산 및 검증
        print("\n[5] 장치 offset 계산")
        offsets = api.call("/experiment/offsets/compute", {})
        if not offsets["ok"]:
            print(f"  실패: {offsets['reason']}")
            return 1

        true_ref = sorted(TRUE_DEVICE_BIAS.values())[len(TRUE_DEVICE_BIAS) // 2]
        print(f"  {'node':<8}{'계산된 offset':>14}{'참값':>10}{'오차':>10}")
        max_err = 0.0
        for item in offsets["nodes"]:
            expected = true_ref - TRUE_DEVICE_BIAS[item["node_id"]]
            err = abs(item["device_offset_db"] - expected)
            max_err = max(max_err, err)
            print(f"  {item['node_id']:<8}{item['device_offset_db']:>14.2f}"
                  f"{expected:>10.2f}{err:>10.2f}")
        print(f"  최대 오차 {max_err:.2f} dB")

        # 6단계: Export
        print("\n[6] CSV 내보내기")
        export = api.call("/experiment/export", {})
        qc = export["qc"]
        print(f"  저장 위치: {export['path']}")
        print(f"  품질 점검: {'통과' if qc['ok'] else '문제 있음'}")
        for p in qc["problems"]:
            print(f"    [문제] {p}")
        for w in qc["warnings"]:
            print(f"    [경고] {w}")
        print(f"  집계: {json.dumps(qc['counts'], ensure_ascii=False)}")

        ok = qc["ok"] and max_err < 1.5
        print(f"\n== 리허설 {'성공' if ok else '실패 — 위 항목 확인 필요'} ==")
        return 0 if ok else 1

    finally:
        for node in nodes.values():
            node.stop()
        api.call("/experiment/end", {})


if __name__ == "__main__":
    sys.exit(main())
