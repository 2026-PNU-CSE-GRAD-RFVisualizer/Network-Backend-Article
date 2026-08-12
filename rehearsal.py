"""최종 실험 리허설 (in-process, 브로커 불필요).

새 절차(§18)를 그대로 재현한다:
  1. 가상 센서 5대 공동 배치 → Offset 측정·계산
  2. C1~C4 / T 역할 배정, 좌표·TX 등록
  3. forward Run 시작 → T1~T10 안정화+기록 → Run 종료
  4. 선택적 reverse Run (T10~T1)
  5. Export → QC

관리자·저장소를 직접 구동하므로(브로커/HTTP 없이) 로직·시간매칭·Export 를 검증한다.
가상 노드에 알려진 편차를 심어 두고 백엔드가 복원하는지 확인한다.

    python rehearsal.py                 # 압축(기본): 안정화 0s, 기록 2s
    python rehearsal.py --reverse       # 역방향 Run 도 실행
    python rehearsal.py --stab 20 --rec 120   # 실제 타이밍(느림)
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.experiment import ExperimentManager, compute_device_offsets  # noqa: E402
from backend.export import export_experiment  # noqa: E402
from backend.store import ExperimentStore  # noqa: E402

ROOM_W, ROOM_D = 15.4, 10.8
AP_POS = (7.7, 1.5, 1.20)
CAL_NODES = {"node1": "C1", "node2": "C2", "node3": "C3", "node4": "C4"}
MOVING = "node5"
ALL_NODES = list(CAL_NODES) + [MOVING]
TRUE_BIAS = {"node1": +1.8, "node2": -2.4, "node3": +0.6, "node4": -1.1, "node5": +3.2}

CAL_POS = {"C1": (2.4, 2.0, 0.8), "C2": (12.8, 2.6, 0.85),
           "C3": (3.1, 8.4, 1.3), "C4": (13.0, 9.2, 1.4)}
TEST_POS = {f"T{i}": (round(1.5 + (i - 1) * (ROOM_W - 3) / 9, 2),
                      round(2.0 + ((i - 1) % 3) * 2.5, 2), 0.8) for i in range(1, 11)}
OFFSET_POS = (7.0, 5.0, 0.8)


def path_loss_rssi(pos) -> float:
    d = max(math.dist(pos, AP_POS), 0.5)
    return 20.0 - (20 * math.log10(2400) - 27.55 + 26.0 * math.log10(d))


def ingest(store, mgr, node, ts, pos):
    rssi = round(path_loss_rssi(pos) + TRUE_BIAS[node] + random.gauss(0, 0.6), 1)
    ctx = mgr.offset_context(node) or mgr.context_at(node, ts)
    if ctx is None:
        return
    store.insert_measurements([{
        "experiment_id": mgr.experiment_id, "session_id": ctx["run_id"],
        "run_id": ctx["run_id"], "segment_id": ctx["segment_id"],
        "point_id": ctx["point_id"], "point_role": ctx["point_role"],
        "node_id": node, "node_ts_ms": ts, "server_ts_ms": ts,
        "rssi_raw_dbm": rssi, "rssi_filtered_dbm": rssi, "sample_count": 5,
        "error_flags": 0, "valid": True,
    }])


def main() -> int:
    p = argparse.ArgumentParser(description="최종 실험 리허설 (in-process)")
    p.add_argument("--stab", type=int, default=0, help="안정화 초(기본 0, 압축)")
    p.add_argument("--rec", type=int, default=2, help="기록 초(기본 2, 압축)")
    p.add_argument("--reverse", action="store_true", help="역방향 Run 도 실행")
    p.add_argument("--rate", type=float, default=5.0, help="가상 발행 Hz")
    p.add_argument("--out", default=None, help="Export 루트(기본 임시폴더)")
    p.add_argument("--seed", type=int, default=7)
    args = p.parse_args()
    random.seed(args.seed)

    tmp = Path(args.out) if args.out else Path("data") / "rehearsal"
    store = ExperimentStore(tmp / "db")
    mgr = ExperimentManager(store)
    exp = f"rehearsal_{time.strftime('%H%M%S')}"
    mgr.start_experiment(exp, "aa:bb:cc:dd:ee:01", 6)
    print(f"[리허설] experiment={exp}  stab={args.stab}s rec={args.rec}s reverse={args.reverse}")

    # 1. Offset: 5대 공동 배치
    for n in ALL_NODES:
        mgr.assign(n, "offset-00", "offset")
    off = mgr.start_offset_run()
    t = int(time.time() * 1000)
    for i in range(30):
        for n in ALL_NODES:
            ingest(store, mgr, n, t + i * 200, OFFSET_POS)
    mgr.stop_offset_run()
    res = compute_device_offsets(store, exp, offset_run_id=off["offset_run_id"])
    ref = sorted(TRUE_BIAS.values())[len(TRUE_BIAS) // 2]
    ok_off = all(abs(o["device_offset_db"] - (ref - TRUE_BIAS[o["node_id"]])) < 0.6
                 for o in res["nodes"])
    print(f"[1] Offset 계산: {'복원 OK' if ok_off else '복원 실패'} ({len(res['nodes'])}대)")

    # 2. 역할 배정 + 좌표 + TX
    for n, pt in CAL_NODES.items():
        mgr.assign(n, pt, "calibration")
        store.upsert_point(exp, pt, "calibration", *CAL_POS[pt], None, t)
    mgr.assign(MOVING, "T-move", "test")
    for tp, pos in TEST_POS.items():
        store.upsert_point(exp, tp, "test", *pos, None, t)
    store.upsert_tx(exp, "tx-01", *AP_POS, 2_400_000_000, "aa:bb:cc:dd:ee:01", 6, None)
    print("[2] 역할 배정 + 좌표 10개 + TX 등록")

    def run_pass(direction: str, pass_index: int) -> None:
        mgr.start_run(direction, pass_index)
        seq = list(TEST_POS) if direction == "forward" else list(reversed(TEST_POS))
        for order, tp in enumerate(seq, start=1):
            seg = mgr.prepare_test_segment(tp, order, args.stab, args.rec)
            # 안정화 구간: C1~C4 는 이동 데이터로 저장(segment 없음), T 는 제외
            if args.stab > 0:
                ingest(store, mgr, "node1", seg["recording_started_at_ms"] - 500, CAL_POS["C1"])
            # 기록 구간: 실제 rate 로 발행 (compressed 면 몇 개만)
            rs, re = seg["recording_started_at_ms"], seg["recording_ended_at_ms"]
            n_samples = max(3, int(args.rec * args.rate))
            for k in range(n_samples):
                ts = rs + int(k * (re - rs) / n_samples)
                if ts >= re:
                    break
                for n in CAL_NODES:
                    ingest(store, mgr, n, ts, CAL_POS[CAL_NODES[n]])
                ingest(store, mgr, MOVING, ts, TEST_POS[tp])
            mgr.finish_test_segment()
            time.sleep(args.rec + 0.2)   # 다음 위치 창이 겹치지 않도록(실제론 물리 이동)
            print(f"    {direction} {tp} (order {order}) 기록 완료")
        mgr.end_run()

    # 3. forward Run
    print("[3] forward Run")
    run_pass("forward", 1)
    # 4. reverse Run (선택)
    if args.reverse:
        print("[4] reverse Run")
        run_pass("reverse", 2)

    # 5. 사후(post) Offset 측정 — 실험 전후 편차 변화(drift) 확인용(재보정 아님)
    post = mgr.start_offset_run("post")
    t2 = int(time.time() * 1000)
    for i in range(30):
        for n in ALL_NODES:
            ingest(store, mgr, n, t2 + i * 200, OFFSET_POS)
    mgr.stop_offset_run()
    compute_device_offsets(store, exp, post["offset_run_id"])
    mgr.attach_post_offset(post["offset_run_id"])
    print(f"[5] 사후 Offset 측정·계산 (drift 확인용)")

    # 6. Export + QC
    out_root = tmp / "experiments"
    result = export_experiment(store, exp, out_root,
                               expected_samples=max(3, int(args.rec * args.rate)),
                               expected_test_points=10)
    qc = result["qc"]
    print(f"[5] Export → {result['path']}")
    print(f"    QC: {'통과' if qc['ok'] else '문제 있음'} | counts={qc['counts']}")
    for pr in qc["problems"]:
        print("    문제:", pr)
    for w in qc["warnings"][:5]:
        print("    경고:", w)

    store.close()
    return 0 if (ok_off and qc["ok"]) else 1


if __name__ == "__main__":
    sys.exit(main())
