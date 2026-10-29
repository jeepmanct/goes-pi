const $ = s => document.querySelector(s);
const api = async (path, body) => {
  const r = await fetch(path, body === undefined ? {} :
    {method: 'POST', body: JSON.stringify(body), headers: {'Content-Type': 'application/json'}});
  return r.json();
};

// ---- units (US / metric), remembered per browser
const Units = (() => {
  let metric = false;
  try { metric = localStorage.units === 'metric'; } catch {}
  const r0 = v => Math.round(v);
  const u = {
    get metric() { return metric; },
    set(m) { metric = m; try { localStorage.units = m ? 'metric' : 'us'; } catch {} },
    tu: () => metric ? '°C' : '°F', su: () => metric ? 'km/h' : 'mph', pu: () => metric ? 'hPa' : 'inHg', du: () => metric ? 'km' : 'mi',
    t: f => f == null ? f : metric ? Math.round((f - 32) * 50 / 9) / 10 : f,       // temperature, F in
    dt: f => f == null ? f : metric ? Math.round(f * 50 / 9) / 10 : f,             // temperature difference
    wmph: v => v == null ? v : metric ? r0(v * 1.609344) : v,
    wkt: v => v == null ? v : metric ? r0(v * 1.852) : r0(v * 1.150779),
    p: v => v == null ? v : metric ? Math.round(v * 33.8639 * 10) / 10 : v,
    d: v => v == null ? v : metric ? Math.round(v * 1.609344) : v,
    // convert units inside NWS free text
    text(s) {
      if (!metric || !s) return s;
      const F = x => Math.round((x - 32) * 5 / 9);
      return String(s)
        .replace(/(-?\d+)\s*°\s*F\b/g, (m, a) => F(+a) + '°C')
        .replace(/\b(\d+) to (\d+) mph/g, (m, a, b) => r0(a * 1.609344) + ' to ' + r0(b * 1.609344) + ' km/h')
        .replace(/\b(\d+) mph/g, (m, a) => r0(a * 1.609344) + ' km/h')
        .replace(/\b(\d+) kt\b/g, (m, a) => r0(a * 1.852) + ' km/h')
        .replace(/(dew point )(-?\d+)°/g, (m, a, b) => a + F(+b) + '°')
        .replace(/\b(\d+) mi\b/g, (m, a) => r0(a * 1.609344) + ' km')
        .replace(/\b(?:(to|into) |in |near |around )(?:the )?(?:(lower|mid|upper) )?(\d)0s\b/gi, (m, t, w, d) => (t ? t + ' ' : '') + 'around ' + F(+d * 10 + ({lower: 2, mid: 5, upper: 8}[(w || '').toLowerCase()] ?? 5)) + '°C')
        .replace(/\b(high|low|highs|lows)( near| around| of)? (-?\d+)\b(?!\s*°)/gi, (m, a, b, c) => a + (b || '') + ' ' + F(+c) + '°C')
        .replace(/\b(\d+) to (\d+) inch(?:es)?\b/g, (m, a, b) => (a * 2.54).toFixed(0) + ' to ' + (b * 2.54).toFixed(0) + ' cm')
        .replace(/\b(\d+(?:\.\d+)?) (?:inch|inches)\b/g, (m, a) => (a * 2.54).toFixed(0) + ' cm')
        .replace(/\b(\d+) ft\b/g, (m, a) => (a * 0.3048).toFixed(1) + ' m')
        .replace(/(\d+(?:\.\d+)?) inHg/g, (m, a) => (a * 33.8639).toFixed(1) + ' hPa');
    },
  };
  return u;
})();
function unitsRefresh() {
  const b = $('#units-btn'); if (b) b.textContent = Units.metric ? '°C · km/h' : '°F · mph';
  const active = document.querySelector('section.active'); if (active) show(active.id);
  if (typeof loadWeather === 'function' && !active) loadWeather();
}
{
  const b = document.createElement('button'); b.id = 'units-btn'; b.className = 'pill'; b.style.cssText = 'margin-left:auto;cursor:pointer;background:none';
  b.title = 'Switch between US and metric units';
  b.onclick = () => { Units.set(!Units.metric); unitsRefresh(); };
  document.querySelector('header').append(b); b.textContent = Units.metric ? '°C · km/h' : '°F · mph';
}

// ---- tabs
// Everyday tabs sit in the bar; the admin ones are in the More menu.
const TABS = [['globe','Views'],['insights','Insights'],['weather','Weather'],['charts','Charts'],['gallery','Gallery'],['forecast','Forecast']];
const MORE = [['aim','Aim'],['signal','Signal'],['skywarn','Skywarn'],['pi','Pi'],['control','Control'],['config','Config'],['images','Files'],['mail','Mail'],['notify','Notifications']];
TABS.forEach(([id, name]) => {
  const b = document.createElement('button');
  b.textContent = name; b.dataset.tab = id;
  b.onclick = () => show(id);
  $('#tabs').append(b);
});
const moreBtn = document.createElement('button');
moreBtn.textContent = 'More ▾'; moreBtn.id = 'more-btn';
$('#tabs').append(moreBtn);
const menu = document.createElement('div'); menu.className = 'menu'; menu.id = 'more-menu';
MORE.forEach(([id, name]) => {
  const b = document.createElement('button');
  b.textContent = name; b.dataset.tab = id;
  b.onclick = () => { menu.classList.remove('open'); show(id); };
  menu.append(b);
});
document.body.append(menu);
moreBtn.onclick = e => {
  e.stopPropagation();
  if (menu.classList.toggle('open')) {
    const r = moreBtn.getBoundingClientRect();
    menu.style.top = r.bottom + 4 + 'px';
    menu.style.left = Math.max(8, Math.min(r.left, innerWidth - menu.offsetWidth - 8)) + 'px';
  }
};
document.addEventListener('click', () => menu.classList.remove('open'));
function show(id) {
  document.querySelectorAll('section').forEach(s => s.classList.toggle('active', s.id === id));
  document.querySelectorAll('nav button[data-tab], .menu button').forEach(b => b.classList.toggle('active', b.dataset.tab === id));
  const inMore = MORE.find(([k]) => k === id);
  moreBtn.classList.toggle('active', !!inMore);
  moreBtn.textContent = (inMore ? inMore[1] : 'More') + ' ▾';
  try { localStorage.tab = id; } catch {}
  if (id === 'config') loadConfig();
  if (id === 'images') loadDir(curPath);
  if (id === 'gallery') loadGallery(true);
  if (id === 'globe') loadGlobe();
  if (id === 'mail') { loadMail(); loadShare(); }
  if (id === 'forecast') loadForecast();
  if (id === 'weather') loadWeather();
  if (id === 'skywarn') loadSkywarn();
  if (id === 'notify') loadNotify();
  if (id === 'aim') renderSky();
  if (id === 'charts') loadCharts();
  if (id === 'signal') loadSignal();
  if (id === 'pi') loadPi();
  if (id === 'insights') loadInsights();
}
let startTab = 'weather';     // always open on Weather; the tab you leave on is no longer restored

// ---- status
let status = {};
const fmtBytes = n => n > 1e9 ? (n/1e9).toFixed(1)+' GB' : n > 1e6 ? (n/1e6).toFixed(1)+' MB' : (n/1e3).toFixed(0)+' KB';
async function pollStatus() {
  try {
    status = await api('/api/status');
    for (const [n, el] of [['goesrecv','#p-recv'],['goesproc','#p-proc']]) {
      const s = status[n];
      $(el).textContent = `${n}: ${s.running ? (s.external ? 'on (ext)' : 'on') : 'off'}`;
      $(el).classList.toggle('on', s.running);
      const up = s.uptime == null ? '' : s.uptime >= 3600 ? ` · up ${Math.floor(s.uptime/3600)} h ${Math.floor(s.uptime%3600/60)} min`
        : s.uptime >= 60 ? ` · up ${Math.floor(s.uptime/60)} min` : ` · up ${Math.round(s.uptime)} s`;
      const st = $('#st-' + n);
      st.textContent = s.running ? `● Running${s.external ? ' (started outside the panel)' : ''}${up}` : '○ Stopped';
      st.className = 'pstate ' + (s.running ? 'on' : 'off');
      document.querySelector(`[data-act="${n}/start"]`).disabled = s.running;
      document.querySelector(`[data-act="${n}/stop"]`).disabled = !s.running;
      document.querySelector(`[data-act="${n}/restart"]`).disabled = !s.running;
    }
    if (status.panel) {
      const sec = Math.max(0, Date.now() / 1000 - status.panel.started), st = new Date(status.panel.started * 1000);
      $('#panel-up').textContent = 'Panel started ' + st.toLocaleString([], {weekday: 'short', hour: 'numeric', minute: '2-digit'}) + ' · up ' +
        (sec >= 86400 ? Math.floor(sec / 86400) + ' d ' + Math.floor(sec % 86400 / 3600) + ' h' : sec >= 3600 ? Math.floor(sec / 3600) + ' h ' + Math.floor(sec % 3600 / 60) + ' min' : sec >= 60 ? Math.floor(sec / 60) + ' min' : Math.round(sec) + ' s');
    }
    const run = status.goesrecv.running;
    const t = $('#aim-toggle');
    t.textContent = run ? 'Stop goesrecv' : 'Start goesrecv';
    t.className = 'b ' + (run ? 'stop' : 'go');
  } catch (e) {}
}
document.addEventListener('click', async e => {
  const a = e.target.dataset.act;
  if (!a) return;
  e.target.disabled = true;
  const r = await api('/api/' + a, {});
  $('#ctl-msg').textContent = `${a}: ${r.result || r.error}`;
  await pollStatus();
});
$('#webui-restart').onclick = async e => {
  if (!confirm('Restart the panel? The receiver stops for about a minute and then starts again by itself.')) return;
  const btn = e.target, msg = $('#ctl-msg');
  btn.disabled = true; msg.textContent = 'Restarting the panel…';
  const r = await api('/api/webui/restart', {});
  if (r.error) { msg.textContent = r.error; btn.disabled = false; return; }
  let sawDown = false;
  for (let i = 0; i < 60; i++) {                       // wait up to 2 minutes for it to go down and come back
    await new Promise(res => setTimeout(res, 2000));
    try {
      const x = await fetch('/api/status', {cache: 'no-store'});
      if (x.ok && sawDown) { location.reload(); return; }
      if (!x.ok) sawDown = true;
    } catch { sawDown = true; }
    msg.textContent = sawDown ? 'Waiting for the panel to come back…' : 'Restarting the panel…';
  }
  msg.textContent = 'The panel has not come back yet. Reload this page in a minute.'; btn.disabled = false;
};
$('#aim-toggle').onclick = async () => {
  const run = status.goesrecv && status.goesrecv.running;
  $('#aim-toggle').disabled = true;
  const r = await api('/api/goesrecv/' + (run ? 'stop' : 'start'), {});
  $('#aim-msg').textContent = r.result || r.error;
  $('#aim-toggle').disabled = false;
  pollStatus();
};

// ---- aim
const METRICS = [
  ['ok','Packets OK /s',''], ['drops','Dropped /s',''], ['vit','Viterbi err (avg)','lower = better'],
  ['rs','Reed-Solomon (sum)','0 = clean'], ['freq','Freq offset Hz','near 0 when locked'],
  ['omega','Clock omega','steady ≈ locked'], ['gain','AGC gain','auto, not the tuner setting']
];
$('#metrics').innerHTML = METRICS.map(([k,l,s]) =>
  `<div class="metric"><div class="l">${l}</div><div class="v" id="m-${k}">–</div><div class="s" id="ms-${k}">${s}</div></div>`).join('');
