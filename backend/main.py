import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .config import PROJECT_ROOT, settings
from .database import Database
from .experiment import Conflict, ExperimentManager, compute_device_offsets
from .export import export_experiment, import_points_csv
from .metrics import Metrics
from .mqtt_bridge import MqttBridge
from .pages import MEASURE_HTML
from .realtime import WebSocketHub, WindowBuffer
from .state import NodeRegistry, now_ms
from .store import ExperimentStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

db = Database(settings.database_dsn)
registry = NodeRegistry(settings.node_timeout_seconds)
# 실시간 경로는 9월 졸업작품 범위. 논문 실험에서는 켜지 않는다.
window_buffer = WindowBuffer(settings.window_size_ms, settings.window_grace_ms) if settings.enable_realtime else None
# hub 는 노드 online/offline 알림에도 쓰이므로 항상 만든다. 구독자가 없으면 비용이 없다.
hub = WebSocketHub()
metrics = Metrics()
store = ExperimentStore(settings.experiment_data_path)
sessions = ExperimentManager(store)
mqtt_bridge: MqttBridge | None = None
tasks: list[asyncio.Task] = []


def require_experiment() -> str:
    if sessions.experiment_id is None:
        raise HTTPException(status_code=400, detail="실험이 시작되지 않았습니다.")
    return sessions.experiment_id


class ExperimentStart(BaseModel):
    experiment_id: str
    ap_bssid: str | None = None
    ap_channel: int | None = None
    note: str | None = None


class OffsetStart(BaseModel):
    phase: str = "pre"          # pre | post
    note: str | None = None


class OffsetCompute(BaseModel):
    offset_run_id: str | None = None
    phase: str = "pre"


class PostOffset(BaseModel):
    offset_run_id: str
    run_id: str | None = None


class RunStart(BaseModel):
    direction: str = "forward"
    pass_index: int = 1
    offset_run_id: str | None = None
    note: str | None = None


class SegmentPrepare(BaseModel):
    point_id: str
    order_index: int
    stabilization_seconds: int | None = None
    recording_seconds: int | None = None
    note: str | None = None


class Assignment(BaseModel):
    node_id: str
    point_id: str
    point_role: str


class PointsCsv(BaseModel):
    csv: str


class ExportRequest(BaseModel):
    # 압축 리허설처럼 측정 시간이 짧을 때 기대 샘플 수를 낮춰 잡기 위한 값.
    # 지정하지 않으면 설정값(위치당 30개)을 쓴다.
    expected_samples: int | None = None


class TxInput(BaseModel):
    tx_id: str = "tx-01"
    pos_x: float | None = None
    pos_y: float | None = None
    pos_z: float | None = None
    frequency_hz: int | None = 2_400_000_000
    note: str | None = None


class NodeMeta(BaseModel):
    node_id: str
    pos_x: float | None = None
    pos_y: float | None = None
    pos_z: float | None = None
    rot_w: float | None = None
    rot_x: float | None = None
    rot_y: float | None = None
    rot_z: float | None = None
    description: str | None = None

async def watch_heartbeats() -> None:
    while True:
        for status in registry.expire_offline():
            await db.upsert_node_status(status)
            await hub.broadcast({"type": "node_status", "node": status})
        await asyncio.sleep(1)

