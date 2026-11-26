(() => {
  'use strict';

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const fmtBytes = (n) => {
    if (n == null || isNaN(n)) return '—';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0, v = Number(n);
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return `${v >= 100 || i === 0 ? v.toFixed(0) : v.toFixed(1)} ${u[i]}`;
  };
  const fmtRate = (n) => n == null || isNaN(n) ? '—' : `${fmtBytes(n)}/s`;
  const fmtNum = (n) => n == null || isNaN(n) ? '—' : Number(n).toLocaleString();
  const fmtDur = (s) => {
    if (s == null || isNaN(s)) return '—';
    s = Math.max(0, Math.floor(s));
    const d = Math.floor(s / 86400); s %= 86400;
    const h = Math.floor(s / 3600); s %= 3600;
    const m = Math.floor(s / 60); const sec = s % 60;
    if (d > 0) return `${d}d ${h}h ${m}m`;
    if (h > 0) return `${h}h ${m}m ${sec}s`;
    if (m > 0) return `${m}m ${sec}s`;
    return `${sec}s`;
  };
  const fmtTime = (ts) => {
    if (!ts) return '—';
    const d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return '—';
    return d.toLocaleString([], { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' });
  };
  const fmtLogTime = (ts) => {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    if (isNaN(d.getTime())) return '';
    return d.toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  };
  const esc = (s) => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

  const state = {
    view: 'dashboard',
    config: { server_url: '', username: '', has_password: false, drive_letter: 'Z' },
    connected: false,
    metrics: [],
    metricsSource: null,
    logsSource: null,
    lastMetric: null,
    prevMetric: null,
    filesPath: '/',
    files: [],
    jobs: [],
    jobsTimer: null,
    refreshTimer: null,
  };

  // Toasts
  function toast(msg, kind = '') {
    const host = $('#toastHost');
    const el = document.createElement('div');
    el.className = `toast ${kind}`.trim();
    el.textContent = msg;
    host.appendChild(el);
    setTimeout(() => { el.classList.add('out'); el.addEventListener('animationend', () => el.remove(), { once: true }); }, 3200);
  }

  // Navigation
  const viewMeta = {
    dashboard: ['Dashboard', 'Live view of your ModalFiles server.'],
    files: ['Files', 'Browse, inspect, and compress remote folders.'],
    logs: ['Logs', 'Live server log stream.'],
    settings: ['Settings', 'Connection and Windows drive mapping.'],
  };
  function showView(name) {
    state.view = name;
    $$('.nav-item').forEach(b => b.classList.toggle('active', b.dataset.view === name));
    $$('.view').forEach(v => v.classList.toggle('active', v.id === `view-${name}`));
    const [t, s] = viewMeta[name] || viewMeta.dashboard;
    $('#pageTitle').textContent = t;
    $('#pageSubtitle').textContent = s;
    if (name === 'files') loadFiles(state.filesPath);
    if (name === 'logs') ensureLogs();
    if (name === 'dashboard') ensureMetrics();
  }
  $$('.nav-item').forEach(b => b.addEventListener('click', () => showView(b.dataset.view)));

  // Connection pill
  function setConn(ok, label) {
    state.connected = !!ok;
    $('#connDot').className = `conn-dot ${ok ? 'ok' : ok === false ? 'err' : ''}`.trim();
    $('#connLabel').textContent = label || (ok ? 'Connected' : 'Not connected');
  }

  // Config
  async function loadConfig() {
    try {
      const r = await fetch('/local/api/config');
      const cfg = await r.json();
      state.config = { ...state.config, ...cfg };
      $('#cfgUrl').value = cfg.server_url || '';
      $('#cfgUser').value = cfg.username || '';
      $('#cfgDrive').value = (cfg.drive_letter || 'Z').replace(':', '');
      $('#cfgPass').value = '';
      $('#cfgPass').placeholder = cfg.has_password ? 'Password saved (leave blank to keep)' : 'Enter password';
    } catch (e) { console.error(e); }
  }

  async function saveConfig(e) {
    e?.preventDefault();
    const body = {
      server_url: $('#cfgUrl').value.trim(),
      username: $('#cfgUser').value.trim(),
      drive_letter: ($('#cfgDrive').value.trim() || 'Z').replace(':', '').toUpperCase(),
    };
    const pass = $('#cfgPass').value;
    if (pass) body.password = pass;
    const msg = $('#settingsMsg');
    msg.textContent = 'Saving...'; msg.className = 'form-msg';
    try {
      const r = await fetch('/local/api/config', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
      if (!r.ok) throw new Error('Save failed');
      await loadConfig();
      msg.textContent = 'Settings saved.'; msg.className = 'form-msg ok';
      toast('Settings saved', 'ok');
      reconnectStreams();
    } catch (err) {
      msg.textContent = err.message; msg.className = 'form-msg err';
      toast(err.message, 'err');
    }
  }
  $('#settingsForm').addEventListener('submit', saveConfig);

  async function testConnection() {
    const btn = $('#testConnBtn');
    const sbtn = $('#settingsTestBtn');
    btn.disabled = sbtn.disabled = true;
    const msg = $('#settingsMsg');
    msg.textContent = 'Testing connection...'; msg.className = 'form-msg';
    try {
      const r = await fetch('/local/api/test-connection', { method: 'POST' });
      const data = await r.json();
      if (data.ok) {
        setConn(true, 'Connected');
        msg.textContent = 'Connection OK.'; msg.className = 'form-msg ok';
        toast('Connected to ModalFiles server', 'ok');
        if (data.status) applyMetrics(data.status);
        reconnectStreams();
        loadFiles(state.filesPath);
        loadJobs();
      } else {
        setConn(false, 'Connection failed');
        msg.textContent = data.error || 'Connection failed'; msg.className = 'form-msg err';
        toast(data.error || 'Connection failed', 'err');
      }
    } catch (e) {
      setConn(false, 'Connection failed');
      msg.textContent = e.message; msg.className = 'form-msg err';
    } finally {
      btn.disabled = sbtn.disabled = false;
    }
  }
  $('#testConnBtn').addEventListener('click', testConnection);
  $('#settingsTestBtn').addEventListener('click', testConnection);
  $('#refreshBtn').addEventListener('click', () => { testConnection(); loadFiles(state.filesPath); loadJobs(); });

  // Metrics
  function ensureMetrics() {
    if (state.metricsSource) return;
    connectMetrics();
  }
  function connectMetrics() {
    disconnectMetrics();
    const url = '/local/api/remote/metrics';
    try {
      const es = new EventSource(url);
      state.metricsSource = es;
      es.onmessage = (ev) => {
        try {
          const m = JSON.parse(ev.data);
          applyMetrics(m);
          setConn(true, 'Connected');
        } catch {}
      };
      es.onerror = () => {
        setConn(false, 'Reconnecting...');
        // EventSource auto-reconnects; don't close.
      };
    } catch (e) {
      console.error(e);
    }
  }
  function disconnectMetrics() {
    if (state.metricsSource) { try { state.metricsSource.close(); } catch {} state.metricsSource = null; }
  }

  function applyMetrics(m) {
    state.prevMetric = state.lastMetric;
    state.lastMetric = m;
    const prev = state.prevMetric;
    const dt = prev ? Math.max(0.5, (m.uptime_s || 0) - (prev.uptime_s || 0)) : 1;
    const inRate = prev ? Math.max(0, ((m.bytes_in||0) - (prev.bytes_in||0)) / dt) : 0;
    const outRate = prev ? Math.max(0, ((m.bytes_out||0) - (prev.bytes_out||0)) / dt) : 0;
    m._inRate = inRate; m._outRate = outRate;
    state.metrics.push(m);
    if (state.metrics.length > 90) state.metrics.shift();

    $('#statBytesIn').textContent = fmtBytes(m.bytes_in);
    $('#statBytesOut').textContent = fmtBytes(m.bytes_out);
    $('#statRequests').textContent = fmtNum(m.requests_total);
    $('#statRequestsMeta').textContent = `${fmtNum(m.requests_active)} active / ${fmtNum(m.requests_total)} total`;
    $('#statUptime').textContent = fmtDur(m.uptime_s);
    $('#statUptimeMeta').textContent = m.volume_attached ? 'Volume attached' : 'No volume attached';

    $('#healthCpu').textContent = (m.cpu_user_s != null) ? `${(m.cpu_user_s + (m.cpu_sys_s||0)).toFixed(1)}s` : '—';
    $('#healthRss').textContent = m.max_rss_kb != null ? fmtBytes(m.max_rss_kb * 1024) : '—';
    $('#healthLoad').textContent = Array.isArray(m.loadavg) ? m.loadavg.map(x => x.toFixed(2)).join(' / ') : '—';
    if (m.disk_total) {
      const used = m.disk_used || 0;
      const pct = Math.max(0, Math.min(100, (used / m.disk_total) * 100));
      $('#healthDisk').textContent = `${pct.toFixed(0)}%`;
      $('#diskBarFill').style.width = pct + '%';
      $('#diskBarLabel').textContent = `${fmtBytes(used)} used of ${fmtBytes(m.disk_total)}`;
    }
    $('#healthErrors').textContent = fmtNum(m.errors_total);
    $('#health7z').textContent = m.seven_zip ? 'Available' : 'Fallback (zipfile)';

    $('#rateIn').textContent = fmtRate(inRate);
    $('#rateOut').textContent = fmtRate(outRate);
    $('#activeReq').textContent = fmtNum(m.requests_active);
    const maxRate = Math.max(1024, ...state.metrics.map(x => Math.max(x._inRate||0, x._outRate||0)));
    $('#rateInBar').style.width = Math.min(100, (inRate / maxRate) * 100) + '%';
    $('#rateOutBar').style.width = Math.min(100, (outRate / maxRate) * 100) + '%';
    $('#activeReqBar').style.width = Math.min(100, ((m.requests_active||0) / 20) * 100) + '%';

    drawChart();
  }

  function drawChart() {
    const svg = $('#throughputChart');
    const W = 800, H = 220, pad = {l:8,r:8,t:14,b:18};
    const innerW = W - pad.l - pad.r, innerH = H - pad.t - pad.b;
    const data = state.metrics.slice(-60);
    if (data.length < 2) {
      svg.innerHTML = `<text x="${W/2}" y="${H/2}" text-anchor="middle" fill="#73829b" font-size="13" font-family="Segoe UI, sans-serif">Waiting for metrics…</text>`;
      return;
    }
    const maxV = Math.max(1024, ...data.map(d => Math.max(d._inRate||0, d._outRate||0)));
    const x = (i) => pad.l + (i / (data.length - 1)) * innerW;
    const y = (v) => pad.t + innerH - (Math.max(0,v) / maxV) * innerH;
    const pathFor = (key) => data.map((d,i) => `${i===0?'M':'L'}${x(i).toFixed(1)},${y(d[key]||0).toFixed(1)}`).join(' ');
    const areaFor = (key) => {
      const top = data.map((d,i) => `${i===0?'M':'L'}${x(i).toFixed(1)},${y(d[key]||0).toFixed(1)}`).join(' ');
      return `${top} L${x(data.length-1).toFixed(1)},${(pad.t+innerH).toFixed(1)} L${x(0).toFixed(1)},${(pad.t+innerH).toFixed(1)} Z`;
    };
    const grid = [0,0.25,0.5,0.75,1].map(t => {
      const gy = pad.t + innerH * (1-t);
      const label = fmtRate(maxV * t);
      return `<line x1="${pad.l}" y1="${gy}" x2="${W-pad.r}" y2="${gy}" stroke="rgba(255,255,255,0.06)" stroke-width="1"/>
              <text x="${W-pad.r}" y="${gy-4}" text-anchor="end" fill="#64748b" font-size="10" font-family="Segoe UI, sans-serif">${esc(label)}</text>`;
    }).join('');
    svg.innerHTML = `
      <defs>
        <linearGradient id="gIn" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#60a5fa" stop-opacity="0.28"/><stop offset="100%" stop-color="#60a5fa" stop-opacity="0"/></linearGradient>
        <linearGradient id="gOut" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#a78bfa" stop-opacity="0.22"/><stop offset="100%" stop-color="#a78bfa" stop-opacity="0"/></linearGradient>
      </defs>
      ${grid}
      <path d="${areaFor('_outRate')}" fill="url(#gOut)"></path>
      <path d="${areaFor('_inRate')}" fill="url(#gIn)"></path>
      <path d="${pathFor('_outRate')}" fill="none" stroke="#a78bfa" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"></path>
      <path d="${pathFor('_inRate')}" fill="none" stroke="#60a5fa" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"></path>
    `;
  }

  function reconnectStreams() {
    disconnectMetrics();
    disconnectLogs();
    state.metrics = [];
    state.lastMetric = null;
    state.prevMetric = null;
    if (state.view === 'dashboard' || true) connectMetrics();
    if (state.view === 'logs') connectLogs();
  }

  // Logs
  function ensureLogs() { if (!state.logsSource) connectLogs(); }
  function connectLogs() {
    disconnectLogs();
    const term = $('#logTerminal');
    try {
      const es = new EventSource('/local/api/remote/logs');
      state.logsSource = es;
      es.onmessage = (ev) => {
        try { appendLog(JSON.parse(ev.data)); } catch {}
      };
      es.onerror = () => { /* auto-reconnect */ };
    } catch (e) { console.error(e); }
  }
  function disconnectLogs() {
    if (state.logsSource) { try { state.logsSource.close(); } catch {} state.logsSource = null; }
  }
  function appendLog(entry) {
    const term = $('#logTerminal');
    const atBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 40;
    const line = document.createElement('div');
    line.className = 'log-line';
    const lvl = esc(entry.level || 'INFO');
    line.innerHTML = `<span class="log-ts">${esc(fmtLogTime(entry.ts))}</span><span class="log-level ${lvl}">${lvl}</span><span class="log-msg">${esc(entry.msg || '')}</span>`;
    term.appendChild(line);
    // cap lines in DOM
    while (term.childNodes.length > 800) term.removeChild(term.firstChild);
    if ($('#logsAutoscroll').checked && atBottom) term.scrollTop = term.scrollHeight;
  }
  $('#logsClearBtn').addEventListener('click', () => { $('#logTerminal').innerHTML = ''; });

  // Files
  function pathParts(p) {
    const clean = '/' + (p || '/').split('/').filter(Boolean).join('/');
    const parts = clean === '/' ? [] : clean.slice(1).split('/');
    return { clean, parts };
  }
  function renderBreadcrumbs(p) {
    const { parts } = pathParts(p);
    const wrap = $('#breadcrumbs');
    let html = `<button class="crumb ${parts.length===0?'current':''}" data-path="/">Root</button>`;
    let acc = '';
    parts.forEach((part, i) => {
      acc += '/' + part;
      const cur = i === parts.length - 1;
      html += `<span class="muted">/</span><button class="crumb ${cur?'current':''}" data-path="${esc(acc)}">${esc(part)}</button>`;
    });
    wrap.innerHTML = html;
    $$('.crumb', wrap).forEach(b => b.addEventListener('click', () => loadFiles(b.dataset.path)));
  }

  async function loadFiles(p = state.filesPath) {
    state.filesPath = p || '/';
    renderBreadcrumbs(state.filesPath);
    const body = $('#filesBody');
    body.innerHTML = `<tr><td colspan="6" class="empty-cell">Loading…</td></tr>`;
    try {
      const r = await fetch(`/local/api/remote/files?path=${encodeURIComponent(state.filesPath)}`);
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'Failed to load files');
      const entries = data.entries || [];
      if (entries.length === 0) {
        body.innerHTML = `<tr><td colspan="6" class="empty-cell">This folder is empty.</td></tr>`;
        return;
      }
      body.innerHTML = entries.map(e => renderFileRow(e)).join('');
      $$('.file-open', body).forEach(btn => btn.addEventListener('click', () => loadFiles(btn.dataset.path)));
      $$('.file-zip', body).forEach(btn => btn.addEventListener('click', () => createZip(btn.dataset.path, btn)));
    } catch (e) {
      body.innerHTML = `<tr><td colspan="6" class="empty-cell">${esc(e.message)}</td></tr>`;
    }
  }

  function fileIcon(entry) {
    if (entry.kind === 'dir') return `<span class="file-icon dir"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7z"/></svg></span>`;
    const n = (entry.name || '').toLowerCase();
    if (n.endsWith('.zip')) return `<span class="file-icon zip"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M7 3h7l5 5v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M14 3v5h5M10 7v2m0 2v2m0 2v2"/></svg></span>`;
    return `<span class="file-icon file"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M7 3h7l5 5v11a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2z"/><path d="M14 3v5h5"/></svg></span>`;
  }

  function renderFileRow(e) {
    const isDir = e.kind === 'dir';
    const name = isDir
      ? `<button class="file-open" data-path="${esc(e.path)}">${esc(e.name)}</button>`
      : `<span>${esc(e.name)}</span>`;
    const type = isDir ? 'Folder' : (e.name.toLowerCase().endsWith('.zip') ? 'ZIP archive' : 'File');
    const size = isDir ? `<span class="muted">${e.children != null ? fmtNum(e.children) + ' items' : '—'}</span>` : fmtBytes(e.size);
    const modified = e.mtime ? fmtTime(e.mtime) : '—';
    let zipCell = '<span class="muted">—</span>';
    let action = '';
    if (isDir) {
      if (e.has_zip) {
        zipCell = `<span class="badge badge-ok">ZIP exists</span>`;
      } else {
        zipCell = `<span class="badge badge-warn">No ZIP</span>`;
        action = `<button class="btn btn-sm btn-ghost file-zip" data-path="${esc(e.path)}">Compress to ZIP</button>`;
      }
    }
    return `<tr>
      <td><div class="file-name">${fileIcon(e)}${name}</div></td>
      <td class="muted">${esc(type)}</td>
      <td>${size}</td>
      <td class="muted">${esc(modified)}</td>
      <td>${zipCell}</td>
      <td>${action}</td>
    </tr>`;
  }

  $('#filesUpBtn').addEventListener('click', () => {
    const { parts } = pathParts(state.filesPath);
    parts.pop();
    loadFiles('/' + parts.join('/'));
  });
  $('#filesRefreshBtn').addEventListener('click', () => loadFiles(state.filesPath));

  async function createZip(path, btn) {
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = 'Starting...';
    try {
      const r = await fetch('/local/api/remote/zip', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ path })
      });
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'Failed to start ZIP job');
      toast(`Cloud ZIP started for ${path}`, 'ok');
      loadJobs();
      setTimeout(() => loadFiles(state.filesPath), 1200);
    } catch (e) {
      toast(e.message, 'err');
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  }

  // Jobs
  async function loadJobs() {
    try {
      const r = await fetch('/local/api/remote/zip/jobs');
      if (!r.ok) return;
      const data = await r.json();
      state.jobs = data.jobs || [];
      renderJobs();
    } catch {}
  }

  function renderJobs() {
    const host = $('#jobsList');
    const jobs = state.jobs || [];
    if (!jobs.length) {
      host.innerHTML = `<div class="empty-state small">No ZIP jobs yet.</div>`;
      return;
    }
    host.innerHTML = jobs.slice(0, 8).map(j => {
      const pct = Math.max(0, Math.min(100, j.percent || 0));
      const status = j.status || 'unknown';
      const cls = status === 'done' ? 'done' : status === 'error' ? 'error' : '';
      const label = status === 'done' ? 'Done' : status === 'error' ? 'Error' : status === 'running' ? `${pct}%` : 'Queued';
      const meta = j.error ? esc(j.error) : esc(j.last_line || j.stage || '');
      const barCls = (status === 'running' || status === 'queued') ? 'running' : '';
      return `<div class="job-card">
        <div class="job-top">
          <div class="job-name" title="${esc(j.folder || '')}">${esc(j.zip_name || j.folder || 'ZIP job')}</div>
          <div class="job-status ${cls}">${esc(label)}</div>
        </div>
        <div class="job-bar"><div class="job-bar-fill ${barCls}" style="width:${pct}%"></div></div>
        <div class="job-meta" title="${meta}">${meta || '&nbsp;'}</div>
      </div>`;
    }).join('');
  }

  function startJobsPolling() {
    if (state.jobsTimer) clearInterval(state.jobsTimer);
    state.jobsTimer = setInterval(() => { loadJobs(); }, 3000);
  }

  // Drive mapping
  async function mapDrive() {
    const msg = $('#driveMsg');
    msg.textContent = 'Mapping drive...'; msg.className = 'form-msg';
    $('#mapDriveBtn').disabled = true;
    try {
      const r = await fetch('/local/api/map-drive', { method: 'POST' });
      const data = await r.json();
      if (data.ok) {
        msg.textContent = `Mapped ${data.drive} to ${data.unc}`; msg.className = 'form-msg ok';
        toast(`Drive ${data.drive} mapped`, 'ok');
      } else {
        msg.textContent = data.error || 'Mapping failed'; msg.className = 'form-msg err';
        toast(data.error || 'Mapping failed', 'err');
      }
    } catch (e) { msg.textContent = e.message; msg.className = 'form-msg err'; }
    finally { $('#mapDriveBtn').disabled = false; }
  }
  async function unmapDrive() {
    const msg = $('#driveMsg');
    msg.textContent = 'Disconnecting...'; msg.className = 'form-msg';
    $('#unmapDriveBtn').disabled = true;
    try {
      const r = await fetch('/local/api/unmap-drive', { method: 'POST' });
      const data = await r.json();
      if (data.ok) { msg.textContent = 'Drive disconnected.'; msg.className = 'form-msg ok'; toast('Drive disconnected', 'ok'); }
      else { msg.textContent = data.error || data.output || 'Failed'; msg.className = 'form-msg err'; }
    } catch (e) { msg.textContent = e.message; msg.className = 'form-msg err'; }
    finally { $('#unmapDriveBtn').disabled = false; }
  }
  $('#mapDriveBtn').addEventListener('click', mapDrive);
  $('#unmapDriveBtn').addEventListener('click', unmapDrive);

  // Init
  async function init() {
    await loadConfig();
    showView('dashboard');
    connectMetrics();
    startJobsPolling();
    loadJobs();
    // Auto-test if configured
    if (state.config.server_url && state.config.has_password) {
      testConnection();
    } else {
      setConn(false, 'Not configured');
    }
  }

  init();
})();