let hist = [], lastT = 0, best = {vit: null, ok: 0};
async function pollStats() {
  try {
    const r = await api('/api/stats?since=' + lastT);
    for (const p of r.points) { hist.push(p); lastT = Math.max(lastT, p.t); }
    hist = hist.slice(-120);
    render();
  } catch (e) {}
}
const f = (v, d=0) => v == null ? '–' : v.toFixed(d);
function render() {
  const last = hist[hist.length - 1];
  const age = last ? Date.now()/1000 - last.t : null;
  const live = last && age < 6 && status.goesrecv && status.goesrecv.running;
  const b = $('#banner');
  if (!live) {
    b.className = 'banner'; b.innerHTML = 'No data<small>Start goesrecv to begin</small>';
  } else if (last.ok > 0 && !last.drops) {
    b.className = 'banner good'; b.innerHTML = `DECODING<small>${f(last.ok)} packets/s, no drops</small>`;
  } else if (last.ok > 0) {
    b.className = 'banner warn'; b.innerHTML = `WEAK / INTERMITTENT<small>${f(last.ok)} ok, ${f(last.drops)} dropped per s – keep adjusting</small>`;
  } else {
    b.className = 'banner bad'; b.innerHTML = `NO PACKETS<small>Not decoding yet – keep sweeping the dish</small>`;
  }
  if (!last) return;
  for (const [k] of METRICS) $('#m-'+k).textContent = f(last[k], k === 'omega' ? 3 : k === 'gain' ? 0 : 0);
  if (live) {
    if (last.ok > 0 && (best.vit == null || last.vit < best.vit)) best.vit = last.vit;
    best.ok = Math.max(best.ok, last.ok);
    $('#ms-vit').textContent = best.vit == null ? 'lower = better' : `best this run: ${f(best.vit)}`;
    $('#ms-ok').textContent = `best this run: ${f(best.ok)}`;
    tone(last);
  } else tone(null);
  chart('#c-vit', [{d: hist.map(h => h.vit), c: 'var(--warn)'}]);
  chart('#c-pkt', [{d: hist.map(h => h.ok), c: 'var(--good)'}, {d: hist.map(h => h.drops), c: 'var(--bad)'}]);
  chart('#c-freq', [{d: hist.map(h => h.freq), c: 'var(--accent)'}], true);
}
function chart(sel, series, zero) {
  const cv = $(sel), dpr = devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext('2d'); g.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement);
  const all = series.flatMap(s => s.d).filter(v => v != null);
  if (!all.length) return;
  let lo = Math.min(...all, zero ? 0 : Infinity), hi = Math.max(...all, zero ? 0 : -Infinity);
  if (!zero) lo = Math.min(lo, 0);
  if (hi === lo) hi = lo + 1;
  const y = v => h - 14 - (v - lo) / (hi - lo) * (h - 24);
  g.strokeStyle = css.getPropertyValue('--line'); g.fillStyle = css.getPropertyValue('--muted'); g.font = '11px sans-serif';
  g.beginPath(); g.moveTo(0, y(lo)); g.lineTo(w, y(lo)); g.stroke();
  g.fillText(hi.toFixed(0), 2, 10); g.fillText(lo.toFixed(0), 2, h - 2);
  for (const s of series) {
    g.strokeStyle = css.getPropertyValue(s.c.slice(4, -1)); g.lineWidth = 2; g.beginPath();
    let started = false;
    s.d.forEach((v, i) => {
      if (v == null) { started = false; return; }
      const x = i / Math.max(119, s.d.length - 1) * w;
      started ? g.lineTo(x, y(v)) : g.moveTo(x, y(v)); started = true;
    });
    g.stroke();
  }
}

// ---- audio aiming aid: pitch rises as packets decode and viterbi errors fall
let actx, osc, gainNode, soundOn = false;
$('#sound').onclick = () => {
  soundOn = !soundOn;
  $('#sound').textContent = soundOn ? '🔊 Sound on' : '🔇 Sound off';
  if (soundOn && !actx) {
    actx = new (window.AudioContext || window.webkitAudioContext)();
    osc = actx.createOscillator(); gainNode = actx.createGain(); gainNode.gain.value = 0;
    osc.connect(gainNode).connect(actx.destination); osc.start();
  }
  if (actx) actx.resume();
  if (!soundOn && gainNode) gainNode.gain.value = 0;
};
function tone(p) {
  if (!soundOn || !osc) return;
  if (!p) { gainNode.gain.value = 0; return; }
  const vits = hist.map(h => h.vit).filter(v => v != null);
  const lo = Math.min(...vits), hi = Math.max(...vits);
  const q = hi > lo ? 1 - (p.vit - lo) / (hi - lo) : 0.5;      // 0 worst .. 1 best (relative)
  const base = p.ok > 0 ? 500 : 200;                            // decoding = higher register
  osc.frequency.value = base + q * 600;
  gainNode.gain.value = 0.15;
}

$('#gain-up').onclick = () => bump(+1); $('#gain-dn').onclick = () => bump(-1);
async function showTunerGain() {
  const c = await api('/api/config');
  const g = c.fields && c.fields.gain;
  $('#gain-val').textContent = g == null ? '–' : String(g);
}
showTunerGain();
async function bump(d) {
  const cur = Number($('#gain-val').textContent);
  if (isNaN(cur)) return;
  $('#aim-msg').textContent = 'Applying gain…';
  const r = await api('/api/config/fields', {fields: {gain: cur + d}, restart: true});
  $('#aim-msg').textContent = r.result || r.error;
  showTunerGain();
  hist = []; lastT = Date.now() / 1000; best = {vit: null, ok: 0};
}

// ---- config
async function loadConfig() {
  const c = await api('/api/config');
  for (const [k, v] of Object.entries(c.fields)) { const el = $('#f-' + k); if (el && v != null) el.value = String(v); }
  $('#raw-goesrecv').value = c.goesrecv; $('#raw-goesproc').value = c.goesproc;
}
async function saveFields(restart) {
  const fields = {};
  for (const k of ['source','frequency','sample_rate','gain','device_index','bias_tee']) fields[k] = $('#f-' + k).value;
  const r = await api('/api/config/fields', {fields, restart});
  $('#cfg-msg').textContent = r.result || r.error;
  loadConfig();
}
$('#save-fields').onclick = () => saveFields(false);
$('#save-restart').onclick = () => saveFields(true);
document.querySelectorAll('[data-raw]').forEach(b => b.onclick = async () => {
  const w = b.dataset.raw;
  const r = await api('/api/config/raw', {which: w, text: $('#raw-' + w).value});
  $('#cfg-msg').textContent = r.result || r.error;
  alert(r.result || r.error);
});
$('#sdrtest').onclick = async () => {
  const out = $('#sdr-out'); out.style.display = 'block'; out.textContent = 'Checking…';
  const r = await api('/api/sdrtest', {});
  out.textContent = (r.usb.length ? r.usb.join('\n') : 'No RTL-SDR or Airspy found on USB.') +
    (r.rtl_test ? '\n\n' + r.rtl_test : '');
};

// ---- views (regional crops + time-lapse)
let globeFrames = [], globeTimer = null, globeLoadId = 0;
const gUrl = x => '/file/' + x.path.split('/').map(encodeURIComponent).join('/');
const fUrl = x => '/thumb/' + x.path.split('/').map(encodeURIComponent).join('/') + '?w=720';   // light frame for playback
function showGlobe(i) {
  const x = globeFrames[i]; if (!x) return;
  $('#globe-slider').value = i;
  $('#globe-img').src = fUrl(x);
  updateCmp();
  const d = new Date(x.t * 1000);
  $('#globe-time').textContent = d.toLocaleDateString([], {month: 'short', day: 'numeric'}) + ' ' +
    d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'});
}
// ---- compare two views of the same time with a slider
let cmpOn = false, cmpFrames = [], cmpLoadId = 0;
const CMP_PAIR = {northeast: 'ct_northeast', eastus: 'wv_eastus', gulf: 'ct_gulf', namer: 'wv_namer', globe: 'wv_globe', stratford: 'ct_stratford', ct_stratford: 'stratford', fog_stratford: 'stratford', nyc: 'northeast',
  samer: 'globe', ct_northeast: 'northeast', ct_eastus: 'eastus', ct_gulf: 'gulf', ct_namer: 'namer', ct_globe: 'globe',
  wv_eastus: 'eastus', wv_namer: 'namer', wv_globe: 'globe', wv_gulf: 'gulf', fog_northeast: 'northeast', fog_eastus: 'eastus',
  fog_nyc: 'nyc', fog_namer: 'namer', fog_gulf: 'gulf', fog_samer: 'samer', storm: 'northeast', sst_stratford: 'stratford', sst_eastus: 'eastus', rad_stratford: 'ct_stratford', rad_northeast: 'ct_northeast'};
function cmpSplit() {
  const v = +$('#cmp-pos').value;
  $('#cmp-img').style.clipPath = `inset(0 0 0 ${v}%)`;
  const l = $('#cmp-line'); l.style.left = `calc(${v}% - 1px)`;
}
function updateCmp() {
  if (!cmpOn) return;
  const x = globeFrames[+$('#globe-slider').value], im = $('#cmp-img');
  if (!x || !cmpFrames.length) { im.style.display = 'none'; $('#cmp-line').style.display = 'none'; return; }
  let best = cmpFrames[0];
  cmpFrames.forEach(f => { if (Math.abs(f.t - x.t) < Math.abs(best.t - x.t)) best = f; });
  const near = Math.abs(best.t - x.t) <= 3600;      // only pair frames from about the same time
  im.style.display = near ? '' : 'none'; $('#cmp-line').style.display = near ? '' : 'none';
  if (near) im.src = fUrl(best);
  $('#globe-msg').textContent = near ? 'Left: ' + $('#globe-region').selectedOptions[0].textContent + '. Right: ' + $('#cmp-region').selectedOptions[0].textContent + '. Drag the slider to compare.'
    : 'No frame from the other view near this time.';
  cmpSplit();
}
async function loadCmp() {
  const id = ++cmpLoadId;
  const d = await api('/api/gallery?region=' + $('#cmp-region').value + '&limit=200');
  if (id !== cmpLoadId) return;
  cmpFrames = d.items || []; updateCmp();
}
$('#globe-cmp').onclick = () => {
  cmpOn = !cmpOn;
  $('#cmp-row').style.display = cmpOn ? '' : 'none';
  $('#globe-cmp').classList.toggle('ghost', !cmpOn);
  if (cmpOn) {
    const sel = $('#cmp-region');
    if (!sel.options.length) [...$('#globe-region').options].forEach(o => sel.append(new Option(o.textContent, o.value)));
    sel.value = CMP_PAIR[$('#globe-region').value] || 'globe';
    loadCmp();
  } else { $('#cmp-img').style.display = 'none'; $('#cmp-line').style.display = 'none'; loadGlobe(); }
};
$('#cmp-region').onchange = loadCmp;
$('#cmp-pos').oninput = cmpSplit;
function stopGlobe() { clearInterval(globeTimer); globeTimer = null; $('#globe-play').textContent = 'Play'; }
async function loadGlobe() {
  stopGlobe();
  const id = ++globeLoadId;
  const d = await api('/api/gallery?region=' + $('#globe-region').value + '&limit=200');
  if (id !== globeLoadId) return;
  const hrs = +$('#globe-range').value, cut = hrs ? Date.now() / 1000 - hrs * 3600 : 0;
  globeFrames = d.items.filter(x => x.t >= cut).reverse();   // oldest -> newest
  $('#globe-slider').max = Math.max(globeFrames.length - 1, 0);
  $('#globe-msg').textContent = globeFrames.length
    ? `${globeFrames.length} frame${globeFrames.length > 1 ? 's' : ''}. Tap the picture to zoom or save it.`
    : ($('#globe-region').value.startsWith('fog_')
        ? 'No fog images yet. They are only made when part of the region is in darkness and the infrared channels have arrived.'
        : 'No images for this view and time range yet. New ones are built every few minutes as full-disk scans arrive.');
  $('#globe-img').style.display = globeFrames.length ? '' : 'none';
  if (globeFrames.length) showGlobe(globeFrames.length - 1);
}
$('#globe-region').onchange = () => { if ($('#globe-region').value.startsWith('meso')) $('#globe-range').value = '1'; else if ($('#globe-range').value === '1') $('#globe-range').value = '24'; loadGlobe(); if (cmpOn) { $('#cmp-region').value = CMP_PAIR[$('#globe-region').value] || 'globe'; loadCmp(); } };
$('#globe-range').onchange = loadGlobe;
$('#globe-slider').oninput = e => { stopGlobe(); showGlobe(+e.target.value); };
$('#globe-img').onclick = () => { const x = globeFrames[+$('#globe-slider').value]; if (x) lightbox(gUrl(x)); };
$('#globe-play').onclick = async () => {
  if (globeTimer) return stopGlobe();
  if (globeFrames.length < 2) return;
  const id = globeLoadId, btn = $('#globe-play');
  btn.disabled = true;
  let n = 0;                                    // fetch every frame first so playback is smooth
  await Promise.all(globeFrames.map(x => new Promise(res => {
    const im = new Image();
    im.onload = im.onerror = () => { $('#globe-msg').textContent = `Loading ${++n} / ${globeFrames.length}…`; res(); };
    im.src = fUrl(x);
  })));
  btn.disabled = false;
  if (id !== globeLoadId) return;
  $('#globe-msg').textContent = `${globeFrames.length} frames. Tap the picture to zoom or save it.`;
  btn.textContent = 'Pause';
  let i = +$('#globe-slider').value >= globeFrames.length - 1 ? 0 : +$('#globe-slider').value;
  showGlobe(i);
  globeTimer = setInterval(() => { i = (i + 1) % globeFrames.length; showGlobe(i); }, 300);
};