async def watch_segment_deadline() -> None:
    """기록 종료 시각이 지난 TestSegment 를 자동 완료한다. Run 에는 자동 종료가 없다."""
    while True:
        finished = sessions.auto_advance_segment()
        if finished is not None:
            await hub.broadcast({"type": "segment_completed", "segment": finished})
        await asyncio.sleep(0.2)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global mqtt_bridge
    await db.connect()
    loop = asyncio.get_running_loop()
    mqtt_bridge = MqttBridge(settings, loop, db, registry, window_buffer, hub, metrics,
                             store=store, sessions=sessions)
    mqtt_bridge.start()
    # 백엔드 재시작 처리(§15): 이전에 running 으로 남은 Run/Segment 를 interrupted 로 표시.
    # 열린 구간을 정상 완료로 숨기지 않는다.
    interrupted = store.mark_all_open_interrupted(now_ms())
    if interrupted:
        logger.warning("이전 실행에서 열린 채 남은 Run/Segment 를 interrupted 로 표시: %s", interrupted)
    tasks.extend(
        [
            asyncio.create_task(watch_heartbeats()),
            asyncio.create_task(watch_segment_deadline()),
            asyncio.create_task(db.flush_raw_loop(metrics)),
        ]
    )
    if settings.enable_realtime and window_buffer is not None:
        from .realtime.routes import flush_windows
        tasks.append(asyncio.create_task(flush_windows(
            window_buffer, registry, hub, metrics,
            db.insert_frame, settings.window_flush_interval_ms)))
        logger.info("realtime pipeline enabled (9월 졸업작품 범위)")
    else:
        logger.info("realtime pipeline disabled — 논문 실험 모드")
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if mqtt_bridge is not None:
            mqtt_bridge.stop()
        await db.close()
        store.close()

app = FastAPI(title="3DGS RSSI Backend", version="0.5.0-run", lifespan=lifespan)


@app.exception_handler(Conflict)
async def _conflict_handler(request: Request, exc: Conflict) -> JSONResponse:
    # 상태 위반(중복 시작·순서 위반 등)은 409. 조용히 덮어쓰지 않는다.
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def _value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "postgres_connected": db.connected,
        "mqtt_host": settings.mqtt_host,
        "mqtt_port": settings.mqtt_port,
        "mqtt": mqtt_bridge.status() if mqtt_bridge else {"connected": False},
        "enable_realtime": settings.enable_realtime,
        "window_size_ms": settings.window_size_ms,
        "window_grace_ms": settings.window_grace_ms,
        "experiment_id": sessions.experiment_id,
        "project_root": str(PROJECT_ROOT),
        "experiment_db": str(store.db_path),
        "jsonl_backup": str(store.jsonl_path),
        "export_root": str(settings.export_root_path),
        "rssi_filtered_scale": settings.rssi_filtered_scale,
        "test_stabilization_seconds": settings.test_stabilization_seconds,
        "test_recording_seconds": settings.test_recording_seconds,
        "expected_test_points": settings.expected_test_points,
    }

@app.get("/nodes/status")
async def nodes_status() -> dict[str, object]:
    return {"nodes": registry.snapshot()}

@app.get("/metrics")
async def get_metrics() -> dict[str, object]:
    return metrics.snapshot()

@app.get("/nodes/meta")
async def get_nodes_meta() -> dict[str, object]:
    return {"nodes": await db.get_node_meta()}

@app.post("/nodes/meta")
async def set_node_meta(meta: NodeMeta) -> dict[str, object]:
    await db.upsert_node_meta(meta.model_dump())
    return {"ok": True, "node_id": meta.node_id}

# ----------------------------------------------------------------------
# 논문 실험 API (7/23 강의실 측정)
# ----------------------------------------------------------------------

@app.post("/experiment/start")
async def experiment_start(body: ExperimentStart) -> dict[str, object]:
    # 실험 시작 버튼을 누를 때마다 입력한 이름 뒤에 실행 시각을 붙여
    # 매번 새로운 experiment_id 를 만든다. 그러면:
    #   - 이전 실험 폴더는 그대로 남고 (experiments/<이전id>/)
    #   - 이번 실험은 새 폴더(experiments/<새id>/)에 저장된다.
    # 예: classroom_20260723  ->  classroom_20260723_213045
    base = body.experiment_id.strip() or "experiment"
    run_id = f"{base}_{time.strftime('%H%M%S')}"
    return sessions.start_experiment(run_id, body.ap_bssid,
                                     body.ap_channel, body.note)

