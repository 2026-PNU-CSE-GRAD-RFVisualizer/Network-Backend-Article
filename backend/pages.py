"""현장 측정용 웹 페이지 (7/23 강의실에서 노트북으로 사용).

설계 의도: 강의실에서 한 손에 ESP32 를 들고 있는 상태로 조작하므로
클릭 수를 최소화한다. point_id 는 자동 증가하고, 시작 버튼 하나로 30초가 끝난다.
좌표는 현장에서 입력하지 않고 종이에 적은 뒤 나중에 points.csv 로 일괄 등록한다.
"""

MEASURE_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>강의실 RSSI 측정</title>
<style>
 *{box-sizing:border-box}
 body{font-family:system-ui,Segoe UI,Arial,sans-serif;margin:0;padding:16px;
      background:#0f1115;color:#e6e6e6}
 h1{font-size:17px;margin:0 0 12px}
 h2{font-size:13px;color:#9aa0a6;margin:20px 0 6px;text-transform:uppercase;letter-spacing:.5px}
 .row{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end}
 label{display:block;font-size:11px;color:#9aa0a6;margin-bottom:3px}
 input,select{background:#1a1e27;border:1px solid #2a2f3a;color:#e6e6e6;
      border-radius:6px;padding:8px 10px;font-size:14px}
 input[readonly]{opacity:.6}
 button{background:#2b6cf6;border:0;color:#fff;border-radius:6px;padding:10px 16px;
      font-size:14px;font-weight:600;cursor:pointer}
 button:disabled{opacity:.4;cursor:not-allowed}
 button.ghost{background:#1a1e27;border:1px solid #2a2f3a}
 button.danger{background:#8b2c2c}
 button.big{font-size:20px;padding:18px 28px;width:100%;margin-top:12px}
 .panel{background:#151922;border:1px solid #2a2f3a;border-radius:10px;padding:14px;margin-top:12px}
 table{border-collapse:collapse;width:100%;margin-top:6px}
 th,td{border-bottom:1px solid #232833;padding:6px 8px;text-align:left;font-size:13px}
 th{color:#9aa0a6;font-weight:500;font-size:11px;text-transform:uppercase}
 .ok{color:#38d39f}.warn{color:#f0b429}.bad{color:#ff6b6b}
 #countdown{font-size:56px;font-weight:700;text-align:center;margin:8px 0;font-variant-numeric:tabular-nums}
 #banner{padding:10px 14px;border-radius:8px;margin-bottom:12px;font-size:13px;display:none}
 .chip{display:inline-block;background:#1a1e27;border:1px solid #2a2f3a;border-radius:99px;
      padding:3px 10px;font-size:12px;margin:2px}
 .chip.done{background:#14351f;border-color:#2f6b45;color:#7fe0a8}
 .muted{color:#9aa0a6;font-size:12px}
 a{color:#6ea8fe}
</style></head><body>

<h1>강의실 RSSI 측정 &nbsp;<a href="/monitor" style="font-size:13px;color:#6ea8fe">→ 노드 상태 모니터</a></h1>
<div id="mqttWarn" style="display:none;padding:12px 14px;border-radius:8px;margin-bottom:12px;
     background:#4a1414;color:#ff9b9b;font-weight:600;font-size:14px"></div>
<div id="banner"></div>

<div class="panel">
  <h2 style="margin-top:0">1. 실험</h2>
  <div class="row">
    <div><label>experiment_id</label><input id="expId" size="22" value=""></div>
    <div><label>AP BSSID</label><input id="bssid" size="18" placeholder="aa:bb:cc:dd:ee:ff"></div>
    <div><label>채널</label><input id="channel" size="4" type="number"></div>
    <div><button id="btnExp">실험 시작</button></div>
    <div><button id="btnExpEnd" class="ghost">실험 종료</button></div>
  </div>
  <div class="muted" id="expState" style="margin-top:8px">실험이 시작되지 않았습니다.</div>

  <div class="muted" style="margin-top:12px">AP(TX) 좌표 — Sionna RT 실행에 반드시 필요하다.</div>
  <div class="row" style="margin-top:6px">
    <div><label>x</label><input id="txX" size="6" type="number" step="0.01"></div>
    <div><label>y</label><input id="txY" size="6" type="number" step="0.01"></div>
    <div><label>z</label><input id="txZ" size="6" type="number" step="0.01"></div>
    <div><button id="btnTx" class="ghost">AP 좌표 등록</button></div>
    <div class="muted" id="txState" style="padding-bottom:8px">미등록</div>
  </div>
</div>

<div class="panel">
  <h2 style="margin-top:0">2. 노드 배치</h2>
  <div class="muted">고정 보정 센서 4대는 여기서 한 번만 배치한다.
    이동 센서는 아래에서 측정을 시작할 때 자동으로 재배치된다.</div>
  <div class="row" style="margin-top:8px">
    <div><label>node_id</label><input id="asgNode" size="10" placeholder="node1"></div>
    <div><label>point_id</label><input id="asgPoint" size="10" placeholder="cal-01"></div>
    <div><label>역할</label>
      <select id="asgRole">
        <option value="calibration">calibration</option>
        <option value="offset">offset</option>
        <option value="test">test</option>
      </select></div>
    <div><button id="btnAssign" class="ghost">배치</button></div>
    <div><button id="btnAssignAllOffset" class="ghost">전체를 offset-00 으로</button></div>
  </div>
  <table id="assignments"><thead><tr>
    <th>node_id</th><th>point_id</th><th>역할</th>
  </tr></thead><tbody></tbody></table>
</div>

<div class="panel">
  <h2 style="margin-top:0">3. 위치 측정</h2>
  <div class="row">
    <div><label>point_id</label><input id="pointId" size="12" value="offset-00"></div>
    <div><label>이동 센서 node_id</label><input id="movingNode" size="10" placeholder="node4"></div>
    <div><label>역할</label>
      <select id="role">
        <option value="offset">offset (장치 편차)</option>
        <option value="calibration">calibration (보정 4개)</option>
        <option value="test">test (평가 15개)</option>
      </select></div>
    <div><label>측정 시간(초)</label><input id="seconds" size="4" type="number" value="30"></div>
  </div>
  <div id="countdown">--</div>
  <button id="btnStart" class="big">측정 시작 (Enter)</button>
  <div class="row" style="margin-top:8px">
    <button id="btnStop" class="ghost" disabled>지금 종료</button>
    <button id="btnDiscard" class="danger" disabled>버리고 재측정</button>
  </div>
  <table id="progress"><thead><tr>
    <th>node_id</th><th>유효</th><th>전체</th><th>평균 RSSI</th><th>최근 수신</th>
  </tr></thead><tbody></tbody></table>
  <div class="muted" id="progressNote" style="margin-top:6px"></div>
</div>

<div class="panel">
  <h2 style="margin-top:0">4. 진행 상황</h2>
  <div id="doneList"><span class="muted">아직 측정한 위치가 없습니다.</span></div>
</div>

<div class="panel">
  <h2 style="margin-top:0">5. 좌표 등록 및 내보내기</h2>
  <div class="muted">현장에서는 좌표를 종이에 적고, 여기서 CSV 로 한 번에 등록한다.
    헤더: <code>point_id,point_role,pos_x,pos_y,pos_z,note</code></div>
  <textarea id="pointsCsv" rows="6" style="width:100%;margin-top:8px;background:#1a1e27;
    border:1px solid #2a2f3a;color:#e6e6e6;border-radius:6px;padding:8px;font-family:ui-monospace,monospace;font-size:13px"
    placeholder="point_id,point_role,pos_x,pos_y,pos_z&#10;cal-01,calibration,3.2,2.5,0.80&#10;test-01,test,5.4,4.8,1.55"></textarea>
  <div class="row" style="margin-top:8px">
    <button id="btnPoints" class="ghost">좌표 등록</button>
    <button id="btnOffsets" class="ghost">장치 offset 계산</button>
    <button id="btnExport">CSV 내보내기</button>
  </div>
  <pre id="exportOut" class="muted" style="white-space:pre-wrap;margin-top:10px"></pre>
</div>

<script>
let active = null;
let expId = null;

function banner(msg, kind){
  const el = document.getElementById('banner');
  el.style.display = msg ? 'block' : 'none';
  el.textContent = msg || '';
  el.style.background = kind === 'bad' ? '#3a1a1a' : kind === 'warn' ? '#3a3115' : '#14351f';
  el.style.color = kind === 'bad' ? '#ff9b9b' : kind === 'warn' ? '#f0cd7a' : '#7fe0a8';
}

async function api(path, body){
  const opt = body ? {method:'POST', headers:{'Content-Type':'application/json'},
                      body: JSON.stringify(body)} : {};
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if(!r.ok) throw new Error(data.detail || r.statusText);
  return data;
}

// point_id 자동 증가: test-01 -> test-02
function nextPointId(id){
  const m = id.match(/^(.*?)(\\d+)$/);
  if(!m) return id;
  const n = String(parseInt(m[2],10)+1).padStart(m[2].length,'0');
  return m[1]+n;
}

document.getElementById('btnExp').onclick = async () => {
  try{
    const d = await api('/experiment/start', {
      experiment_id: document.getElementById('expId').value.trim(),
      ap_bssid: document.getElementById('bssid').value.trim() || null,
      ap_channel: parseInt(document.getElementById('channel').value) || null,
    });
    banner('실험 시작: ' + d.experiment_id, 'ok');
    refresh();
  }catch(e){ banner(e.message, 'bad'); }
};

document.getElementById('btnExpEnd').onclick = async () => {
  await api('/experiment/end', {});
  banner('실험 종료', 'warn');
  refresh();
};

document.getElementById('btnTx').onclick = async () => {
  try{
    const d = await api('/experiment/tx', {
      tx_id: 'tx-01',
      pos_x: parseFloat(document.getElementById('txX').value),
      pos_y: parseFloat(document.getElementById('txY').value),
      pos_z: parseFloat(document.getElementById('txZ').value),
    });
    banner('AP 좌표 등록 완료', 'ok');
    refresh();
  }catch(e){ banner(e.message, 'bad'); }
};

document.getElementById('btnAssign').onclick = async () => {
  try{
    await api('/experiment/assign', {
      node_id: document.getElementById('asgNode').value.trim(),
      point_id: document.getElementById('asgPoint').value.trim(),
      point_role: document.getElementById('asgRole').value,
    });
    banner('배치 완료', 'ok');
    document.getElementById('asgNode').value = '';
    document.getElementById('asgPoint').value = '';
    refresh();
  }catch(e){ banner(e.message, 'bad'); }
};

// 장치 편차 측정 단계: 5대를 한 책상에 모아 놓고 모두 같은 위치로 배치한다.
document.getElementById('btnAssignAllOffset').onclick = async () => {
  try{
    const s = await api('/session/current');
    const nodes = new Set((s.assignments || []).map(a => a.node_id));
    const status = await api('/nodes/status');
    (status.nodes || []).forEach(n => nodes.add(n.node_id));
    if(!nodes.size){ banner('아직 수신된 노드가 없습니다.', 'warn'); return; }
    for(const node_id of nodes){
      await api('/experiment/assign',
                {node_id, point_id:'offset-00', point_role:'offset'});
    }
    banner(nodes.size + '개 노드를 offset-00 으로 배치했습니다.', 'ok');
    refresh();
  }catch(e){ banner(e.message, 'bad'); }
};

document.getElementById('btnStart').onclick = async () => {
  try{
    await api('/session/start', {
      point_id: document.getElementById('pointId').value.trim(),
      point_role: document.getElementById('role').value,
      seconds: parseInt(document.getElementById('seconds').value) || 30,
      moving_node_id: document.getElementById('movingNode').value.trim() || null,
    });
    banner('');
    refresh();
  }catch(e){ banner(e.message, 'bad'); }
};

document.getElementById('btnStop').onclick = async () => {
  await api('/session/stop', {discard:false}); refresh();
};
document.getElementById('btnDiscard').onclick = async () => {
  if(!confirm('이 측정을 버리고 다시 측정합니까?')) return;
  await api('/session/stop', {discard:true});
  banner('측정을 버렸습니다. 같은 point_id 로 다시 측정하세요.', 'warn');
  refresh();
};

document.getElementById('btnPoints').onclick = async () => {
  try{
    const d = await api('/experiment/points/import',
                        {csv: document.getElementById('pointsCsv').value});
    banner(d.count + '개 좌표 등록됨' + (d.errors.length ? ' / 오류 ' + d.errors.join('; ') : ''),
           d.errors.length ? 'warn' : 'ok');
  }catch(e){ banner(e.message, 'bad'); }
};

document.getElementById('btnOffsets').onclick = async () => {
  try{
    const d = await api('/experiment/offsets/compute', {});
    if(!d.ok){ banner(d.reason, 'bad'); return; }
    banner('offset 계산 완료. 기준 중앙값 ' + d.reference_median_dbm + ' dBm', 'ok');
    document.getElementById('exportOut').textContent = JSON.stringify(d.nodes, null, 2);
  }catch(e){ banner(e.message, 'bad'); }
};

document.getElementById('btnExport').onclick = async () => {
  try{
    const d = await api('/experiment/export', {});
    const qc = d.qc;
    banner(qc.ok ? 'Export 완료 — 품질 점검 통과' : 'Export 완료 — 확인 필요 항목 있음',
           qc.ok ? 'ok' : 'warn');
    document.getElementById('exportOut').textContent =
      '저장 위치: ' + d.path + '\\n\\n' +
      (qc.problems.length ? '문제:\\n' + qc.problems.map(x=>' - '+x).join('\\n') + '\\n\\n' : '') +
      (qc.warnings.length ? '경고:\\n' + qc.warnings.map(x=>' - '+x).join('\\n') + '\\n\\n' : '') +
      JSON.stringify(qc.counts, null, 2);
  }catch(e){ banner(e.message, 'bad'); }
};

document.addEventListener('keydown', e => {
  if(e.key === 'Enter' && e.target.tagName !== 'TEXTAREA' && !active){
    document.getElementById('btnStart').click();
  }
});

async function refresh(){
  let s;
  try{ s = await api('/session/current'); }
  catch(e){ banner('백엔드 연결 실패: ' + e.message, 'bad'); return; }

  expId = s.experiment_id;
  document.getElementById('expState').textContent = expId
    ? ('실험 진행 중: ' + expId + ' / BSSID ' + (s.ap_bssid || '미지정')
       + ' / 채널 ' + (s.ap_channel ?? '미지정'))
    : '실험이 시작되지 않았습니다.';
  if(expId && !document.getElementById('expId').value) {
    document.getElementById('expId').value = expId;
  }

  const wasActive = active;
  active = s.session;

  document.getElementById('btnStart').disabled = !!active || !expId;
  document.getElementById('btnStop').disabled = !active;
  document.getElementById('btnDiscard').disabled = !active;

  const cd = document.getElementById('countdown');
  if(active){
    const sec = Math.ceil(active.remaining_ms / 1000);
    cd.textContent = sec + '초';
    cd.className = sec <= 5 ? 'warn' : '';
  }else{
    cd.textContent = '--';
    cd.className = '';
    // 방금 끝났으면 다음 위치로 자동 증가
    if(wasActive){
      const el = document.getElementById('pointId');
      el.value = nextPointId(wasActive.point_id);
      banner(wasActive.point_id + ' 측정 완료. 다음: ' + el.value, 'ok');
    }
  }

  const tb = document.querySelector('#progress tbody');
  const rows = s.progress || [];
  tb.innerHTML = rows.map(x => {
    const age = x.last_ms ? (Date.now() - x.last_ms) : null;
    const cls = x.valid >= 20 ? 'ok' : x.valid >= 10 ? 'warn' : 'bad';
    return `<tr><td>${x.node_id}</td>
      <td class="${cls}">${x.valid}</td><td>${x.total}</td>
      <td>${x.avg_filtered == null ? '-' : x.avg_filtered.toFixed(1)}</td>
      <td>${age == null ? '-' : (age/1000).toFixed(1) + '초 전'}</td></tr>`;
  }).join('');
  document.getElementById('progressNote').textContent = active
    ? (rows.length + '개 노드 수신 중 (5개여야 정상)')
    : '';

  // MQTT 연결 상태 — 샘플이 안 들어올 때 원인을 즉시 구분할 수 있어야 한다
  const mq = s.mqtt || {};
  const mw = document.getElementById('mqttWarn');
  if(mq.connected === false){
    mw.style.display = 'block';
    mw.textContent = 'MQTT 브로커 연결 끊김 — 지금 측정하면 데이터가 저장되지 않습니다. '
                   + '브로커를 확인하세요.';
  }else if(mq.disconnect_count > 0){
    mw.style.display = 'block';
    mw.style.background = '#3a3115'; mw.style.color = '#f0cd7a';
    mw.textContent = `MQTT 재연결됨 (끊김 ${mq.disconnect_count}회). `
                   + '끊긴 동안 측정한 위치는 샘플 수를 확인하고 필요하면 재측정하세요.';
  }else{
    mw.style.display = 'none';
  }

  const tx = (s.tx || [])[0];
  const txEl = document.getElementById('txState');
  if(tx && tx.pos_x != null){
    txEl.textContent = `등록됨 (${tx.pos_x}, ${tx.pos_y}, ${tx.pos_z})`;
    txEl.className = 'ok';
    if(!document.getElementById('txX').value){
      document.getElementById('txX').value = tx.pos_x;
      document.getElementById('txY').value = tx.pos_y;
      document.getElementById('txZ').value = tx.pos_z;
    }
  }else{
    txEl.textContent = '미등록 — Sionna RT 실행 불가';
    txEl.className = 'bad';
  }

  const asg = s.assignments || [];
  document.querySelector('#assignments tbody').innerHTML = asg.length
    ? asg.map(a => `<tr><td>${a.node_id}</td><td>${a.point_id}</td>
        <td>${a.point_role}</td></tr>`).join('')
    : '<tr><td colspan="3" class="muted">배치된 노드가 없습니다.</td></tr>';

  const done = s.done_points || [];
  document.getElementById('doneList').innerHTML = done.length
    ? done.map(p => `<span class="chip done">${p.point_id} · ${p.valid_samples}</span>`).join('')
    : '<span class="muted">아직 측정한 위치가 없습니다.</span>';
}

// 기본 experiment_id 를 오늘 날짜로 채운다
(function(){
  const d = new Date();
  const ymd = d.getFullYear() + String(d.getMonth()+1).padStart(2,'0')
            + String(d.getDate()).padStart(2,'0');
  document.getElementById('expId').value = 'classroom_' + ymd;
})();

setInterval(refresh, 500);
refresh();
</script></body></html>"""
