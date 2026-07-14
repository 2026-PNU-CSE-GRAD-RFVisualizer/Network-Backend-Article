import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .config import settings
from .database import Database
from .metrics import Metrics
from .mqtt_bridge import MqttBridge
from .state import NodeRegistry, WindowBuffer, now_ms
from .ws import WebSocketHub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

db = Database(settings.database_dsn)
registry = NodeRegistry(settings.node_timeout_seconds)
window_buffer = WindowBuffer(settings.window_size_ms)
hub = WebSocketHub()
metrics = Metrics()
mqtt_bridge: MqttBridge | None = None
tasks: list[asyncio.Task] = []

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

async def flush_windows() -> None:
    while True:
        start = time.perf_counter()
        frames = window_buffer.pop_ready(now_ms(), registry.known_nodes())
        if frames:
            emit_ms = now_ms()
            for frame in frames:
                for node in frame["nodes"].values():
                    srv = node.get("server_receive_ms")
                    if srv is not None:
                        metrics.observe_collect_latency(emit_ms - srv)
                await db.insert_frame(frame)
                await hub.broadcast(frame)
            metrics.inc_frames(len(frames))
            metrics.observe_flush_ms((time.perf_counter() - start) * 1000)
        await asyncio.sleep(settings.window_flush_interval_ms / 1000)

async def watch_heartbeats() -> None:
    while True:
        for status in registry.expire_offline():
            await db.upsert_node_status(status)
            await hub.broadcast({"type": "node_status", "node": status})
        await asyncio.sleep(1)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global mqtt_bridge
    await db.connect()
    loop = asyncio.get_running_loop()
    mqtt_bridge = MqttBridge(settings, loop, db, registry, window_buffer, hub, metrics)
    mqtt_bridge.start()
    tasks.extend(
        [
            asyncio.create_task(flush_windows()),
            asyncio.create_task(watch_heartbeats()),
            asyncio.create_task(db.flush_raw_loop(metrics)),
        ]
    )
    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if mqtt_bridge is not None:
            mqtt_bridge.stop()
        await db.close()

app = FastAPI(title="3DGS RSSI Backend", version="0.3.0", lifespan=lifespan)

@app.get("/health")
async def health() -> dict[str, object]:
    return {
        "ok": True,
        "postgres_connected": db.connected,
        "mqtt_host": settings.mqtt_host,
        "mqtt_port": settings.mqtt_port,
        "window_size_ms": settings.window_size_ms,
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

@app.get("/position/latest")
async def latest_position() -> dict[str, object]:
    return {
        "timestamp": now_ms(),
        "position_x": None,
        "position_y": None,
        "position_z": None,
        "confidence": 0.0,
        "status": "interface_ready_algorithm_pending",
    }

@app.websocket("/frames")
async def frames(websocket: WebSocket) -> None:
    await hub.connect(websocket)
    await hub.send(websocket, {"type": "node_status_snapshot", "nodes": registry.snapshot()})
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        hub.disconnect(websocket)

@app.get("/", response_class=HTMLResponse)
async def dashboard() -> str:
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
<h1>3DGS RSSI Network Monitor</h1>
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