@app.post("/experiment/end")
async def experiment_end() -> dict[str, object]:
    return sessions.end_experiment()

@app.get("/experiment/list")
async def experiment_list() -> dict[str, object]:
    return {"experiments": store.list_experiments()}

@app.post("/experiment/assign")
async def experiment_assign(body: Assignment) -> dict[str, object]:
    try:
        return sessions.assign(body.node_id, body.point_id, body.point_role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

@app.get("/experiment/assignments")
async def experiment_assignments() -> dict[str, object]:
    return {"assignments": store.list_assignments(require_experiment())}

# -- Offset 측정 -----------------------------------------------------
@app.post("/offset-run/start")
async def offset_run_start(body: OffsetStart | None = None) -> dict[str, object]:
    return sessions.start_offset_run(body.phase if body else "pre",
                                     body.note if body else None)

@app.post("/offset-run/stop")
async def offset_run_stop() -> dict[str, object]:
    return sessions.stop_offset_run()

@app.get("/offset-run/current")
async def offset_run_current() -> dict[str, object]:
    active = sessions.active_offset_run()
    return {"offset_run": active.to_dict() if active else None}

# -- 본 실험 Run -----------------------------------------------------
@app.post("/run/start")
async def run_start(body: RunStart) -> dict[str, object]:
    return sessions.start_run(body.direction, body.pass_index, body.offset_run_id, body.note)

@app.post("/run/end")
async def run_end() -> dict[str, object]:
    return sessions.end_run()

@app.get("/run/current")
async def run_current() -> dict[str, object]:
    """UI 가 주기적으로 호출하는 현재 상태(§9.현재 상태 응답)."""
    cur = now_ms()
    run = sessions.active_run()
    seg = sessions.active_test_segment()
    experiment_id = sessions.experiment_id
    cal_nodes, test_node = [], {}
    for node, (point, role) in sorted(sessions.assignments().items()):
        entry = {"node_id": node, "point_id": point,
                 "online": next((n["online"] for n in registry.snapshot()
                                 if n["node_id"] == node), False)}
        if role == "calibration":
            cal_nodes.append(entry)
        elif role == "test":
            test_node = entry
    completed = []
    if run is not None:
        completed = [
            {"point_id": s["point_id"], "attempt_index": s["attempt_index"],
             "status": s["status"], "order_index": s["order_index"]}
            for s in store.list_test_segments(run.run_id)
            if s["status"] == "completed" and not s["superseded"]
        ]
    return {
        "experiment_id": experiment_id,
        "ap_bssid": sessions.ap_bssid,
        "ap_channel": sessions.ap_channel,
        "offset_run": (o.to_dict() if (o := sessions.active_offset_run()) else None),
        "run": run.to_dict() if run else None,
        "test_segment": seg.to_dict(cur) if seg else None,
        "calibration_nodes": cal_nodes,
        "test_node": test_node,
        "completed_points": completed,
        "mqtt": mqtt_bridge.status() if mqtt_bridge else {"connected": False},
    }

# -- TestSegment -----------------------------------------------------
@app.post("/test-segment/prepare")
async def test_segment_prepare(body: SegmentPrepare) -> dict[str, object]:
    return sessions.prepare_test_segment(
        body.point_id, body.order_index,
        body.stabilization_seconds if body.stabilization_seconds is not None
        else settings.test_stabilization_seconds,
        body.recording_seconds if body.recording_seconds is not None
        else settings.test_recording_seconds,
        body.note)

@app.post("/test-segment/stop")
async def test_segment_stop() -> dict[str, object]:
    """현재 Test 즉시 종료(2분 전이라도). 정상 완료로 마감한다."""
    return sessions.finish_test_segment()

@app.post("/test-segment/discard")
async def test_segment_discard() -> dict[str, object]:
    """현재 Test 버리고 재측정 준비. C1~C4 연속 원본은 보존된다."""
    return sessions.discard_test_segment()

@app.get("/test-segment/current")
async def test_segment_current() -> dict[str, object]:
    seg = sessions.active_test_segment()
    return {"test_segment": seg.to_dict() if seg else None}

@app.get("/experiment/runs")
async def experiment_runs() -> dict[str, object]:
    return {"runs": store.list_runs(require_experiment())}

# -- Legacy 차단 -----------------------------------------------------
# 구 세션 API 는 새 Run/TestSegment 로 대체됐다. 조용히 성공시키지 않고 명시적으로 410.
_LEGACY_MSG = ("이 API 는 제거되었습니다. /run/*, /test-segment/*, /offset-run/* 를 사용하세요. "
               "(단일 세션 경로로는 새 실험 데이터를 만들 수 없습니다.)")

@app.post("/session/start")
@app.post("/session/stop")
@app.get("/session/current")
async def legacy_session_gone() -> dict[str, object]:
    raise HTTPException(status_code=410, detail=_LEGACY_MSG)

@app.get("/experiment/points")
async def experiment_points() -> dict[str, object]:
    return {"points": store.list_points(require_experiment())}

@app.post("/experiment/points/import")
async def experiment_points_import(body: PointsCsv) -> dict[str, object]:
    return import_points_csv(store, require_experiment(), body.csv, now_ms())

@app.post("/experiment/tx")
async def experiment_tx(body: TxInput) -> dict[str, object]:
    experiment_id = require_experiment()
    store.upsert_tx(experiment_id, body.tx_id, body.pos_x, body.pos_y, body.pos_z,
                    body.frequency_hz, sessions.ap_bssid, sessions.ap_channel, body.note)
    return {"ok": True, "tx": store.list_tx(experiment_id)}

@app.post("/experiment/offsets/compute")
async def experiment_offsets(body: OffsetCompute | None = None) -> dict[str, object]:
    """지정 OffsetRun(또는 해당 phase 의 최신 완료 OffsetRun)으로 장치 편차 계산."""
    exp = require_experiment()
    offset_run_id = body.offset_run_id if body else None
    if offset_run_id is None:
        latest = store.latest_offset_run(exp, body.phase if body else "pre")
        if latest is None:
            raise HTTPException(status_code=400, detail="완료된 OffsetRun 이 없습니다.")
        offset_run_id = latest["offset_run_id"]
    return compute_device_offsets(store, exp, offset_run_id)

@app.post("/run/attach-post-offset")
async def run_attach_post_offset(body: PostOffset) -> dict[str, object]:
    """사후 OffsetRun 을 본 실험 Run 에 연결(drift 확인용, 재보정 아님)."""
    return sessions.attach_post_offset(body.offset_run_id, body.run_id)

@app.post("/experiment/export")
async def experiment_export(body: ExportRequest | None = None) -> dict[str, object]:
    experiment_id = require_experiment()
    # offset 은 사전/사후 OffsetRun 별로 이미 계산되어 있다(여기서 재계산하지 않음).
    expected = (body.expected_samples if body and body.expected_samples
                else settings.test_recording_seconds)
    return export_experiment(store, experiment_id, settings.export_root_path, expected,
                             settings.expected_test_points, settings.expected_calibration_nodes)

@app.get("/experiment/download/{which}")
async def experiment_download(which: str) -> FileResponse:
    experiment_id = require_experiment()
    paths = {
        "raw": settings.export_root_path / experiment_id / "raw" / "measurements_raw.csv",
        "summary": settings.export_root_path / experiment_id / "processed" / "measurements_summary.csv",
        "calibration": settings.export_root_path / experiment_id / "processed" / "calibration_points.csv",
        "test": settings.export_root_path / experiment_id / "processed" / "test_points.csv",
    }
    path = paths.get(which)
    if path is None:
        raise HTTPException(status_code=404, detail=f"알 수 없는 파일: {which}")
    if not path.exists():
        raise HTTPException(status_code=404, detail="먼저 CSV 내보내기를 실행하세요.")
    return FileResponse(path, filename=path.name, media_type="text/csv")

@app.get("/", response_class=HTMLResponse)
@app.get("/measure", response_class=HTMLResponse)
async def measure_page() -> str:
    """현장 측정 페이지. 논문 실험의 기본 화면이므로 루트에 둔다."""
    return MEASURE_HTML


# 실시간 라우트(WS /frames, /position/latest)는 ENABLE_REALTIME=true 일 때만 붙는다.
if settings.enable_realtime:
    from .realtime.routes import build_router
    app.include_router(build_router(hub, registry))

@app.get("/monitor", response_class=HTMLResponse)
async def dashboard() -> str:
    """노드 상태·성능 모니터. 측정 중 5개 노드가 살아 있는지 확인하는 용도."""
    return DASHBOARD_HTML

DASHBOARD_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<title>RSSI Network Monitor</title>
<style>
 body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:24px;background:#0f1115;color:#e6e6e6}
 h1{font-size:18px} h2{font-size:14px;color:#9aa0a6;margin-top:24px}
 table{border-collapse:collapse;width:100%;margin-top:8px}
 th,td{border:1px solid #2a2f3a;padding:6px 10px;text-align:left;font-size:13px}
 th{background:#1a1e27} .on{color:#38d39f} .off{color:#ff6b6b}
 .cards{display:flex;gap:12px;flex-wrap:wrap}
 .card{background:#1a1e27;border:1px solid #2a2f3a;border-radius:8px;padding:12px 16px;min-width:140px}
 .card .v{font-size:20px;font-weight:600} .card .l{font-size:12px;color:#9aa0a6}
</style></head><body>
<h1>3DGS RSSI Network Monitor
 &nbsp;<a href="/" style="font-size:13px;color:#6ea8fe">→ 강의실 측정 페이지</a></h1>
<div id="conn"></div>
<h2>성능 지표 (metrics)</h2>
<div class="cards" id="cards"></div>
<h2>노드 상태 (nodes)</h2>
<table id="nodes"><thead><tr>
 <th>node_id</th><th>online</th><th>msg_rate_hz</th><th>lost_packets</th><th>last_seen</th>
</tr></thead><tbody></tbody></table>
<script>
async function tick(){
 try{
  const [m,n]=await Promise.all([fetch('/metrics').then(r=>r.json()),fetch('/nodes/status').then(r=>r.json())]);
  const cl=m.collect_latency_ms||{}, fl=m.window_flush_ms||{};
  document.getElementById('cards').innerHTML=[
   ['수신 메시지',m.messages_received],['드롭',m.messages_dropped],
   ['드롭률',(m.drop_rate*100).toFixed(2)+'%'],['발행 프레임',m.frames_emitted],
   ['저장 행',m.raw_rows_written],
   ['수집지연 avg/p95/max',(cl.avg||0)+'/'+(cl.p95||0)+'/'+(cl.max||0)+' ms'],
   ['프레임생성 avg/max',(fl.avg||0)+'/'+(fl.max||0)+' ms'],
  ].map(([l,v])=>`<div class="card"><div class="v">${v}</div><div class="l">${l}</div></div>`).join('');
  const tb=document.querySelector('#nodes tbody');
  tb.innerHTML=(n.nodes||[]).map(x=>`<tr><td>${x.node_id}</td>
   <td class="${x.online?'on':'off'}">${x.online?'ONLINE':'OFFLINE'}</td>
   <td>${x.msg_rate_hz}</td><td>${x.lost_packets}</td>
   <td>${x.last_seen?new Date(x.last_seen).toLocaleTimeString():'-'}</td></tr>`).join('');
  document.getElementById('conn').textContent='';
 }catch(e){document.getElementById('conn').textContent='백엔드 연결 실패: '+e;}
}
setInterval(tick,1000); tick();
</script></body></html>"""