// ---- signal history
function hchart(sel, pts, key, color, fixedLo) {
  const cv = $(sel), dpr = devicePixelRatio || 1;
  const w = cv.clientWidth, h = cv.clientHeight;
  cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext('2d'); g.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement);
  const vals = pts.map(p => p[key]).filter(v => v != null);
  g.font = '11px sans-serif';
  if (!vals.length) { g.fillStyle = css.getPropertyValue('--muted'); g.fillText('No data for this period yet', 8, 20); return; }
  let lo = fixedLo ?? Math.min(...vals), hi = Math.max(...vals);
  if (hi === lo) hi = lo + 1;
  const t0 = pts[0].t, t1 = Math.max(pts[pts.length - 1].t, t0 + 1), gap = Math.max((t1 - t0) / pts.length * 4, 300);
  const x = t => 34 + (t - t0) / (t1 - t0) * (w - 40), y = v => h - 16 - (v - lo) / (hi - lo) * (h - 28);
  g.strokeStyle = css.getPropertyValue('--line'); g.fillStyle = css.getPropertyValue('--muted');
  g.beginPath(); g.moveTo(34, y(lo)); g.lineTo(w, y(lo)); g.stroke();
  g.fillText(hi.toFixed(hi < 10 ? 1 : 0), 2, 12); g.fillText(lo.toFixed(lo < 10 && lo % 1 ? 1 : 0), 2, h - 4);
  const fmt = t => { const d = new Date(t * 1000); return (t1 - t0 > 86400 * 1.5 ? d.toLocaleDateString([], {month: 'short', day: 'numeric'}) + ' ' : '') + d.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'}); };
  g.fillText(fmt(t0), 34, h - 3); const lt = fmt(t1); g.fillText(lt, w - g.measureText(lt).width - 2, h - 3);
  g.strokeStyle = css.getPropertyValue(color); g.lineWidth = 2; g.beginPath();
  let prev = null;
  for (const p of pts) {
    if (p[key] == null) { prev = null; continue; }
    (prev != null && p.t - prev < gap) ? g.lineTo(x(p.t), y(p[key])) : g.moveTo(x(p.t), y(p[key]));
    prev = p.t;
  }
  g.stroke();
}
async function loadOutages() {
  const r = await api('/api/outages'), box = $('#outage-list'); box.innerHTML = '';
  if (r.error) { box.textContent = r.error; return; }
  if (!r.outages.length) { const p = document.createElement('p'); p.className = 'msg'; p.textContent = 'No outages recorded.'; box.append(p); return; }
  r.outages.forEach(o => {
    const d = document.createElement('div'); d.className = 'mrow'; d.style.display = 'block';
    const h = document.createElement('div'); h.style.fontWeight = '600';
    const a = new Date(o.start * 1000);
    h.textContent = a.toLocaleString([], {weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}) + ' · ' + (o.minutes >= 90 ? (o.minutes / 60).toFixed(1) + ' h' : o.minutes + ' min');
    const w = document.createElement('div'); w.className = 'msg'; w.style.margin = '2px 0 0'; w.textContent = o.why;
    d.append(h, w); box.append(d);
  });
}
async function loadSignal() {
  loadOutages();
  const d = await api('/api/history?hours=' + $('#sig-range').value);
  hchart('#h-vit', d.points, 'vit', '--warn'); hchart('#h-ok', d.points, 'ok', '--good', 0); hchart('#h-gain', d.points, 'gain', '--accent');
  const st = d.state || {};
  $('#sig-state').textContent = ({locked: '● Locked', lost: '● No signal', stopped: '○ goesrecv stopped'})[st.signal] || '';
  const sy = (status || {}).system;
  if (sy) {
    const used = sy.disk_total - sy.disk_free, f = used / sy.disk_total;
    $('#disk-fill').style.width = (f * 100).toFixed(0) + '%';
    $('#disk-fill').style.background = f > 0.9 ? 'var(--bad)' : f > 0.75 ? 'var(--warn)' : 'var(--accent)';
    $('#disk-txt').textContent = `${fmtBytes(used)} used of ${fmtBytes(sy.disk_total)} (${fmtBytes(sy.disk_free)} free)`;
  }
  const bits = [];
  if (st.last_restart) bits.push(`Last watchdog restart: ${st.last_restart.name}, ${new Date(st.last_restart.t * 1000).toLocaleString()} (${st.last_restart.why})`);
  if (st.last_alert) bits.push(`Last alert email: ${st.last_alert.subject}, ${new Date(st.last_alert.t * 1000).toLocaleString()}`);
  $('#alert-msg').textContent = bits.join(' · ');
}
$('#sig-range').onchange = loadSignal;
async function alertTest(kind, btn) {
  btn.disabled = true; $('#alert-msg').textContent = 'Working…';
  const r = await api('/api/alerts/test', {kind});
  $('#alert-msg').textContent = r.result || r.error;
  btn.disabled = false;
}
$('#alert-test-sig').onclick = e => alertTest('signal', e.target);
$('#alert-test-wx').onclick = e => alertTest('weather', e.target);

// ---- Pi stats
const piBar = (pct, color) => { const o = document.createElement('div'); o.style.cssText = 'height:12px;border-radius:6px;background:var(--line);overflow:hidden;flex:1';
  const i = document.createElement('div'); i.style.cssText = `height:100%;width:${Math.min(100, Math.max(0, pct)).toFixed(0)}%;background:${color}`; o.append(i); return o; };
const piColor = (v, warn, bad) => v >= bad ? 'var(--bad)' : v >= warn ? 'var(--warn)' : 'var(--accent)';
function piRow(label, pct, text, color, labelW) {
  const r = document.createElement('div'); r.style.cssText = 'display:flex;align-items:center;gap:10px;margin:6px 0;font-size:14px';
  const l = document.createElement('span'); l.style.cssText = `min-width:${labelW || 72}px;color:var(--muted)`; l.textContent = label;
  const t = document.createElement('span'); t.style.cssText = 'min-width:120px;text-align:right;font-variant-numeric:tabular-nums'; t.textContent = text;
  r.append(l, piBar(pct, color), t); return r;
}
function piTile(label, value, sub, color) {
  const d = document.createElement('div'); d.className = 'metric';
  d.innerHTML = '<div class="l"></div><div class="v"></div><div class="s"></div>';
  d.children[0].textContent = label; d.children[1].textContent = value; d.children[2].textContent = sub || '';
  if (color) d.children[1].style.color = color;
  return d;
}
const fmtRate = b => b > 1e6 ? (b / 1e6).toFixed(1) + ' MB/s' : (b / 1e3).toFixed(1) + ' KB/s';
const fmtUp = s => { const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return (d ? d + 'd ' : '') + h + 'h ' + m + 'm'; };
function piRender(live) {
  if (!live || !live.t) { $('#pi-tiles').textContent = 'Collecting the first sample… try again in a few seconds.'; return; }
  const m = live.mem, memPct = 100 * m.used / m.total, th = live.throttle;
  const bad = th && th.now.length;
  const tiles = $('#pi-tiles'); tiles.replaceChildren(
    piTile('CPU', live.cpu.toFixed(0) + '%', live.freq_mhz ? Math.round(live.freq_mhz) + ' MHz' : '', piColor(live.cpu, 70, 90).replace('var(--accent)', '')),
    piTile('Memory', memPct.toFixed(0) + '%', fmtBytes(m.used) + ' of ' + fmtBytes(m.total), piColor(memPct, 75, 90).replace('var(--accent)', '')),
    piTile('Temperature', live.temp == null ? '–' : live.temp.toFixed(0) + ' °C', live.temp == null ? '' : (live.temp * 9 / 5 + 32).toFixed(0) + ' °F', live.temp == null ? '' : piColor(live.temp, 65, 78).replace('var(--accent)', '')),
    piTile('Load', live.load[0].toFixed(2), `5m ${live.load[1].toFixed(2)} · 15m ${live.load[2].toFixed(2)}`),
    piTile('Uptime', fmtUp(live.uptime), live.procs + ' processes'),
    piTile('Updates', live.updates ? (live.updates.count ? live.updates.count + ' waiting' : 'Up to date') : '–',
      live.updates ? (live.updates.security ? live.updates.security + ' security. ' : '') + (live.updates.lists_age_h > 168 ? 'Package list is ' + Math.round(live.updates.lists_age_h / 24) + ' days old' : 'Per the last package list') : 'not available',
      live.updates && live.updates.security ? 'var(--warn)' : ''),
    piTile('Data drive', live.disk_outlook ? '+' + fmtBytes(live.disk_outlook.per_day) + ' / day' : 'Measuring…',
      live.disk_outlook ? fmtBytes(live.disk_outlook.used) + ' used' + (live.disk_outlook.days_to_cap ? ', about ' + Math.round(live.disk_outlook.days_to_cap) + ' days to the 900 GB cleanup limit' : '') : 'needs a day of samples'),
    piTile('SD card writes', live.sd_written == null ? '–' : fmtBytes(live.sd_written), live.sd_written == null || !live.uptime ? 'since boot' : 'since boot, about ' + fmtBytes(live.sd_written / live.uptime * 86400) + ' a day'),
    piTile('Power / throttle', th ? (bad ? 'Problem' : 'OK') : '–', th ? (bad ? th.now.join(', ') : th.past.length ? 'Earlier: ' + th.past.join(', ') : 'No issues since boot') : 'not available', bad ? 'var(--bad)' : th && !th.past.length ? 'var(--good)' : ''));
  $('#pi-cores').replaceChildren(...live.cores.map((c, i) => piRow('Core ' + i, c, c.toFixed(0) + '%', piColor(c, 70, 90))));
  const cache = Math.min(m.cached, m.total - m.used);
  $('#pi-mem').replaceChildren(
    piRow('RAM used', memPct, `${fmtBytes(m.used)} / ${fmtBytes(m.total)}`, piColor(memPct, 75, 90)),
    piRow('File cache', 100 * cache / m.total, fmtBytes(cache), 'var(--muted)'),
    piRow('Swap', m.swap_total ? 100 * m.swap_used / m.swap_total : 0, m.swap_total ? `${fmtBytes(m.swap_used)} / ${fmtBytes(m.swap_total)}` : 'none', piColor(m.swap_total ? 100 * m.swap_used / m.swap_total : 0, 25, 60)));
  $('#pi-disks').replaceChildren(...live.disks.map(d => piRow(d.mount, 100 * d.used / d.total,
    `${fmtBytes(d.used)} / ${fmtBytes(d.total)}`, piColor(100 * d.used / d.total, 75, 90), 110)));
  $('#pi-net').replaceChildren(...Object.entries(live.net).filter(([n, v]) => live.ips[n] || v.rx_total || v.tx_total).map(([n, v]) => {
    const r = document.createElement('div'); r.style.cssText = 'margin:6px 0;font-size:14px';
    r.textContent = `${n}${live.ips[n] ? ' (' + live.ips[n] + ')' : ''}: ↓ ${fmtRate(v.rx)}  ↑ ${fmtRate(v.tx)}  · total ↓ ${fmtBytes(v.rx_total)} ↑ ${fmtBytes(v.tx_total)}`;
    return r; }));
  const tb = document.createElement('table'); tb.style.cssText = 'width:100%;border-collapse:collapse;font-size:14px';
  tb.innerHTML = '<tr style="color:var(--muted);text-align:left"><th>Process</th><th>PID</th><th style="text-align:right">CPU</th><th style="text-align:right">Memory</th></tr>';
  tb.querySelectorAll('th').forEach(h => h.style.padding = '2px 0 2px 10px');
  for (const p of live.top) { const tr = tb.insertRow(); [p.name, p.pid, p.cpu.toFixed(1) + '%', fmtBytes(p.rss)].forEach((v, i) => { const c = tr.insertCell(); c.textContent = v; c.style.padding = '2px 0 2px 10px'; if (i > 1) c.style.textAlign = 'right'; }); }
  $('#pi-top').replaceChildren(tb);
}
function piSpecs(sp) {
  const rows = [['Model', sp.model], ['SoC', sp.soc], ['Board revision', sp.revision], ['CPU', `${sp.cpu} × ${sp.cores} (${sp.arch})`],
    ['CPU speed', sp.cpu_min_mhz ? `${sp.cpu_min_mhz} – ${sp.cpu_max_mhz} MHz` : ''], ['RAM', fmtBytes(sp.ram)], ['Swap', sp.swap ? fmtBytes(sp.swap) : 'none'],
    ['OS', sp.os], ['Kernel', sp.kernel], ['Firmware', sp.firmware], ['Boot disk', sp.root_device], ['Hostname', sp.hostname], ['Python', sp.python]];
  $('#pi-specs').replaceChildren(...rows.filter(r => r[1]).map(([k, v]) => { const tr = document.createElement('tr');
    const a = document.createElement('td'), b = document.createElement('td'); a.textContent = k; b.textContent = v;
    a.style.cssText = 'color:var(--muted);padding:4px 12px 4px 0;white-space:nowrap'; tr.append(a, b); return tr; }));
}
let piSpecsDone = false, piStorageAt = 0;
async function piLive() {
  try { const d = await api('/api/pi'); piRender(d.live); if (!piSpecsDone && d.specs) { piSpecs(d.specs); piSpecsDone = true; } } catch {}
}
async function piHistory() {
  try {
    const d = await api('/api/pi/history?hours=' + $('#pi-range').value);
    hchart('#ph-cpu', d.points, 'cpu', '--accent', 0); hchart('#ph-mem', d.points, 'mem', '--good', 0); hchart('#ph-temp', d.points, 'temp', '--warn');
    hchart('#ph-net', d.points.map(p => ({t: p.t, kb: p.rx == null ? null : (p.rx + p.tx) / 1000})), 'kb', '--accent', 0);
    $('#pi-hist-note').textContent = d.points.length ? '' : 'History starts after the first full minute';
  } catch {}
}
async function piData() {
  if (Date.now() - piStorageAt < 600000) return;
  piStorageAt = Date.now(); $('#pi-data').textContent = 'Satellite data: measuring…';
  try { const d = await api('/api/storage'); $('#pi-data').textContent = 'Satellite data (~/goes/data): ' + fmtBytes(d.bytes); } catch { $('#pi-data').textContent = ''; }
}
function loadPi() { piLive(); piHistory(); piData(); }
$('#pi-range').onchange = piHistory;
setInterval(() => { if ($('#pi').classList.contains('active') && !document.hidden) piLive(); }, 3000);
setInterval(() => { if ($('#pi').classList.contains('active') && !document.hidden) piHistory(); }, 60000);

