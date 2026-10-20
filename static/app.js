const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const COL = {HELP: '#b91c1c', PAIN: '#c2410c', WASHROOM: '#7c3aed', MEDICINE: '#be185d', WATER: '#0f766e',
             FOOD: '#a16207', REPOSITION: '#1d4ed8', BLANKET: '#475569'};
const fx = (v, d = 2) => v == null ? '–' : (+v).toFixed(d);
const esc = x => String(x).replace(/[&<>"]/g, c => '&#' + c.charCodeAt(0) + ';');
const PAGE = document.body.dataset.page;

async function api(path, body) {
  const r = await fetch(path, body !== undefined
    ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {});
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
const fail = e => alert(e.message);

// a chip is one request: colour AND text show the category
const short = n => 'Pt' + String(n).replace(/\D/g, '');
function chip(r, cls = '') {
  const aged = r.status === 'queued' && r.eff < r.pri ? ' ▲' : '';
  const tip = `${r.pat}, level ${r.pri}${aged ? ' (aged to ' + r.eff + ')' : ''}, arrived ${r.arr}s`;
  return `<span class="chip ${cls}" style="--c:${COL[r.cat]}" title="${esc(tip)}">#${r.id} ${r.cat} · ${short(r.pat)}${aged}</span>`;
}
const chips = (list, empty) => list.length ? list.map(r => chip(r)).join('') : `<em>${empty}</em>`;

let S = null;
function ack(id) { api(`/api/request/${id}/ack`, {}).then(draw).catch(fail); }

function draw(s) {
  if (!s) return;
  S = s;
  $('#clock').textContent = fx(s.t, 1);
  const st = s.finished ? 'finished' : s.running ? 'running' : 'paused';
  $('#status').textContent = st;
  $('#status').className = 'pill ' + (s.finished ? 'fin' : s.running ? 'run' : '');
  $$('[data-m]').forEach(e => e.textContent = fx(s.m[e.dataset.m], +e.dataset.d));
  $$('[data-cfg]').forEach(e => { if (document.activeElement !== e) e.value = String(s.cfg[e.dataset.cfg]); });

  if ($('#p-q')) {
    $('#alerts').innerHTML = s.alerts.map(a =>
      `<div class="alert" role="alert">🆘 HELP from ${esc(a.pat)} (#${a.id}), ${a.status === 'queued' ? 'waiting' : 'being served'}
       <button class="sm pri" onclick="ack(${a.id})">Acknowledge</button></div>`).join('');
    $('#p-in').innerHTML = chips(s.incoming, 'none');
    $('#qtxt').textContent = `${s.m.qlen} / ${s.m.capacity}`;
    $('#qbar').style.width = Math.min(100, 100 * s.m.qlen / s.m.capacity) + '%';
    $('#p-q').innerHTML = s.queue.length ? s.queue.map((r, i) => chip(r, i ? '' : 'first')).join('') : '<em>empty</em>';
    $('#p-cg').innerHTML = s.caregivers.map((r, i) => `<div class="cg"><small>Caregiver ${i + 1}</small><br>` + (r
      ? chip(r) + `<div class="bar"><i style="width:${Math.min(100, 100 * (r.svc - r.left) / r.svc)}%"></i></div>`
      : '<em>idle</em>') + '</div>').join('');
    $('#p-done').innerHTML = chips(s.done, 'none');
    $('#p-lost').innerHTML = chips(s.lost, 'none');
    $('#log').innerHTML = s.log.map(l => `<div><span>${l.wall} · t=${l.t}s</span>${esc(l.msg)}</div>`).join('');
    const pat = $('#mpat'), cat = $('#mcat');
    if (!pat.options.length) {
      pat.innerHTML = s.patients.map(p => `<option>${esc(p)}</option>`).join('');
      cat.innerHTML = s.cats.map(c => `<option>${c}</option>`).join('');
    }
  }
  if ($('#rtable') && s.all) {
    const f = $('#flt').value;
    $('#rtable tbody').innerHTML = s.all.filter(r => !f || r.status === f).map(r => `<tr><td>${r.id}</td><td>${chip(r)}</td>
      <td>${esc(r.pat)}</td><td>${r.pri}</td><td>${fx(r.arr, 1)}</td><td class="st-${r.status}">${r.status}</td>
      <td>${fx(r.wait, 1)}</td><td>${r.pre || ''}</td>
      <td>${!r.ack && ['queued', 'processing'].includes(r.status) ? `<button class="sm pri" onclick="ack(${r.id})">Acknowledge</button>` : ''}</td></tr>`).join('');
  }
  if ($('#charts')) {
    line('c-q', s.hist, 'q', '#0e9aa7'); line('c-w', s.hist, 'w', '#6366f1');
    line('c-th', s.hist, 'th', '#15803d'); line('c-em', s.hist, 'em', '#b91c1c');
    const pp = s.m.per_patient, mx = Math.max(...Object.values(pp), 0.01);
    $('#pp').innerHTML = Object.keys(pp).length ? Object.entries(pp).map(([k, v]) =>
      `<div class="eb"><span>${esc(k)}</span><i style="width:${300 * v / mx}px;background:#0e9aa7"></i>${fx(v, 1)} s</div>`).join('')
      : '<em>Start a run to see this.</em>';
  }
}

function line(id, h, k, col) {
  const c = document.getElementById(id), g = c.getContext('2d'), W = c.width, H = c.height, P = 36;
  g.clearRect(0, 0, W, H);
  g.font = '11px sans-serif';
  if (h.length < 2) { g.fillStyle = '#64748b'; g.fillText('Start a run to see data', P, H / 2); return; }
  const mx = Math.max(...h.map(d => d[k]), 0.001) * 1.1, t0 = h[0].t, t1 = h[h.length - 1].t;
  const X = t => P + (W - P - 8) * (t - t0) / Math.max(t1 - t0, 1), Y = v => H - 22 - (H - 34) * v / mx;
  g.strokeStyle = '#e2e8f0'; g.fillStyle = '#64748b';
  for (let i = 0; i <= 4; i++) {
    const v = mx * i / 4;
    g.beginPath(); g.moveTo(P, Y(v)); g.lineTo(W - 8, Y(v)); g.stroke();
    g.fillText(v.toFixed(mx < 1 ? 2 : v < 10 ? 1 : 0), 2, Y(v) + 4);
  }
  g.strokeStyle = col; g.lineWidth = 2; g.beginPath();
  h.forEach((d, i) => i ? g.lineTo(X(d.t), Y(d[k])) : g.moveTo(X(d.t), Y(d[k])));
  g.stroke();
  g.fillStyle = '#64748b'; g.fillText(t1 + ' s', W - 40, H - 6);
}

// ---- saved runs (Requests page)
async function loadRuns() {
  if (!$('#runs')) return;
  try {
    const rows = await api('/api/runs');
    $('#runs tbody').innerHTML = rows.map(r => `<tr><td>${r.id}</td><td>${esc(r.started)}</td><td>${esc(r.status)}</td>
      <td>${esc(r.algo)}</td><td>${r.preempt ? 'on' : 'off'}</td><td>${r.caregivers}</td><td>${esc(r.load)}</td>
      <td>${r.generated ?? ''}</td><td>${fx(r.avg_wait, 1)}</td>
      <td><a href="/export/requests.csv?run=${r.id}">requests CSV</a></td></tr>`).join('');
  } catch (e) { /* ignore: table keeps old rows */ }
}

// ---- policy comparison (Performance page)
const POLCOL = {'FCFS': '#94a3b8', 'Priority': '#0e9aa7', 'Priority + Aging': '#6366f1',
                'Priority + Preemption': '#0f766e', 'Priority + Aging + Preemption': '#4338ca'};
let EXPDATA = null;
function showExp() {
  if (!EXPDATA || !$('#etable')) return;
  const load = $('#eload').value, rows = EXPDATA.results.filter(r => r.load === load);
  $('#etable tbody').innerHTML = rows.map(r => `<tr><td><b>${esc(r.policy)}</b></td><td>${fx(r.avg_wait, 1)}</td>
    <td>${fx(r.help_wait, 1)}</td><td>${fx(r.emerg_wait, 1)}</td><td>${fx(r.routine_wait, 1)}</td><td>${fx(r.routine_max, 1)}</td>
    <td>${fx(r.starved, 1)}</td><td>${fx(r.dropped, 1)}</td><td>${fx(r.preemptions, 1)}</td><td>${fx(r.fairness, 2)}</td></tr>`).join('');
  const bar = (k, title) => {
    const mx = Math.max(...rows.map(r => r[k]), 0.01);
    return `<h4 style="margin-top:14px">${title} (s)</h4>` + rows.map(r =>
      `<div class="eb"><span>${esc(r.policy)}</span><i style="width:${300 * r[k] / mx}px;background:${POLCOL[r.policy]}"></i>${fx(r[k], 1)}</div>`).join('');
  };
  $('#ebars').innerHTML = bar('emerg_wait', 'HELP + PAIN mean wait') + bar('routine_max', 'Longest routine wait');
  const c = EXPDATA.cfg;
  $('#estat').textContent = `${EXPDATA.ts} · ${c.seeds} runs per setting · ${c.caregivers} caregivers · queue ${c.capacity} · averages over runs`;
}
async function loadExp() { try { const e = await api('/api/experiments'); if (e.length) { EXPDATA = e[0]; showExp(); } } catch (e) { } }
async function pollExp() {
  const s = await api('/api/experiment/status');
  if (s.running) { $('#estat').textContent = `Running… ${s.done} of ${s.total}`; setTimeout(pollExp, 600); return; }
  $('#erun').disabled = false;
  if (s.error) { $('#estat').textContent = 'Failed: ' + s.error; return; }
  loadExp();
}

// ---- wiring
$$('[data-act]').forEach(b => b.onclick = () => api('/api/control', {action: b.dataset.act}).then(draw).catch(fail));
$$('[data-cfg]').forEach(e => e.onchange = () => api('/api/control', {action: 'config', [e.dataset.cfg]: e.value}).then(draw).catch(fail));
if ($('#msub')) $('#msub').onclick = () => api('/api/request', {cat: $('#mcat').value, patient: $('#mpat').value})
  .then(s => { $('#mmsg').textContent = ''; draw(s); }).catch(e => { $('#mmsg').textContent = e.message; });
if ($('#flt')) $('#flt').onchange = () => draw(S);
if ($('#eload')) $('#eload').onchange = showExp;
if ($('#erun')) $('#erun').onclick = async () => {
  $('#erun').disabled = true;
  try {
    await api('/api/experiment', {seeds: $('#e-seeds').value, caregivers: $('#e-cg').value, capacity: $('#e-cap').value});
    pollExp();
  } catch (e) { $('#erun').disabled = false; $('#estat').textContent = e.message; }
};

async function poll() {
  try { draw(await api('/api/state' + ($('#rtable') ? '?all=1' : ''))); } catch (e) { }
  setTimeout(poll, 500);
}
poll();
loadRuns(); setInterval(loadRuns, 3000);
loadExp();
