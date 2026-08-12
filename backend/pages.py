"""현장 측정 페이지 (최종 실험: Run + TestSegment 흐름).

새 API 사용: /offset-run/*, /run/*, /test-segment/*, /run/current.
프레임워크 없이 단일 HTML/JS. 모바일 우선(§11.5): 현재 위치·남은 시간·큰 버튼을 위에.
"""

MEASURE_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RF 최종 실험 측정</title>
<style>
 :root{--bg:#0f1115;--card:#1a1e27;--bd:#2a2f3a;--fg:#e6e6e6;--mut:#9aa0a6;
   --ok:#38d39f;--warn:#ffb020;--err:#ff6b6b;--acc:#6ea8fe}
 *{box-sizing:border-box} body{font-family:system-ui,Segoe UI,Arial,sans-serif;
   margin:0;background:var(--bg);color:var(--fg);padding:12px;max-width:820px;margin:0 auto}
 h1{font-size:17px;margin:6px 0} h2{font-size:13px;color:var(--mut);margin:18px 0 8px;
   text-transform:uppercase;letter-spacing:.04em}
 .card{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px;margin:10px 0}
 .big{font-size:15px} button{font-size:15px;padding:12px 16px;border-radius:9px;border:1px solid var(--bd);
   background:#222736;color:var(--fg);cursor:pointer;margin:4px 4px 4px 0}
 button.primary{background:var(--acc);color:#08131f;border-color:var(--acc);font-weight:700}
 button.warn{background:#3a2a10;border-color:var(--warn);color:var(--warn)}
 button.err{background:#3a1414;border-color:var(--err);color:var(--err)}
 button:disabled{opacity:.4;cursor:not-allowed}
 input,select{font-size:14px;padding:8px;border-radius:7px;border:1px solid var(--bd);
   background:#10141c;color:var(--fg);margin:2px}
 .row{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
 .kv{background:#10141c;border:1px solid var(--bd);border-radius:8px;padding:10px}
 .kv .l{font-size:11px;color:var(--mut)} .kv .v{font-size:18px;font-weight:600}
 .on{color:var(--ok)} .off{color:var(--err)} .muted{color:var(--mut)}
 .hero{text-align:center;padding:16px} .hero .pt{font-size:34px;font-weight:800}
 .hero .timer{font-size:28px;font-weight:700;margin:6px 0}
 .pill{display:inline-block;padding:3px 9px;border-radius:20px;font-size:12px;border:1px solid var(--bd)}
 .pill.run{background:#10261d;color:var(--ok);border-color:var(--ok)}
 .banner{padding:10px;border-radius:8px;margin:8px 0;display:none}
 .banner.err{display:block;background:#3a1414;color:var(--err);border:1px solid var(--err)}
 textarea{width:100%;min-height:70px;background:#10141c;color:var(--fg);border:1px solid var(--bd);border-radius:7px;padding:8px}
 .done span{display:inline-block;background:#10261d;color:var(--ok);border:1px solid var(--ok);
   border-radius:6px;padding:2px 7px;margin:2px;font-size:12px}
 small{color:var(--mut)}
</style></head><body>

<h1>RF 최종 실험 측정 <a href="/monitor" style="font-size:12px;color:var(--acc)">→ 모니터</a></h1>
<div id="mqttBanner" class="banner err"></div>

<!-- ===== 현장 조작 (모바일 우선: 위에) ===== -->
<div class="card" id="fieldCard">
  <div class="hero">
    <div class="muted" id="dirLabel">-</div>
    <div class="pt" id="curPoint">준비</div>
    <div class="timer" id="timer">—</div>
    <div id="segState" class="muted"></div>
  </div>
  <div class="row" style="justify-content:center">
    <button class="primary big" id="btnPrepare" onclick="prepareSegment()">위치 도착 · 안정화 시작</button>
    <button class="warn" id="btnStop" onclick="stopSegment()">현재 Test 즉시 종료</button>
    <button class="err" id="btnDiscard" onclick="discardSegment()">버리고 재측정</button>
  </div>
  <div class="row" style="justify-content:center;margin-top:4px">
    <input id="remeasurePoint" placeholder="완료 위치 재측정 (예: T4)" size="12">
    <button onclick="remeasure()">이 위치 재측정</button>
  </div>
  <div class="done" id="donePoints" style="margin-top:10px"></div>
</div>

<!-- ===== Run 진행 상태 ===== -->
<h2>Run 상태</h2>
<div class="card">
  <div class="row" style="justify-content:space-between">
    <div><span class="pill run" id="runPill" style="display:none">running</span>
      <span id="runInfo" class="muted">Run 없음</span></div>
    <div class="muted">경과 <b id="runElapsed">—</b></div>
  </div>
  <h2>C1~C4 / T 상태</h2>
  <div class="grid" id="nodeGrid"></div>
</div>

<!-- ===== 실험 준비 (아래로) ===== -->
<h2>실험 준비</h2>
<div class="card">
  <div class="row">
    <input id="expId" placeholder="experiment_id (예: classroom)" size="20">
    <input id="bssid" placeholder="AP BSSID" size="16">
    <input id="chan" placeholder="채널" size="4">
    <button onclick="startExperiment()">실험 시작</button>
  </div>
  <small id="expState">실험 미시작</small>

  <h2>센서 배정</h2>
  <div class="row">
    <span>C1</span><input id="nC1" placeholder="node_id" size="8">
    <span>C2</span><input id="nC2" placeholder="node_id" size="8">
    <span>C3</span><input id="nC3" placeholder="node_id" size="8">
    <span>C4</span><input id="nC4" placeholder="node_id" size="8">
    <span>T(이동)</span><input id="nT" placeholder="node_id" size="8">
    <button onclick="assignAll()">배정</button>
  </div>

  <h2>장비 편차(Offset)</h2>
  <div class="row">
    <select id="offsetPhase"><option value="pre">사전(pre)</option><option value="post">사후(post)</option></select>
    <button onclick="offsetStart()">측정 시작</button>
    <button onclick="offsetStop()">종료</button>
    <button onclick="offsetCompute()">계산</button>
    <button onclick="attachPost()">사후를 Run에 연결</button>
    <small id="offsetState">-</small>
  </div>

  <h2>AP(TX) 좌표</h2>
  <div class="row">
    <input id="txx" placeholder="x" size="5"><input id="txy" placeholder="y" size="5">
    <input id="txz" placeholder="z" size="5"><button onclick="setTx()">TX 등록</button>
    <small id="txState"></small>
  </div>

  <h2>본 실험 Run</h2>
  <div class="row">
    <select id="direction"><option value="forward">정방향 T1→T10</option>
      <option value="reverse">역방향 T10→T1</option></select>
    <input id="passIndex" type="number" value="1" size="3" title="pass_index">
    <button class="primary" onclick="startRun()">전체 실험 시작</button>
    <button class="err" onclick="endRun()">전체 실험 종료</button>
  </div>

  <h2>좌표 등록 (points.csv)</h2>
  <textarea id="pointsCsv" placeholder="point_id,point_role,pos_x,pos_y,pos_z
T1,test,1.0,2.0,0.8
C1,calibration,..."></textarea>
  <div class="row"><button onclick="importPoints()">좌표 등록</button>
    <small id="pointsState"></small></div>

  <h2>내보내기</h2>
  <div class="row"><button onclick="exportCsv()">CSV 내보내기 + 품질점검</button>
    <small id="exportState"></small></div>
</div>

<script>
const TOTAL_POINTS = 10;
let cfg = {stab: 20, rec: 120};   // /health 에서 갱신
let last = null;

async function api(path, body){
  const opt = body===undefined ? {} :
    {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)};
  const r = await fetch(path, opt);
  const d = await r.json().catch(()=>({}));
  if(!r.ok){ throw new Error(d.detail || (r.status+' 오류')); }
  return d;
}
function note(id, msg, ok){ const e=document.getElementById(id); if(e){e.textContent=msg; e.style.color=ok?'var(--ok)':'var(--err)';} }

async function startExperiment(){
  try{ const d=await api('/experiment/start',{experiment_id:document.getElementById('expId').value,
      ap_bssid:document.getElementById('bssid').value||null,
      ap_channel:parseInt(document.getElementById('chan').value)||null});
    note('expState','실험 시작: '+d.experiment_id, true);
  }catch(e){ note('expState',e.message,false);} }

async function assignAll(){
  const map=[['nC1','C1'],['nC2','C2'],['nC3','C3'],['nC4','C4']];
  try{
    for(const [f,pt] of map){ const v=document.getElementById(f).value.trim();
      if(v) await api('/experiment/assign',{node_id:v,point_id:pt,point_role:'calibration'}); }
    const t=document.getElementById('nT').value.trim();
    if(t) await api('/experiment/assign',{node_id:t,point_id:'T-move',point_role:'test'});
    note('expState','배정 완료', true);
  }catch(e){ note('expState',e.message,false);} }

let lastPostOffsetId = null;
function offPhase(){ return document.getElementById('offsetPhase').value; }
async function offsetStart(){ try{ await api('/offset-run/start',{phase:offPhase()}); note('offsetState',offPhase()+' Offset 측정 중...',true);}catch(e){note('offsetState',e.message,false);} }
async function offsetStop(){ try{ await api('/offset-run/stop',{}); note('offsetState','Offset 측정 종료',true);}catch(e){note('offsetState',e.message,false);} }
async function offsetCompute(){ try{ const d=await api('/experiment/offsets/compute',{phase:offPhase()});
  if(d.ok && offPhase()==='post') lastPostOffsetId=d.offset_run_id;
  note('offsetState', d.ok?(offPhase()+' offset 계산 완료: '+d.nodes.length+'대'):d.reason, d.ok);}catch(e){note('offsetState',e.message,false);} }
async function attachPost(){ if(!lastPostOffsetId){ note('offsetState','먼저 사후(post) 측정·계산을 하세요',false); return; }
  try{ await api('/run/attach-post-offset',{offset_run_id:lastPostOffsetId}); note('offsetState','사후 Offset 을 Run 에 연결(drift용)',true);}catch(e){note('offsetState',e.message,false);} }
async function remeasure(){ const p=document.getElementById('remeasurePoint').value.trim();
  if(!p){ alert('재측정할 위치(예: T4)를 입력하세요'); return; }
  const n=parseInt(p.replace(/[^0-9]/g,''))||1;
  try{ await api('/test-segment/prepare',{point_id:p, order_index:n, stabilization_seconds:cfg.stab, recording_seconds:cfg.rec}); }
  catch(e){ alert('재측정 시작 실패: '+e.message); } }

async function setTx(){ try{ await api('/experiment/tx',{tx_id:'tx-01',
    pos_x:parseFloat(document.getElementById('txx').value),
    pos_y:parseFloat(document.getElementById('txy').value),
    pos_z:parseFloat(document.getElementById('txz').value)});
  note('txState','TX 등록됨',true);}catch(e){note('txState',e.message,false);} }

async function startRun(){ try{ await api('/run/start',{
    direction:document.getElementById('direction').value,
    pass_index:parseInt(document.getElementById('passIndex').value)||1});
  }catch(e){ alert('실험 시작 실패: '+e.message);} }
async function endRun(){ try{ await api('/run/end',{}); }catch(e){ alert('종료 실패: '+e.message);} }

function nextPoint(dir, completed){
  const done=new Set(completed.map(c=>c.point_id));
  const seq=[]; for(let i=1;i<=TOTAL_POINTS;i++) seq.push('T'+i);
  if(dir==='reverse') seq.reverse();
  for(let i=0;i<seq.length;i++) if(!done.has(seq[i])) return {point_id:seq[i], order_index:i+1};
  return null;
}

async function prepareSegment(){
  if(!last || !last.run){ alert('먼저 전체 실험을 시작하세요.'); return; }
  const np=nextPoint(last.run.direction, last.completed_points||[]);
  if(!np){ alert('모든 위치를 측정했습니다.'); return; }
  try{ await api('/test-segment/prepare',{point_id:np.point_id, order_index:np.order_index,
      stabilization_seconds:cfg.stab, recording_seconds:cfg.rec}); }
  catch(e){ alert('안정화 시작 실패: '+e.message); } }

async function stopSegment(){ try{ await api('/test-segment/stop',{}); }catch(e){ alert(e.message);} }
async function discardSegment(){ if(!confirm('현재 Test 를 버리고 재측정할까요? (C1~C4 원본은 보존)')) return;
  try{ await api('/test-segment/discard',{}); }catch(e){ alert(e.message);} }

async function importPoints(){ try{ const d=await api('/experiment/points/import',{csv:document.getElementById('pointsCsv').value});
  note('pointsState', d.count+'개 등록'+(d.errors.length?(' / 오류 '+d.errors.length):''), !d.errors.length);}catch(e){note('pointsState',e.message,false);} }
async function exportCsv(){ try{ note('exportState','내보내는 중...',true); const d=await api('/experiment/export',{});
  note('exportState', d.qc.ok?'완료 (QC 통과)':('완료 (QC 문제 '+d.qc.problems.length+')'), d.qc.ok);}catch(e){note('exportState',e.message,false);} }

function fmt(ms){ if(ms==null) return '—'; const s=Math.max(0,Math.round(ms/1000));
  return Math.floor(s/60)+':'+String(s%60).padStart(2,'0'); }

function render(s){
  last=s;
  // MQTT 경고
  const mq=s.mqtt||{}; const banner=document.getElementById('mqttBanner');
  if(mq.connected===false){ banner.textContent='⚠ MQTT 연결 끊김 — 측정 데이터가 안 들어옵니다'; banner.classList.add('err'); }
  else banner.classList.remove('err');

  document.getElementById('expState').textContent = s.experiment_id? ('실험: '+s.experiment_id) : '실험 미시작';
  document.getElementById('offsetState').textContent = s.offset_run? 'Offset 측정 중...' : (document.getElementById('offsetState').textContent||'-');

  // Run
  const runPill=document.getElementById('runPill');
  if(s.run){ runPill.style.display='inline-block';
    document.getElementById('runInfo').textContent = s.run.run_id+' · '+s.run.direction+' · pass '+s.run.pass_index;
    document.getElementById('runElapsed').textContent = fmt(s.run.elapsed_ms);
    document.getElementById('dirLabel').textContent = s.run.direction==='reverse'?'역방향 T10→T1':'정방향 T1→T10';
  } else { runPill.style.display='none'; document.getElementById('runInfo').textContent='Run 없음';
    document.getElementById('runElapsed').textContent='—'; document.getElementById('dirLabel').textContent='-'; }

  // TestSegment 히어로
  const seg=s.test_segment;
  const curPoint=document.getElementById('curPoint'), timer=document.getElementById('timer'), st=document.getElementById('segState');
  const bP=document.getElementById('btnPrepare'), bS=document.getElementById('btnStop'), bD=document.getElementById('btnDiscard');
  if(seg){
    curPoint.textContent=seg.point_id;
    if(seg.state==='stabilizing'){ st.textContent='안정화 중'; timer.textContent='안정화 '+fmt(seg.remaining_stabilization_ms); }
    else if(seg.state==='recording'){ st.textContent='● 기록 중'; timer.textContent='기록 '+fmt(seg.remaining_recording_ms); }
    else { st.textContent='완료'; timer.textContent='—'; }
    bP.disabled=true; bS.disabled=false; bD.disabled=false;
  } else {
    const np = s.run ? nextPoint(s.run.direction, s.completed_points||[]) : null;
    curPoint.textContent = s.run ? (np? ('다음: '+np.point_id) : '모두 완료') : '준비';
    timer.textContent='—'; st.textContent = s.run? '위치로 이동 후 버튼을 누르세요' : '전체 실험을 시작하세요';
    bP.disabled = !(s.run && np); bS.disabled=true; bD.disabled=true;
  }

  // 완료 위치
  document.getElementById('donePoints').innerHTML =
    (s.completed_points||[]).sort((a,b)=>a.order_index-b.order_index)
      .map(c=>'<span>'+c.point_id+'</span>').join('');

  // 노드 그리드
  const nodes=[...(s.calibration_nodes||[])]; if(s.test_node && s.test_node.node_id) nodes.push({...s.test_node,_t:true});
  document.getElementById('nodeGrid').innerHTML = nodes.map(n=>{
    const gap = n.gap_ms==null ? '-' : (n.gap_ms>5000?('⚠'+Math.round(n.gap_ms/1000)+'s'):(Math.round(n.gap_ms/1000)+'s'));
    return '<div class="kv"><div class="l">'+(n._t?'T ':'')+(n.point_id||'')+' · '+n.node_id+'</div>'+
      '<div class="v '+(n.online?'on':'off')+'">'+(n.online?'ONLINE':'OFFLINE')+'</div>'+
      '<div class="l">샘플 '+(n.samples||0)+' · 공백 '+gap+'</div></div>';
  }).join('') || '<small class="muted">배정된 센서 없음</small>';
}

async function tick(){
  try{ const s=await api('/run/current'); render(s); }catch(e){ /* 백엔드 재기동 중일 수 있음 */ }
}
async function init(){
  try{ const h=await fetch('/health').then(r=>r.json());
    if(h.test_stabilization_seconds) cfg.stab=h.test_stabilization_seconds;
    if(h.test_recording_seconds) cfg.rec=h.test_recording_seconds; }catch(e){}
  setInterval(tick, 500); tick();
}
init();
</script></body></html>"""