// ---- time-lapse video (mp4, seekable) and GIF
$('#globe-vid').onclick = async e => {
  const b = e.target, hrs = +$('#globe-range').value || 72, view = $('#globe-region').value;
  b.disabled = true; $('#globe-msg').textContent = 'Building the video (about 10 seconds)…';
  const u = `/mp4/${view}-${hrs}h.mp4`;
  try {
    const r = await fetch(u, {headers: {Range: 'bytes=0-0'}});
    if (!r.ok && r.status !== 206) { $('#globe-msg').textContent = (await r.json().catch(() => ({}))).error || 'Could not build the video.'; }
    else { lightbox(u); $('#lb-mail').style.display = 'none'; $('#globe-msg').textContent = ''; }
  } catch { $('#globe-msg').textContent = 'Could not build the video.'; }
  b.disabled = false;
};
// ---- time-lapse GIF
$('#globe-gif').onclick = async e => {
  const b = e.target, hrs = +$('#globe-range').value || 72, view = $('#globe-region').value;
  b.disabled = true; $('#globe-msg').textContent = 'Building the animated GIF (can take up to a minute)…';
  const u = `/gif/${view}-${hrs}h.gif`;
  try {
    const r = await fetch(u);
    if (!r.ok) { $('#globe-msg').textContent = (await r.json().catch(() => ({}))).error || 'Could not build the GIF.'; }
    else { await r.blob(); lightbox(u); $('#lb-mail').style.display = 'none'; $('#globe-msg').textContent = ''; }
  } catch { $('#globe-msg').textContent = 'Could not build the GIF.'; }
  b.disabled = false;
};

// ---- gallery
let galOff = 0;
const GAL_N = 60;
async function loadGallery(reset) {
  if (reset) { galOff = 0; $('#gal').innerHTML = ''; }
  const reg = $('#gal-region').value;
  const d = await api(`/api/gallery?offset=${galOff}&limit=${GAL_N}&region=${encodeURIComponent(reg)}`);
  d.items.forEach(x => {
    if (reset && !reg && ![...$('#gal-region').options].some(o => o.value === x.region)) {
      $('#gal-region').add(new Option(x.region, x.region));
    }
    const dt = new Date(x.t * 1000);
    const u = '/file/' + x.path.split('/').map(encodeURIComponent).join('/');
    const el = document.createElement('div'); el.className = 'g';
    el.title = dt.toISOString().replace('T', ' ').slice(0, 19) + ' UTC\n' + x.name;
    el.innerHTML = `<img loading="lazy" src="/thumb/${u.slice(6)}"><div class="cap"><b>${dt.toLocaleDateString([], {month: 'short', day: 'numeric'})} ${dt.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}</b><span>${x.region}${x.chan ? " · ch " + x.chan : ""}</span></div>`;
    el.onclick = () => lightbox(u);
    $('#gal').append(el);
  });
  galOff += d.items.length;
  $('#gal-count').textContent = d.total ? `${galOff} of ${d.total} images` : 'No images yet';
  $('#gal-more').style.display = galOff < d.total ? '' : 'none';
}
$('#gal-more').onclick = () => loadGallery(false);
$('#gal-region').onchange = () => loadGallery(true);

// ---- images
let curPath = '';
async function loadDir(p) {
  curPath = p;
  const d = await api('/api/files?path=' + encodeURIComponent(p));
  const parts = p ? p.split('/') : [];
  $('#crumbs').innerHTML = '<a data-p="">data</a>' + parts.map((s, i) =>
    ` / <a data-p="${parts.slice(0, i + 1).join('/')}">${s}</a>`).join('');
  $('#crumbs').querySelectorAll('a').forEach(a => a.onclick = () => loadDir(a.dataset.p));
  $('#dirs').innerHTML = '';
  d.dirs.forEach(x => {
    const el = document.createElement('div'); el.className = 'dir';
    el.innerHTML = `<span>📁 ${x.name}</span><span class="m">${new Date(x.mtime*1000).toLocaleString()}</span>`;
    el.onclick = () => loadDir(x.path); $('#dirs').append(el);
  });
  $('#files').innerHTML = '';
  d.files.slice(0, 200).forEach(x => {
    const el = document.createElement('div'); el.className = 'file';
    const u = '/file/' + x.path.split('/').map(encodeURIComponent).join('/');
    el.innerHTML = (x.image ? `<img loading="lazy" src="/thumb/${u.slice(6)}">` : '') +
      `<div>${x.name}<div class="m">${fmtBytes(x.size)} · ${new Date(x.mtime*1000).toLocaleTimeString()}</div></div>`;
    el.onclick = () => x.image ? lightbox(u) : textViewer(u);
    $('#files').append(el);
  });
  if (d.files.length > 200) $('#files').insertAdjacentHTML('beforeend', `<p class="msg">Showing 200 of ${d.files.length}.</p>`);
  $('#zip').href = '/zip?path=' + encodeURIComponent(p);
  api('/api/storage').then(s => $('#storage').textContent = 'Total stored: ' + fmtBytes(s.bytes));
}
let lbUrl = '';
function lightbox(u) {
  lbUrl = u;
  const vid = u.endsWith('.mp4');
  $('#lb-vid').pause(); $('#lb-vid').style.display = vid ? '' : 'none';
  if (vid) { $('#lb-vid').src = u; $('#lb-vid').play().catch(() => {}); }
  $('#lb-txt').style.display = 'none'; $('#lb-wrap').style.display = ''; $('#lb-hint').style.display = '';
  $('#lb-form').style.display = 'none'; $('#lb-mail').style.display = ''; $('#lb-save').style.display = '';
  $('#lb-img').classList.remove('zoom'); $('#lb-img').src = vid ? '' : u;
  if (vid) { $('#lb-wrap').style.display = 'none'; $('#lb-hint').style.display = 'none'; }
  $('#lightbox').style.display = 'flex';
}
async function textViewer(u) {
  lbUrl = u;
  $('#lb-form').style.display = 'none'; $('#lb-mail').style.display = 'none';
  $('#lb-wrap').style.display = 'none'; $('#lb-hint').style.display = 'none';
  $('#lb-txt').style.display = 'block'; $('#lb-txt').textContent = 'Loading…';
  $('#lightbox').style.display = 'flex';
  try { $('#lb-txt').textContent = await (await fetch(u)).text(); }
  catch { $('#lb-txt').textContent = 'Could not load file.'; }
}
$('#lb-img').onclick = () => $('#lb-img').classList.toggle('zoom');
$('#lb-save').onclick = async () => {
  const b = $('#lb-save'); b.disabled = true;
  try {
    const blob = await (await fetch(lbUrl)).blob();
    const name = decodeURIComponent(lbUrl.split('/').pop());
    const f = new File([blob], name, {type: blob.type});
    if (navigator.canShare && navigator.canShare({files: [f]})) await navigator.share({files: [f]});
    else $('#lb-hint').textContent = 'Sharing needs HTTPS (open the ts.net address). Or touch and hold the image to save it.';
  } catch (e) { /* share sheet dismissed */ }
  b.disabled = false;
};
// picture by email, from the viewer
const lbRel = () => decodeURIComponent(lbUrl.replace(/^\/file\//, ''));
function lbMailClose() {
  $('#lb-form').style.display = 'none'; $('#lb-wrap').style.display = ''; $('#lb-hint').style.display = '';
}
$('#lb-mail').onclick = async () => {
  $('#lbm-msg').textContent = ''; $('#lbm-cap').textContent = 'Loading…';
  $('#lb-wrap').style.display = 'none'; $('#lb-hint').style.display = 'none'; $('#lb-form').style.display = 'block';
  const [d, m] = await Promise.all([api('/api/mail/describe?path=' + encodeURIComponent(lbRel())), api('/api/mail')]);
  if (d.error) { $('#lbm-cap').textContent = d.error; return; }
  $('#lbm-cap').textContent = d.label + ' · ' + d.when;
  $('#lbm-list').innerHTML = '';
  (m.recipients || []).forEach(a => {
    const l = document.createElement('label'), c = document.createElement('input');
    c.type = 'checkbox'; c.checked = true; c.value = a;
    l.append(c, a); $('#lbm-list').append(l);
  });
  $('#lbm-send').disabled = false;
};
$('#lbm-cancel').onclick = lbMailClose;
$('#lbm-send').onclick = async () => {
  const to = [...$('#lbm-list').querySelectorAll('input:checked')].map(c => c.value);
  const other = $('#lbm-other').value.trim(); if (other) to.push(other);
  if (!to.length) { $('#lbm-msg').textContent = 'Choose at least one recipient.'; return; }
  $('#lbm-send').disabled = true; $('#lbm-msg').textContent = 'Sending…';
  const r = await api('/api/mail/image', {path: lbRel(), to, note: $('#lbm-note').value});
  if (r.error) { $('#lbm-msg').textContent = r.error; $('#lbm-send').disabled = false; return; }
  $('#lbm-msg').textContent = r.result; $('#lbm-other').value = ''; $('#lbm-note').value = '';
  setTimeout(lbMailClose, 1400);
};
$('#lb-close').onclick = () => { $('#lb-vid').pause(); $('#lightbox').style.display = 'none'; };

// ---- mail
const mailNote = t => { $('#m-msg').textContent = t || ''; };
function renderMail(d) {
  $('#m-from').innerHTML = '';
  $('#m-from').append('Sends from ', Object.assign(document.createElement('b'), {textContent: d.sender}),
    d.password_ok ? '' : '  -  password file missing, nothing will send');
  $('#m-next').textContent = 'Next email: ' + d.next_send + (d.last_sent ? '  (last sent ' + d.last_sent + ')' : '  (none sent automatically yet)');
  if (document.activeElement !== $('#m-time')) $('#m-time').value = d.send_time;
  $('#m-list').innerHTML = '';
  d.recipients.forEach(addr => {
    const row = document.createElement('div'); row.className = 'mrow';
    const a = document.createElement('span'); a.className = 'addr'; a.textContent = addr;
    const t = document.createElement('button'); t.className = 'b ghost'; t.textContent = 'Send test';
    t.onclick = async () => {
      t.disabled = true; mailNote('Sending test to ' + addr + '…');
      const r = await api('/api/mail/test', {to: addr});
      mailNote(r.result || r.error); t.disabled = false;
    };
    const x = document.createElement('button'); x.className = 'b stop'; x.textContent = 'Remove';
    x.disabled = d.recipients.length < 2;
    x.onclick = async () => {
      if (!confirm('Remove ' + addr + ' from the list?')) return;
      const r = await api('/api/mail/recipients', {remove: addr});
      if (r.error) mailNote(r.error); else { renderMail(r); mailNote('Removed ' + addr); }
    };
    row.append(a, t, x); $('#m-list').append(row);
  });
  $('#m-add-btn').disabled = d.recipients.length >= d.max;
}
async function loadMail() { mailNote(''); try { renderMail(await api('/api/mail')); } catch (e) { mailNote('Could not load mail settings.'); } }
$('#m-add-btn').onclick = async () => {
  const v = $('#m-add').value.trim(); if (!v) return;
  const r = await api('/api/mail/recipients', {add: v});
  if (r.error) return mailNote(r.error);
  $('#m-add').value = ''; renderMail(r); mailNote('Added ' + v + '. Use "Send test" to check it arrives.');
};
$('#m-add').onkeydown = e => { if (e.key === 'Enter') $('#m-add-btn').click(); };
$('#m-time-save').onclick = async () => {
  const r = await api('/api/mail/time', {send_time: $('#m-time').value});
  if (r.error) return mailNote(r.error);
  renderMail(r); mailNote('Send time saved.');
};

// ---- share links, push, forecast
const shNote = t => { $('#sh-msg').textContent = t || ''; };
let shData = null;
function renderShare(d) {
  shData = d;
  $('#sh-info').textContent = d.public_url ? 'Public address: ' + d.public_url : 'No public_url in mail.conf yet - links will not work from outside.';
  $('#pt-topic').value = d.topic || '';
  $('#sh-list').innerHTML = '';
  d.links.forEach(l => {
    const url = d.public_url + l.path;
    const row = document.createElement('div'); row.className = 'mrow';
    const a = document.createElement('span'); a.className = 'addr';
    const b = document.createElement('b'); b.textContent = l.name;
    const sub = document.createElement('div'); sub.className = 'msg';
    sub.textContent = (l.email ? l.email + ' · ' : '') + l.total + ' opens, ' + l.week + ' in 7 days' +
      (l.last ? ', last ' + new Date(l.last * 1000).toLocaleString() : ', never opened');
    a.append(b, sub);
    const cp = document.createElement('button'); cp.className = 'b ghost'; cp.textContent = 'Copy link';
    cp.onclick = async () => {
      try { await navigator.clipboard.writeText(url); shNote('Copied the link for ' + l.name + '.'); }
      catch { prompt('Copy this link:', url); }
    };
    const rn = document.createElement('button'); rn.className = 'b ghost'; rn.textContent = 'New link';
    rn.onclick = async () => {
      if (!confirm('Cancel the old link for ' + l.name + ' and make a new one?')) return;
      const r = await api('/api/share/rotate', {id: l.id});
      if (r.error) shNote(r.error); else { renderShare(r); shNote('New link made for ' + l.name + '. The old one no longer works.'); }
    };
    const x = document.createElement('button'); x.className = 'b stop'; x.textContent = 'Remove';
    x.disabled = d.links.length < 2;
    x.onclick = async () => {
      if (!confirm('Remove the link for ' + l.name + '? It will stop working.')) return;
      const r = await api('/api/share/revoke', {id: l.id});
      if (r.error) shNote(r.error); else { renderShare(r); shNote('Removed.'); }
    };
    row.append(a, cp, rn, x); $('#sh-list').append(row);
  });
}
async function loadShare() { shNote(''); try { renderShare(await api('/api/share')); } catch (e) { shNote('Could not load share links.'); } }
$('#sh-add').onclick = async () => {
  const r = await api('/api/share/add', {name: $('#sh-name').value, email: $('#sh-email').value});
  if (r.error) return shNote(r.error);
  $('#sh-name').value = ''; $('#sh-email').value = ''; renderShare(r); shNote('Link added. Use "Copy link" to send it.');
};
const ptNote = t => { $('#pt-msg').textContent = t || ''; };
$('#pt-gen').onclick = () => {
  const a = new Uint8Array(8); crypto.getRandomValues(a);
  $('#pt-topic').value = 'goes-' + [...a].map(n => (n % 36).toString(36)).join('');
};
$('#pt-save').onclick = async () => {
  const r = await api('/api/push/topic', {topic: $('#pt-topic').value});
  if (r.error) return ptNote(r.error);
  renderShare(r); ptNote(r.topic ? 'Saved. Subscribe to "' + r.topic + '" in the ntfy app, then send a test.' : 'Push turned off.');
};
$('#pt-test').onclick = e => alertTest('push', e.target).then(() => { ptNote($('#alert-msg').textContent); });
async function loadForecast() {
  $('#fc-msg').textContent = 'Loading…';
  const r = await api('/api/forecast');
  const out = $('#fc-out'); out.innerHTML = '';
  $('#fc-msg').textContent = r.offices.length
    ? 'National Weather Service forecast discussions as received over the satellite. The offices come from afd_offices in mail.conf.'
    : 'No forecast discussions have arrived in the last 48 hours.';
  r.offices.forEach(o => {
    const h = document.createElement('h2'); h.textContent = o.office; out.append(h);
    const i = document.createElement('div'); i.className = 'msg';
    i.textContent = o.issued + (o.age_h > 12 ? '  ·  ' + Math.round(o.age_h) + ' h old' : ''); out.append(i);
    o.sections.forEach(s => {
      const t = document.createElement('div'); t.style.cssText = 'font-weight:600;margin-top:12px'; t.textContent = s.head; out.append(t);
      s.paras.forEach(p => { const e = document.createElement('p'); e.style.margin = '4px 0'; e.textContent = p; out.append(e); });
    });
  });
}

// ---- small multi-line time chart (forecast and history charts)
function lineChart(sel, series, o = {}) {
  const cv = $(sel), dpr = devicePixelRatio || 1, w = cv.clientWidth, h = cv.clientHeight;
  if (!w) return;
  cv.width = w * dpr; cv.height = h * dpr;
  const g = cv.getContext('2d'); g.scale(dpr, dpr);
  const css = getComputedStyle(document.documentElement), col = c => css.getPropertyValue(c).trim() || c;
  const all = series.flatMap(s => s.pts.filter(p => p[1] != null));
  g.font = '11px sans-serif'; g.fillStyle = col('--muted');
  if (!all.length) { g.fillText('No data yet', 8, 20); return; }
  const t0 = Math.min(...all.map(p => p[0])), t1 = Math.max(...all.map(p => p[0]), t0 + 1);
  let lo = o.lo ?? Math.min(...all.map(p => p[1])), hi = o.hi ?? Math.max(...all.map(p => p[1]));
  if (o.lo == null) lo = Math.floor(lo - (hi - lo) * 0.08 - 0.001); if (o.hi == null) hi = Math.ceil(hi + 0.001);
  if (hi === lo) hi = lo + 1;
  const L = 36, T = 16, B = 16, x = t => L + (t - t0) / (t1 - t0) * (w - L - 6), y = v => h - B - (v - lo) / (hi - lo) * (h - B - T);
  g.strokeStyle = col('--line'); g.lineWidth = 1;
  for (let i = 0; i <= 2; i++) { const v = lo + (hi - lo) * i / 2; g.beginPath(); g.moveTo(L, y(v)); g.lineTo(w, y(v)); g.stroke();
    g.fillStyle = col('--muted'); g.fillText((o.dec ? v.toFixed(o.dec) : Math.round(v)) + '', 2, y(v) + 4); }
  const span = t1 - t0, fmt = t => { const d = new Date(t * 1000); return (span > 86400 * 1.2 ? d.toLocaleDateString([], {weekday: 'short'}) + ' ' : '') + d.toLocaleTimeString([], {hour: 'numeric'}); };
  g.fillStyle = col('--muted'); g.fillText(fmt(t0), L, h - 3); const lt = fmt(t1); g.fillText(lt, w - g.measureText(lt).width - 4, h - 3);
  const now = Date.now() / 1000;
  if (o.now && now > t0 && now < t1) { g.strokeStyle = col('--muted'); g.setLineDash([3, 3]); g.beginPath(); g.moveTo(x(now), T); g.lineTo(x(now), h - B); g.stroke(); g.setLineDash([]); }
  let lx = L;
  series.forEach(s => {
    g.strokeStyle = col(s.color); g.fillStyle = col(s.color); g.lineWidth = 2; g.beginPath();
    let prev = null;
    s.pts.forEach(p => { if (p[1] == null) { prev = null; return; } prev == null || p[0] - prev > (o.gap || 1e9) ? g.moveTo(x(p[0]), y(p[1])) : g.lineTo(x(p[0]), y(p[1])); prev = p[0]; });
    g.stroke(); g.fillText(s.label, lx, 11); lx += g.measureText(s.label).width + 14;
  });
}
const wxHr = t => new Date(t * 1000).toLocaleTimeString([], {hour: 'numeric'});
async function loadPointForecast() {
  const r = await api('/api/pointforecast');
  if (r.error || !r.points || !r.points.length) { $('#pf-card').style.display = 'none'; return; }
  $('#pf-card').style.display = '';
  $('#pf-title').textContent = 'Forecast for ' + r.name.replace(/-/g, ', ');
  $('#pf-note').textContent = 'National Weather Service point forecast, ' + r.issued + '. Every 3 hours for the first days, then every 6.';
  const pts = r.points;
  lineChart('#pf-temp', [{label: 'Temp ' + Units.tu(), color: '--warn', pts: pts.map(p => [p.t, Units.t(p.temp)])}, {label: 'Dew point', color: '--accent', pts: pts.map(p => [p.t, Units.t(p.dew)])}], {now: true});
  lineChart('#pf-wind', [{label: 'Wind ' + Units.su(), color: '--good', pts: pts.map(p => [p.t, Units.wmph(p.wind_mph)])}], {now: true, lo: 0});
  const tb = $('#pf-table'); tb.innerHTML = '';
  const day = new Intl.DateTimeFormat([], {weekday: 'short'});
  pts.filter(p => p.step_h === 3).slice(0, 10).forEach(p => {
    const d = document.createElement('div'); d.style.cssText = 'display:flex;gap:10px;padding:4px 0;border-top:1px solid var(--line);font-size:14px';
    const cells = [day.format(new Date(p.t * 1000)) + ' ' + wxHr(p.t), Units.t(p.temp) + '°', p.sky || '', (p.wind_dir || '') + ' ' + (Units.wmph(p.wind_mph) ?? '') + ' ' + Units.su(), p.pop != null ? 'Rain ' + p.pop + '%' : ''];
    cells.forEach((c, i) => { const s = document.createElement('span'); s.textContent = c; s.style.flex = i === 2 ? '1.3' : '1'; d.append(s); });
    tb.append(d);
  });
}
async function loadWeatherHistory() {
  const r = await api('/api/weather/history?hours=' + $('#wh-hours').value);
  if (r.error) return;
  const p = r.points, T = (k, f = v => v) => p.map(x => [x.t, f(x[k])]), gap = 3 * 3600;
  $('#wh-tend').textContent = r.tendency ? `Pressure ${r.tendency.text} (${r.tendency.change_inhg > 0 ? '+' : ''}${Units.metric ? (r.tendency.change_inhg * 33.8639).toFixed(1) : r.tendency.change_inhg.toFixed(2)} ${Units.pu()} in 3 hours)` : '';
  lineChart('#wh-temp', [{label: 'Temp ' + Units.tu(), color: '--warn', pts: T('temp', Units.t)}, {label: 'Dew point', color: '--accent', pts: T('dew', Units.t)}], {gap});
  lineChart('#wh-press', [{label: 'Pressure ' + Units.pu(), color: '--accent', pts: T('press', Units.p)}], {gap, dec: Units.metric ? 0 : 2});
  lineChart('#wh-wind', [{label: 'Wind ' + Units.su(), color: '--good', pts: T('wind', Units.wkt)}, {label: 'Gust', color: '--bad', pts: T('gust', Units.wkt)}], {gap, lo: 0});
}
$('#wh-hours').onchange = loadWeatherHistory;

// ---- weather products (EMWIN text bulletins)
let wxOffset = 0, wxReady = false, wxMine = [];
const wxFmt = t => new Date(t * 1000).toLocaleString([], {month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'});
async function loadWeather() {
  if (!wxReady) {
    const s = await api('/api/products/summary');
    if (s.error) { $('#wx-msg').textContent = s.error; return; }
    wxMine = s.mine;
    s.categories.forEach(c => { const o = document.createElement('option'); o.value = c.code; o.textContent = `${c.label} (${c.n})`; $('#wx-cat').append(o); });
    s.offices.forEach(c => { const o = document.createElement('option'); o.value = c; o.textContent = c; $('#wx-office').append(o); });
    wxReady = true;
    $('#wx-cat').value = 'AFD'; $('#wx-office').value = wxMine[0] || '';
    wxSearch(true, 5);
  }
  loadWxAlerts(); loadLocal(); loadWinter(); loadTropical(); loadTides(); loadPointForecast(); loadWeatherHistory();
}
async function loadWxAlerts() {
  const r = await api('/api/alerts/active');
  const box = $('#wx-alerts'); box.innerHTML = '';
  if (r.error) return;
  const loc = r.local || [], wide = r.alerts.length;
  const c = document.createElement('div');
  c.className = 'banner ' + (loc.length ? (loc.some(a => a.level === 'W') ? 'bad' : 'warn') : 'good');
  c.textContent = loc.length ? loc.length + ' alert' + (loc.length > 1 ? 's' : '') + ' for Stratford' : 'No alerts for Stratford';
  const sm = document.createElement('small');
  sm.textContent = (loc.length ? loc.map(a => `${a.name}${a.ends ? ' until ' + wxFmt(a.ends) : ''}`).join(' · ') + '. ' : 'Nothing in force for Fairfield County or the western Sound. ')
    + (wide ? wide + ' more in the wider region (' + wxMine.join(', ') + ').' : '');
  c.append(sm); box.append(c);
}
function locBlock(title, rows, note) {
  const d = document.createElement('div'); d.style.marginTop = '14px';
  const h = document.createElement('div'); h.style.fontWeight = '700'; h.textContent = title; d.append(h);
  if (note) { const n = document.createElement('div'); n.className = 'msg'; n.style.margin = '2px 0 6px'; n.textContent = note; d.append(n); }
  rows.forEach(([a, b]) => {
    const r = document.createElement('div'); r.style.cssText = 'margin:4px 0';
    if (b === undefined) r.textContent = a; else { const s = document.createElement('b'); s.textContent = a + ' '; r.append(s, document.createTextNode(b)); }
    d.append(r);
  });
  return d;
}
async function loadLocal() {
  const r = await api('/api/local'); const body = $('#loc-body'); body.innerHTML = '';
  if (r.error) { $('#loc-now').textContent = r.error; return; }
  $('#loc-title').textContent = r.place;
  const n = r.now;
  $('#loc-now').textContent = n ? (n.temp_f != null ? Units.t(n.temp_f) + Units.tu() + '  ' : '') + (n.wx.length ? n.wx[0] : (n.sky.length ? n.sky[n.sky.length - 1].cover : '')) : 'No recent observation';
  $('#loc-now2').textContent = n ? 'Bridgeport airport (KBDR), ' + wxFmt(n.t) + ' · ' + Units.text(n.summary) + (n.pressure_inhg ? ' · ' + (Units.metric ? Units.p(n.pressure_inhg).toFixed(1) : n.pressure_inhg.toFixed(2)) + ' ' + Units.pu() : '') : '';
  if (r.forecast) body.append(locBlock(r.forecast.name + ' forecast', r.forecast.periods.slice(0, 6).map(x => x.map(Units.text)), r.forecast.issued));
  if (r.marine) body.append(locBlock('Long Island Sound (west of New Haven)', (r.marine.headlines || []).map(h => [Units.text(h)]).concat(r.marine.periods.slice(0, 5).map(x => x.map(Units.text))), r.marine.issued));
  if (r.taf) { const d = locBlock('Airport forecast (TAF) for Bridgeport', [], wxFmt(r.taf.t)); const pre = document.createElement('pre'); pre.textContent = r.taf.text; d.append(pre); body.append(d); }
  const sp = r.space;
  if (sp && (sp.alert || sp.sunspot != null)) {
    const rows = [];
    if (sp.alert) rows.push([sp.alert.text + ',', wxFmt(sp.alert.t) + (sp.alert.aurora ? '. ' + sp.alert.aurora : '')]);
    if (sp.sunspot != null) rows.push(['Sunspot number', sp.sunspot + ' · 10.7 cm flux ' + sp.flux]);
    body.append(locBlock('Space weather', rows, 'Aurora is only likely in Connecticut at a K-index of 7 or more.'));
  }
}
async function loadTropical() {
  const card = $('#trop-card'), r = await api('/api/tropical');
  if (!r || !r.headline || (!r.season && !r.storms.length)) { card.style.display = 'none'; return; }
  card.style.display = '';
  const head = $('#trop-head'); head.textContent = r.headline;
  head.style.color = `var(--${r.storms.length ? 'bad' : (r.outlook && r.outlook.areas.length ? 'warn' : 'good')})`;
  const body = $('#trop-body'); body.innerHTML = '';
  r.storms.forEach(s => body.append(locBlock(s.title + ' · advisory ' + s.number, Object.entries(s.keys).map(([k, v]) => [k, Units.text(v)]), wxFmt(s.t))));
  ((r.outlook || {}).areas || []).forEach(a => body.append(locBlock('Area to watch', [[a.text, '']].concat(
    a.two_day != null ? [['Chance in 2 days', a.two_day + '%']] : [], a.seven_day != null ? [['Chance in 7 days', a.seven_day + '%']] : []))));
  if (r.outlook) { const m = document.createElement('div'); m.className = 'msg'; m.textContent = 'National Hurricane Center outlook issued ' + r.outlook.issued + '. Season: June 1 to November 30; this card hides itself outside it unless a storm is active.'; body.append(m); }
}
async function loadWinter() {
  const card = $('#winter-card'), r = await api('/api/winter');
  if (!r || !r.stage || (!r.season && r.stage !== 'alert')) { card.style.display = 'none'; return; }
  card.style.display = '';
  const lvl = {alert: 'bad', likely: 'warn', watch: 'warn', none: 'good'}[r.stage];
  const head = $('#winter-head'); head.textContent = r.headline;
  head.style.color = `var(--${lvl})`;
  const body = $('#winter-body'); body.innerHTML = '';
  const rows = [];
  r.alerts.forEach(a => rows.push([a.name, a.ends ? 'until ' + wxFmt(a.ends) : '']));
  const inch = v => Units.metric ? Math.round(v * 2.54) + ' cm' : v + ' in';
  r.snow.forEach(s => rows.push(['Snow ' + new Date(s.t * 1000).toLocaleString([], {weekday: 'short', hour: 'numeric'}) + ' (12 h)', s.lo === s.hi ? inch(s.hi) : inch(s.lo) + ' to ' + inch(s.hi)]));
  if (rows.length) body.append(locBlock('Right now', rows));
  if (r.signals.length) body.append(locBlock('What forecasters are saying', r.signals.map(x => [x.src + (x.issued ? ' · ' + x.issued : ''), Units.text(x.text)]),
    'From the New York office, which covers all of Connecticut’s coast, Long Island and northern New Jersey.'));
  if (r.stage === 'none') { const m = document.createElement('div'); m.className = 'msg'; m.textContent = 'Checks the NWS alerts, the Bridgeport snow forecast and the forecasters’ discussion every 5 minutes. You get a phone push when it changes.'; body.append(m); }
}
async function loadTides() {
  const card = $('#tide-card'), r = await api('/api/tides');
  if (!r || r.error || !r.predicted) { card.style.display = 'none'; return; }
  card.style.display = '';
  const ft = v => Units.metric ? (v * 0.3048).toFixed(2) + ' m' : v.toFixed(1) + ' ft';
  const clock = t => new Date(t * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  const day = t => new Date(t * 1000).toLocaleDateString([], {weekday: 'short'});
  const nowS = Date.now() / 1000, next = r.hilo.find(h => h.t > nowS), prev = r.hilo.filter(h => h.t <= nowS).pop();
  const lv = r.level && r.level.ft != null ? r.level.ft : null;
  const dir = next ? (next.type === 'H' ? 'Rising' : 'Falling') : '';
  $('#tide-now').textContent = (lv != null ? ft(lv) + ' above low-water datum' : 'Tide') + (dir ? ' · ' + dir : '');
  const bits = [];
  if (next) bits.push('Next ' + (next.type === 'H' ? 'high' : 'low') + ' ' + clock(next.t) + ' (' + ft(next.v) + ')');
  if (r.surge_ft != null) bits.push((r.surge_ft >= 0 ? '+' : '−') + ft(Math.abs(r.surge_ft)) + ' vs. predicted');
  if (r.water_temp_f != null) bits.push('water ' + Units.t(r.water_temp_f) + Units.tu());
  if (r.wind) bits.push('wind ' + r.wind.dir + ' ' + Units.wmph(Math.round(r.wind.mph)) + (r.wind.gust_mph ? ' gust ' + Units.wmph(Math.round(r.wind.gust_mph)) : '') + ' ' + Units.su());
  $('#tide-now2').textContent = 'Bridgeport harbor · ' + bits.join(' · ') + (r.stale ? ' · (data may be out of date)' : '');
  // chart: observed vs predicted, -12 h to +36 h
  const t0 = nowS - 12 * 3600, t1 = nowS + 36 * 3600, W = 600, H = 150, L = 34, B = 18;
  const pts = r.predicted.filter(p => p[0] >= t0 && p[0] <= t1 && p[1] != null);
  const wl = r.flood && r.flood.watch, lo = Math.min(...pts.map(p => p[1]), -1), hi = Math.max(...pts.map(p => p[1]), 9, wl ? wl + 0.7 : 0);
  const X = t => L + (t - t0) / (t1 - t0) * (W - L - 4), Y = v => 6 + (hi - v) / (hi - lo) * (H - B - 6);
  const path = a => a.map((p, i) => (i ? 'L' : 'M') + X(p[0]).toFixed(1) + ',' + Y(p[1]).toFixed(1)).join('');
  const obs = r.observed.filter(p => p[0] >= t0);
  const ticks = [];
  for (let d = Math.ceil(t0 / 21600) * 21600; d < t1; d += 21600) { const h = new Date(d * 1000).getHours(); if (h % 6 === 0) ticks.push(d); }
  const cs = getComputedStyle(document.documentElement), fg = cs.getPropertyValue('--muted').trim() || '#888', ac = cs.getPropertyValue('--accent').trim() || '#2a7ae2';
  $('#tide-chart').innerHTML = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;margin-top:8px" role="img" aria-label="Tide height forecast">
    <line x1="${L}" x2="${W - 4}" y1="${Y(0)}" y2="${Y(0)}" stroke="${fg}" stroke-opacity=".3" stroke-dasharray="3 3"/>
    <text x="${L - 4}" y="${Y(0) + 4}" font-size="10" text-anchor="end" fill="${fg}">0</text>
    <text x="${L - 4}" y="${Y(Math.round(hi - 1)) + 4}" font-size="10" text-anchor="end" fill="${fg}">${Units.metric ? (Math.round(hi - 1) * .3048).toFixed(1) : Math.round(hi - 1)}</text>
    ${ticks.map(d => `<text x="${X(d)}" y="${H - 4}" font-size="10" text-anchor="middle" fill="${fg}">${new Date(d * 1000).getHours() === 0 ? day(d) : clock(d).replace(':00', '')}</text>`).join('')}
    ${wl ? `<line x1="${L}" x2="${W - 4}" y1="${Y(wl)}" y2="${Y(wl)}" stroke="var(--warn)" stroke-width="1.5" stroke-dasharray="5 3"/><text x="${W - 6}" y="${Y(wl) - 3}" font-size="10" text-anchor="end" fill="var(--warn)">flood statements start here</text>` : ''}
    <path d="${path(pts)}" fill="none" stroke="${ac}" stroke-width="2"/>
    <path d="${path(obs)}" fill="none" stroke="${fg}" stroke-width="1.5" stroke-opacity=".8"/>
    <line x1="${X(nowS)}" x2="${X(nowS)}" y1="4" y2="${H - B}" stroke="${fg}" stroke-opacity=".5"/>
  </svg><div class="msg">Blue: predicted tide. Grey: measured so far. Dashed: where the NWS began issuing coastal flood statements at Bridgeport (8.5 ft).</div>`;
  const h = $('#tide-hilo'); h.innerHTML = '';
  const est = Object.fromEntries((r.highs || []).map(x => [x.t, x]));
  h.append(locBlock('High and low tides', r.hilo.filter(x => x.t > nowS - 1800).slice(0, 8).map(x => {
    const e = est[x.t], extra = e && e.category ? ' → about ' + ft(e.estimated) + ', flooding possible (' + e.category + ')' : e && Math.abs(e.estimated - x.v) >= 0.3 ? ' → about ' + ft(e.estimated) : '';
    return [(x.type === 'H' ? 'High ' : 'Low ') + day(x.t) + ' ' + clock(x.t), ft(x.v) + extra];
  }), 'The “about” figure adds the water’s current offset from the prediction (storm surge), fading over a day. It is an estimate.'));
}
async function wxSearch(reset, first = 40) {
  if (reset) { wxOffset = 0; $('#wx-list').innerHTML = ''; }
  const size = reset ? first : 10;      // forecast discussions open with 5; Load more adds 10
  const p = new URLSearchParams({cat: $('#wx-cat').value, office: $('#wx-office').value, q: $('#wx-q').value,
    hours: $('#wx-hours').value, limit: size, offset: wxOffset, warnings: wxWarnOnly ? 1 : 0});
  $('#wx-msg').textContent = 'Searching…';
  const r = await api('/api/products?' + p);
  if (r.error) { $('#wx-msg').textContent = r.error; return; }
  r.items.forEach(it => {
    const d = document.createElement('div'); d.className = 'mrow'; d.style.cssText = 'cursor:pointer;display:block';
    const h = document.createElement('div'); h.style.fontWeight = '600'; h.textContent = `${it.label} · ${it.office}`;
    const m = document.createElement('div'); m.className = 'msg'; m.style.margin = '2px 0';
    m.textContent = wxFmt(it.t) + (it.title ? '  ·  ' + it.title : '');
    const pre = document.createElement('pre'); pre.style.display = 'none';
    d.append(h, m, pre);
    d.onclick = async () => {
      if (pre.style.display === 'none') {
        if (!pre.textContent) { const t = await api('/api/product?name=' + encodeURIComponent(it.name)); pre.textContent = t.text || t.error; }
        pre.style.display = '';
      } else pre.style.display = 'none';
    };
    $('#wx-list').append(d);
  });
  wxOffset += r.items.length;
  $('#wx-more').style.display = r.more ? '' : 'none';
  $('#wx-msg').textContent = $('#wx-list').children.length ? '' : 'Nothing matches in that time window.';
}
let wxWarnOnly = false;
$('#wx-go').onclick = () => { wxWarnOnly = false; wxSearch(true); };
$('#wx-more').onclick = () => wxSearch(false);
$('#wx-q').onkeydown = e => { if (e.key === 'Enter') { wxWarnOnly = false; wxSearch(true); } };
$('#wx-mine').onclick = () => { wxWarnOnly = false; $('#wx-cat').value = ''; $('#wx-office').value = wxMine[0] || ''; $('#wx-q').value = ''; wxSearch(true); };
$('#wx-warn').onclick = () => { wxWarnOnly = true; $('#wx-cat').value = ''; $('#wx-office').value = ''; wxSearch(true); };
$('#wx-afd').onclick = () => { wxWarnOnly = false; $('#wx-cat').value = 'AFD'; $('#wx-office').value = wxMine[0] || ''; wxSearch(true, 5); };

// ---- sky: sun, moon and where the dish points (Stratford, CT)
const SKY = {lat: 41.18, lon: -73.13, satLon: -75.0, tz: 'America/New_York'};   // Stratford, CT
const rad = Math.PI / 180;
function sunPos(ms) {
  const n = ms / 86400000 + 2440587.5 - 2451545.0;
  const L = 280.460 + 0.9856474 * n, g = (357.528 + 0.9856003 * n) * rad;
  const lam = (L + 1.915 * Math.sin(g) + 0.020 * Math.sin(2 * g)) * rad, eps = (23.439 - 0.0000004 * n) * rad;
  const ra = Math.atan2(Math.cos(eps) * Math.sin(lam), Math.cos(lam)), dec = Math.asin(Math.sin(eps) * Math.sin(lam));
  const H = ((280.46061837 + 360.98564736629 * n + SKY.lon) * rad) - ra, p = SKY.lat * rad;
  const el = Math.asin(Math.sin(p) * Math.sin(dec) + Math.cos(p) * Math.cos(dec) * Math.cos(H));
  const az = Math.atan2(Math.sin(H), Math.cos(H) * Math.sin(p) - Math.tan(dec) * Math.cos(p)) + Math.PI;
  return {el: el / rad, az: (az / rad + 360) % 360};
}
function dishDir() {
  const d = (SKY.satLon - SKY.lon) * rad, p = SKY.lat * rad, c = Math.cos(p) * Math.cos(d), k = 6378.14 / 42164.17;
  return {el: Math.atan((c - k) / Math.sqrt(1 - c * c)) / rad, az: (180 + Math.atan(Math.tan(d) / Math.sin(p)) / rad + 360) % 360};
}
function angSep(a, b) {
  const x = a.el * rad, y = b.el * rad, d = (a.az - b.az) * rad;
  return Math.acos(Math.min(1, Math.max(-1, Math.sin(x) * Math.sin(y) + Math.cos(x) * Math.cos(y) * Math.cos(d)))) / rad;
}
function nextCross(from, rising) {
  let prev = sunPos(from).el + 0.833;
  for (let t = from + 120000; t < from + 27 * 3600000; t += 120000) {
    const v = sunPos(t).el + 0.833;
    if (rising ? (prev < 0 && v >= 0) : (prev >= 0 && v < 0)) return t;
    prev = v;
  }
  return null;
}
function moonInfo(ms) {
  const age = (((ms / 86400000 + 2440587.5) - 2451550.1) % 29.530588853 + 29.530588853) % 29.530588853;
  const illum = (1 - Math.cos(2 * Math.PI * age / 29.530588853)) / 2;
  const names = ['New moon', 'Waxing crescent', 'First quarter', 'Waxing gibbous', 'Full moon', 'Waning gibbous', 'Last quarter', 'Waning crescent'];
  return {name: names[Math.round(age / 29.530588853 * 8) % 8], illum: Math.round(illum * 100)};
}
function renderSky() {
  const box = $('#sky'); if (!box) return;
  const now = Date.now(), sun = sunPos(now), dish = dishDir(), sep = angSep(sun, dishDir());
  const tm = t => t ? new Date(t).toLocaleTimeString([], {timeZone: SKY.tz, hour: 'numeric', minute: '2-digit'}) : '–';
  const rise = nextCross(now, true), set = nextCross(now, false), moon = moonInfo(now);
  const warn = sep < 4 && sun.el > 0;
  box.textContent = '';
  const line = t => { const d = document.createElement('div'); d.textContent = t; box.append(d); return d; };
  line(`Dish target: azimuth ${dish.az.toFixed(1)}°, elevation ${dish.el.toFixed(1)}°`);
  line(`Sun now: azimuth ${sun.az.toFixed(0)}°, ${sun.el > 0 ? 'elevation ' + sun.el.toFixed(0) + '°' : 'below the horizon'}`);
  const sp = line(sun.el > 0 ? `Sun is ${sep.toFixed(1)}° from the dish direction` + (warn ? ' – sun interference, expect signal dropouts' : '') : 'Sun is down');
  if (warn) sp.style.color = 'var(--warn)';
  line(`Sunrise ${tm(rise)}, sunset ${tm(set)}`);
  line(`Moon: ${moon.name}, ${moon.illum}% lit`);
}
setInterval(() => { if ($('#aim').classList.contains('active')) renderSky(); }, 30000);

// ---- skywarn: spotter status, CT warnings, storm reports, outlook, tropical
function skLine(box, text, bold, cls) {
  const d = document.createElement('div'); d.style.margin = '5px 0'; if (cls) d.className = cls;
  if (bold) { const b = document.createElement('b'); b.textContent = bold + ' '; d.append(b); }
  d.append(document.createTextNode(text)); box.append(d); return d;
}
async function loadSkywarn() {
  const r = await api('/api/skywarn');
  if (r.error) { $('#sk-banner').textContent = r.error; return; }
  const lv = r.spotter.level, b = $('#sk-banner');
  b.className = 'banner ' + (lv === 'requested' ? 'bad' : lv === 'possible' ? 'warn' : lv === 'none' ? 'good' : '');
  const names = {none: 'Spotter activation not expected', possible: 'Spotter activation possible', requested: 'Spotters requested', unknown: 'Spotter status unknown'};
  b.innerHTML = ''; b.append(names[lv] || lv);
  const sm = document.createElement('small'); sm.textContent = r.spotter.offices.map(o => `${o.office}: ${o.level}`).join(' · ') || 'No outlook received yet'; b.append(sm);
  const al = $('#sk-alerts'); al.innerHTML = '';
  if (!r.alerts.length) skLine(al, 'Nothing in force for any Connecticut zone.');
  r.alerts.forEach(a => {
    const d = skLine(al, `${a.counties.join(', ') || 'Connecticut'}${a.ends ? ', until ' + wxFmt(a.ends) : ''} (${a.office})`, a.name + (a.stratford ? ' – includes Stratford' : ''));
    if (a.level === 'W') d.style.color = 'var(--bad)'; else if (a.level === 'A') d.style.color = 'var(--warn)';
  });
  const sp = $('#sk-spot'); sp.innerHTML = '';
  if (!r.spotter.offices.length) skLine(sp, 'No Hazardous Weather Outlook received in the last 36 hours.');
  r.spotter.offices.forEach(o => {
    skLine(sp, o.statement || 'No spotter statement in the outlook.', `${o.office} (${wxFmt(o.t)})`);
    if (o.day1) { const x = skLine(sp, o.day1); x.className = 'msg'; }
  });
  $('#sk-rep-h').textContent = `Local storm reports within ${Units.d(r.radius_miles)} ${Units.metric ? 'kilometers' : 'miles'}`;
  const rp = $('#sk-rep'); rp.innerHTML = '';
  if (!r.reports.length) skLine(rp, 'No reports in the last 48 hours.');
  r.reports.forEach(x => skLine(rp, `${x.where}, ${x.county} ${x.state} · ${Units.d(x.miles)} ${Units.du()} · ${x.source}${x.remarks ? ' · ' + Units.text(x.remarks) : ''}`,
    `${wxFmt(x.t)} ${x.event}${x.mag ? ' ' + x.mag : ''}`));
  const ot = $('#sk-out'); ot.innerHTML = '';
  if (!r.outlook.length) skLine(ot, 'No outlook received yet.');
  r.outlook.forEach(o => {
    skLine(ot, o.headline || 'No severe thunderstorm areas forecast.', `Day ${o.day} (${o.valid || wxFmt(o.t)})`);
    if (o.relevant.length) o.relevant.forEach(p => { const x = skLine(ot, p); x.className = 'msg'; });
    else { const x = skLine(ot, 'Connecticut and New England are not mentioned.'); x.className = 'msg'; }
  });
  const tp = $('#sk-trop'); tp.innerHTML = '';
  const t = r.tropical;
  skLine(tp, t.outlook || 'No tropical outlook received yet.', 'Atlantic outlook' + (t.outlook_t ? ' (' + wxFmt(t.outlook_t) + ')' : ''));
  if (t.storm) skLine(tp, `${t.storm.name} near ${t.storm.lat}, ${t.storm.lon}`, 'Active storm');
  if (t.coastal.length) t.coastal.forEach(a => skLine(tp, `${a.name}${a.ends ? ' until ' + wxFmt(a.ends) : ''}`, 'Coastal alert for Stratford:'));
  else skLine(tp, 'No coastal or marine alerts for Stratford or the western Sound.');
}

// ---- notifications: switches, times, history
async function loadNotify() {
  const r = await api('/api/notify');
  if (r.error) { $('#nt-msg').textContent = r.error; return; }
  const box = $('#nt-list'); box.innerHTML = '';
  r.categories.forEach(c => {
    const row = document.createElement('label'); row.style.cssText = 'display:flex;gap:10px;align-items:flex-start;padding:8px 0;border-top:1px solid var(--line)';
    const cb = document.createElement('input'); cb.type = 'checkbox'; cb.dataset.cat = c.id; cb.checked = !!r.settings.enabled[c.id]; cb.style.cssText = 'width:auto;margin-top:4px';
    const t = document.createElement('div'); const b = document.createElement('div'); b.style.fontWeight = '600'; b.textContent = c.label;
    const d = document.createElement('div'); d.className = 'msg'; d.style.margin = '0'; d.textContent = c.desc; t.append(b, d); row.append(cb, t); box.append(row);
  });
  $('#nt-health').value = r.settings.health_time; $('#nt-image').value = r.settings.image_time;
  const lg = $('#nt-log'); lg.innerHTML = '';
  if (!r.log.length) { const p = document.createElement('p'); p.className = 'msg'; p.textContent = 'Nothing sent yet. Alerts appear here as they happen.'; lg.append(p); }
  const label = Object.fromEntries(r.categories.map(c => [c.id, c.label]));
  r.log.forEach(x => {
    const d = document.createElement('div'); d.className = 'mrow'; d.style.display = 'block';
    const h = document.createElement('div'); h.style.fontWeight = '600'; h.textContent = x.title;
    const m = document.createElement('div'); m.className = 'msg'; m.style.margin = '2px 0';
    m.textContent = new Date(x.t * 1000).toLocaleString([], {weekday: 'short', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit'}) +
      ' · ' + (label[x.cat] || x.cat) + ' · ' + (x.sent ? 'sent' : 'not sent' + (x.note ? ' (' + x.note + ')' : ''));
    d.append(h, m);
    if (x.body) { const b = document.createElement('div'); b.style.cssText = 'font-size:14px;white-space:pre-wrap'; b.textContent = x.body.slice(0, 300); d.append(b); }
    lg.append(d);
  });
}
$('#nt-save').onclick = async () => {
  const enabled = {}; document.querySelectorAll('#nt-list input[data-cat]').forEach(c => enabled[c.dataset.cat] = c.checked);
  const r = await api('/api/notify/settings', {enabled, health_time: $('#nt-health').value, image_time: $('#nt-image').value});
  $('#nt-msg').textContent = r.error ? r.error : 'Saved.';
};
$('#nt-test').onclick = async e => {
  e.target.disabled = true; $('#nt-msg').textContent = 'Sending…';
  const r = await api('/api/notify/test', {}); $('#nt-msg').textContent = r.result || r.error; e.target.disabled = false; loadNotify();
};

// ---- charts (EMWIN graphics: radar, warnings map, fronts...)
let chFrames = [], chTimer = null, chReady = false;
const chAge = t => { const m = Math.round((Date.now() / 1000 - t) / 60); return m < 90 ? m + ' min ago' : (m / 60).toFixed(1) + ' h ago'; };
async function loadCharts() {
  if (!chReady) {
    const r = await api('/api/charts');
    if (r.error) { $('#ch-msg').textContent = r.error; return; }
    r.groups.forEach(g => {
      const og = document.createElement('optgroup'); og.label = g.group;
      g.items.forEach(i => { const o = document.createElement('option'); o.value = i.code; o.textContent = i.label; og.append(o); });
      $('#ch-sel').append(og);
    });
    chReady = true;
  }
  chLoad();
}
async function chLoad() {
  clearInterval(chTimer); chTimer = null;
  const code = $('#ch-sel').value; if (!code) return;
  $('#ch-msg').textContent = 'Loading…';
  const r = await api(`/api/chart?code=${encodeURIComponent(code)}&n=${$('#ch-n').value}`);
  if (r.error || !r.frames.length) { $('#ch-msg').textContent = r.error || 'Nothing received yet.'; return; }
  chFrames = r.frames;
  chFrames.forEach(f => { const i = new Image(); i.src = '/file/emwin/' + encodeURIComponent(f.name); });
  const sl = $('#ch-slider'); sl.max = chFrames.length - 1; sl.value = chFrames.length - 1;
  sl.style.display = chFrames.length > 1 ? '' : 'none'; $('#ch-play').style.display = chFrames.length > 1 ? '' : 'none';
  chShow(chFrames.length - 1);
  if (chFrames.length > 1) { $('#ch-play').textContent = 'Pause'; chTimer = setInterval(chStep, 700); }
}
function chShow(i) {
  const f = chFrames[i]; if (!f) return;
  $('#ch-img').src = '/file/emwin/' + encodeURIComponent(f.name);
  $('#ch-slider').value = i;
  $('#ch-msg').textContent = new Date(f.t * 1000).toLocaleString([], {weekday: 'short', hour: 'numeric', minute: '2-digit'}) + '  ·  ' + chAge(f.t) + (chFrames.length > 1 ? `  ·  frame ${i + 1} of ${chFrames.length}` : '');
}
function chStep() { chShow((+$('#ch-slider').value + 1) % chFrames.length); }
$('#ch-sel').onchange = chLoad; $('#ch-n').onchange = chLoad;
$('#ch-slider').oninput = e => { clearInterval(chTimer); chTimer = null; $('#ch-play').textContent = 'Play'; chShow(+e.target.value); };
$('#ch-play').onclick = () => {
  if (chTimer) { clearInterval(chTimer); chTimer = null; $('#ch-play').textContent = 'Play'; }
  else { chTimer = setInterval(chStep, 700); $('#ch-play').textContent = 'Pause'; }
};

// ---- logs
if (navigator.standalone || matchMedia('(display-mode: standalone)').matches) {
  $('#zip').style.display = 'none'; $('#zip-note').style.display = '';
}
show(startTab);
// background polls pause while the page is hidden and catch up when it comes back
const whenVisible = fn => () => { if (!document.hidden) fn(); };
pollStatus(); setInterval(whenVisible(pollStatus), 3000);
setInterval(whenVisible(pollStats), 2000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { pollStatus(); pollStats(); } });

// ---- insights: numbers worked out from the satellite, radar, forecasts and signal log
const insT = (rows, head) => '<table style="width:100%;border-collapse:collapse;font-size:14px"><tr style="color:var(--muted);text-align:left">' +
  head.map((h, i) => `<th style="padding:2px 8px 2px 0;${i ? 'text-align:right' : ''}">${h}</th>`).join('') + '</tr>' +
  rows.map(r => '<tr>' + r.map((c, i) => `<td style="padding:2px 8px 2px 0;${i ? 'text-align:right' : ''}">${c == null ? '–' : c}</td>`).join('') + '</tr>').join('') + '</table>';
const insBars = (items, max, fmt) => items.map(([l, v]) => `<div style="display:flex;align-items:center;gap:8px;margin:3px 0;font-size:13px"><span style="width:70px;color:var(--muted)">${l}</span>` +
  `<span style="flex:1;background:var(--bg);border-radius:4px;height:10px"><span style="display:block;height:10px;border-radius:4px;background:var(--accent);width:${Math.max(1, Math.min(100, 100 * v / max))}%"></span></span><span style="width:60px;text-align:right">${fmt(v)}</span></div>`).join('');
const insAgo = iso => { const m = Math.round((Date.now() - Date.parse(iso)) / 60000); return m < 90 ? m + ' min ago' : Math.round(m / 60) + ' h ago'; };
const insImg = (p, cap) => p ? `<a href="/file/${p}" target="_self"><img src="/thumb/${p}?w=720" style="width:100%;border-radius:8px;display:block" loading="lazy"></a><div class="msg">${cap}</div>` : '<p class="msg">No picture yet.</p>';
async function loadInsights() {
  let d; try { d = await api('/api/insights'); } catch { return; }
  if (!d.updated) { $('#ins-note').textContent = 'Nothing yet. The first numbers appear within 15 minutes.'; return; }
  $('#ins-note').textContent = 'Updated ' + insAgo(d.updated) + '. Everything here comes from data this Pi already receives.';
  const c = [], im = d.images || {};
  const s = d.sst || {}, r = d.radar || {}, cl = d.clouds || {}, f = d.forecast || {}, di = d.dish || {};
  // water temperature
  let h = '<div class="card"><h2>Water temperature (from the satellite)</h2>';
  if (s.latest) h += `<div class="grid"><div class="metric"><div class="l">Long Island Sound</div><div class="v">${Units.t(s.latest.sound_f).toFixed(Units.metric ? 1 : 0)} ${Units.tu()}</div><div class="s">${s.sound_24h ? 'last 24 h: ' + Units.t(s.sound_24h.min).toFixed(Units.metric ? 1 : 0) + '–' + Units.t(s.sound_24h.max).toFixed(Units.metric ? 1 : 0) + ' ' + Units.tu() : ''}</div></div>` +
    Object.entries(s.latest.points || {}).map(([k, v]) => `<div class="metric"><div class="l">${k}</div><div class="v">${Units.t(v).toFixed(Units.metric ? 1 : 0)} ${Units.tu()}</div></div>`).join('') + '</div>';
  h += insImg(im.sst_stratford, 'Clear-sky surface temperature; gray is cloud. Coastal pixels mix land and water, so trust open water most. Full set in Views.');
  h += `<p class="msg">Checked against NWS water temperatures at the Jersey shore: ${s.pairs ? s.pairs + ' comparisons over ' + s.days + ' day(s), average error ' + (s.mean_error_f > 0 ? '+' : '') + Units.dt(s.mean_error_f) + ' ' + Units.tu() : 'none yet'}. ` +
    (s.calibrated ? 'Calibration offset applied: ' + (s.offset_k > 0 ? '+' : '') + s.offset_k + ' K.' : 'Calibration starts after 8 comparisons over 3 days. Expect about ±' + (Units.metric ? '1.7 °C' : '3 °F') + '.') + '</p></div>';
  c.push(h);
  // radar
  h = '<div class="card"><h2>Radar near Stratford</h2><div class="grid">';
  const lt = r.latest;
  h += `<div class="metric"><div class="l">Right now</div><div class="v">${lt && lt.max25 != null ? lt.max25.toFixed(0) + ' dBZ' : 'Dry'}</div><div class="s">${lt && lt.max25 != null ? 'strongest echo within 25 km' : 'no echo within 25 km'}</div></div>` +
    `<div class="metric"><div class="l">Last 24 hours</div><div class="v">${r.wet_frames_24h ? r.max_dbz_24h.toFixed(0) + ' dBZ' : 'Dry'}</div><div class="s">${r.wet_frames_24h ? r.wet_frames_24h + ' of ' + r.frames_24h + ' radar frames showed rain' : r.frames_24h + ' radar frames, no rain'}</div></div></div>`;
  h += insImg(im.rad_stratford, 'NWS radar over the satellite infrared picture. Radar comes from the EMWIN feed; no internet needed.') + '</div>';
  c.push(h);
  // clouds
  h = '<div class="card"><h2>Cloud cover over Stratford</h2>';
  if (cl.scans) {
    h += `<div class="grid"><div class="metric"><div class="l">Now</div><div class="v">${cl.latest.cloud}%</div><div class="s">${insAgo(cl.latest.t)}</div></div>` +
      `<div class="metric"><div class="l">Average</div><div class="v">${cl.mean_cloud}%</div><div class="s">${cl.days.length} day(s), ${cl.clear_days} clear, ${cl.cloudy_days} cloudy</div></div>` +
      `<div class="metric"><div class="l">Agrees with airport</div><div class="v">${cl.agreement.pct == null ? '–' : cl.agreement.pct + '%'}</div><div class="s">${cl.agreement.n} comparisons with KBDR</div></div></div>`;
    h += '<h2 style="margin-top:12px">By day</h2>' + insBars(cl.days.slice(-14).map(x => [x.date.slice(5), x.cloud]), 100, v => v + '%');
    h += '<h2 style="margin-top:12px">By hour of day (local)</h2>' + insBars(cl.by_hour.map(x => [x.hour + ':00', x.cloud]), 100, v => v + '%');
    h += '<p class="msg">The satellite also sees high thin cloud that the airport sensor cannot, so some disagreement is normal. History began ' + new Date(cl.first).toLocaleDateString() + '.</p>';
  } else h += '<p class="msg">Collecting.</p>';
  c.push(h + '</div>');
  // forecast
  h = '<div class="card"><h2>How good is the forecast?</h2>';
  if (f.buckets && f.buckets.length) {
    h += (f.headline ? `<p style="margin:0 0 8px">Temperature forecasts for the next 24 hours have been off by <b>${Units.dt(f.headline.temp_mae_f)} ${Units.tu()}</b> on average (${f.headline.n} checks this week).</p>` : '') +
      insT(f.buckets.map(b => [b.label, b.n, Units.dt(b.temp_mae), Units.dt(b.temp_bias), Units.dt(b.persistence_mae), Units.wmph(b.wind_mae), b.sky_mae == null ? null : b.sky_mae + '%']),
        ['Looking ahead', 'Checks', 'Temp error ' + Units.tu(), 'Temp bias ' + Units.tu(), '"Same as now" ' + Units.tu(), 'Wind error ' + Units.su(), 'Cloud error']);
    const bs = f.buckets.filter(b => b.temp_mae != null), mx = Math.max(...bs.map(b => Math.max(b.temp_mae, b.persistence_mae || 0)), 1), bw = 560 / Math.max(bs.length, 1);
    h += `<svg viewBox="0 0 600 150" style="width:100%;height:auto;margin-top:10px" role="img" aria-label="Forecast error by lead time">` + bs.map((b, i) => {
      const x = 20 + i * bw, hh = v => 100 * v / mx;
      return `<rect x="${x + bw * .12}" y="${110 - hh(b.temp_mae)}" width="${bw * .34}" height="${hh(b.temp_mae)}" fill="var(--accent)"/>` +
        (b.persistence_mae != null ? `<rect x="${x + bw * .5}" y="${110 - hh(b.persistence_mae)}" width="${bw * .34}" height="${hh(b.persistence_mae)}" fill="var(--muted)" fill-opacity=".6"/>` : '') +
        `<text x="${x + bw * .3}" y="${106 - hh(b.temp_mae)}" font-size="10" text-anchor="middle" fill="var(--text)">${Units.dt(b.temp_mae)}</text>` +
        `<text x="${x + bw / 2}" y="128" font-size="11" text-anchor="middle" fill="var(--muted)">${b.label}</text>`;
    }).join('') + `</svg><div class="msg">Blue: NWS forecast error. Grey: error if you assumed the temperature never changes. Shorter bars are better.</div>`;
    h += `<p class="msg">Each point forecast is saved when NWS Upton issues it and compared with the Bridgeport airport report at the time it was meant for. "Same as now" is the error you would get by assuming the temperature never changes. ${f.scored} checks from ${f.issued} forecasts.` +
      (f.pop && f.pop.rain_rate >= 5 ? ` Rain chances: ${f.pop.n} checks, score ${f.pop.brier} against ${f.pop.climatology_brier} for always guessing the average (lower is better).` : '') + '</p>';
  } else h += '<p class="msg">Collecting.</p>';
  c.push(h + '</div>');
  // dish
  h = '<div class="card"><h2>Dish signal vs weather and sun</h2>';
  if (di.categories && di.categories.length) {
    h += `<p class="msg" style="margin-top:0">${di.minutes} minutes over ${di.days} day(s). Viterbi errors: lower is better.</p>` +
      insT(di.categories.map(x => [x.name, x.minutes, x.vit, x.delta == null ? null : (x.delta > 0 ? '+' : '') + x.delta, x.locked_pct + '%']), ['Weather', 'Minutes', 'Errors', 'vs clear', 'Locked']);
    if (di.categories.length < 2) h += '<p class="msg">Only clear weather so far. Rain, snow and cloudy days will fill in the comparison.</p>';
    h += '<h2 style="margin-top:12px">Sun near the dish</h2>' + insT(di.sun.map(x => [x.name, x.minutes, x.vit, x.locked_pct + '%']), ['Sun from dish axis', 'Minutes', 'Errors', 'Locked']);
    h += '<p class="msg">The sun is a strong radio noise source. If errors rise when it is within a few degrees of the dish axis, that is the cause, not the weather.</p>';
  } else h += '<p class="msg">Collecting.</p>';
  c.push(h + '</div>');
  $('#ins-body').innerHTML = c.join('');
}
setInterval(() => { if ($('#insights').classList.contains('active') && !document.hidden) loadInsights(); }, 120000);
