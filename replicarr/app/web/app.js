// Replicarr — vanilla ES modules, relative paths only (Ingress-safe)
// API keys never reach the browser.

// ── DOM helpers ───────────────────────────────────────────────────────────────
const $ = (sel, ctx = document) => ctx.querySelector(sel);
const $$ = (sel, ctx = document) => [...ctx.querySelectorAll(sel)];

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { "Content-Type": "application/json", ...opts.headers },
    ...opts,
    body: opts.body != null ? JSON.stringify(opts.body) : undefined,
  });
  if (!r.ok) {
    const txt = await r.text().catch(() => "");
    throw new Error(`${r.status}: ${txt}`);
  }
  return r.status === 204 ? null : r.json();
}

// ── Formatters ────────────────────────────────────────────────────────────────
function fmtBytes(b) {
  if (b == null || b < 0) return "—";
  if (b === 0) return "0 B";
  if (b < 1024) return `${b} B`;
  if (b < 1024 ** 2) return `${(b / 1024).toFixed(1)} KB`;
  if (b < 1024 ** 3) return `${(b / 1024 ** 2).toFixed(1)} MB`;
  return `${(b / 1024 ** 3).toFixed(2)} GB`;
}
function fmtSpeed(bps) { return (!bps || bps < 1) ? "—" : fmtBytes(bps) + "/s"; }
function fmtEta(s) {
  if (!s || s <= 0) return "—";
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.round(s / 60)}m`;
  return `${Math.round(s / 3600)}h`;
}
function esc(v) {
  return String(v ?? "")
    .replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ── Chip ──────────────────────────────────────────────────────────────────────
function chipClass(state, paused) {
  if (paused) return "paused";
  switch (state) {
    case "idle":     return "synced";
    case "syncing":  return "syncing";
    case "scanning": return "scanning";
    case "error":    return "error";
    default:         return "offline";
  }
}
function chipLabel(state, paused) {
  if (paused) return "Paused";
  switch (state) {
    case "idle":     return "Up to Date";
    case "syncing":  return "Syncing";
    case "scanning": return "Scanning";
    case "error":    return "Error";
    default:         return state || "Unknown";
  }
}
function chip(state, paused) {
  const c = chipClass(state, paused);
  const l = chipLabel(state, paused);
  return `<span class="chip ${c}"><span class="chip-dot"></span>${l}</span>`;
}

// ── State ─────────────────────────────────────────────────────────────────────
let activeTab     = "overview";
let instances     = [];   // from /api/instances (no api_key)
let statusData    = [];   // from /api/status
let transferData  = null; // from /api/transfers
let selectedInstId  = null;
let selectedFolderId = null;

// ── Polling ───────────────────────────────────────────────────────────────────
async function poll() {
  try {
    [statusData, transferData] = await Promise.all([
      api("api/status"),
      api("api/transfers"),
    ]);
    applyPoll();
  } catch (e) {
    console.warn("Poll error:", e);
  }
}

function applyPoll() {
  updateSidebarCount();
  if (activeTab === "overview")   renderOverview();
  if (activeTab === "transfers")  renderTransfers();
  if (activeTab === "instances")  renderInstancesTab();
  if (selectedInstId)             updateDetailPanel();
}

// ── Tab routing ───────────────────────────────────────────────────────────────
function switchTab(tab) {
  activeTab = tab;
  $$(".topbar-tab, .sidebar-item").forEach(el => {
    el.classList.toggle("active", el.dataset.tab === tab);
  });
  $$(".tab-panel").forEach(el =>
    el.classList.toggle("hidden", el.dataset.panel !== tab)
  );
  if (tab === "overview")   renderOverview();
  if (tab === "transfers")  renderTransfers();
  if (tab === "instances")  renderInstancesTab();
}

// ── Sidebar count & filters ───────────────────────────────────────────────────
function updateSidebarCount() {
  $("#sb-count").textContent = instances.length;
}

let activeFilter = null;

function applyFilter(filter) {
  activeFilter = activeFilter === filter ? null : filter;
  $$("[id^='filter-']").forEach(el => el.classList.remove("active"));
  if (activeFilter) $(`#filter-${activeFilter}`)?.classList.add("active");
  renderOverview();
}

function filteredStatus() {
  if (!activeFilter) return statusData;
  return statusData.filter(inst => {
    if (activeFilter === "online")  return inst.online === true;
    if (activeFilter === "offline") return inst.online === false;
    if (activeFilter === "syncing") return (inst.folders || []).some(f => f.state === "syncing");
    return true;
  });
}

// ── Overview tab ──────────────────────────────────────────────────────────────
function renderOverview() {
  renderQuickCards();
  renderFolderTable();
}

function renderQuickCards() {
  const el = $("#quick-cards");
  const visible = filteredStatus();
  if (!statusData.length) {
    el.innerHTML = `<div class="loading-row" style="grid-column:1/-1">
      No instances. <button class="btn btn-primary btn-sm" onclick="switchTab('instances')">Add Instance</button>
    </div>`;
    return;
  }

  el.innerHTML = visible.map(inst => {
    const online = inst.online;
    const folderCount = inst.folders?.length ?? 0;
    const totalBytes = (inst.folders || []).reduce((s, f) => s + (f.globalBytes || 0), 0);
    const iconCls = online ? "" : "offline";
    const selCls  = selectedInstId === inst.id ? "selected" : "";

    return `<div class="quick-card ${selCls}" onclick="selectInstance('${esc(inst.id)}')">
      <div class="quick-card-icon ${iconCls}">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="${online ? "var(--accent)" : "var(--red)"}" stroke-width="2">
          <ellipse cx="12" cy="5" rx="9" ry="3"/>
          <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/>
          <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>
        </svg>
      </div>
      <div class="quick-card-name">${esc(inst.name)}</div>
      <div class="quick-card-meta">
        ${online
          ? `${fmtBytes(totalBytes)} · ${folderCount} folder${folderCount !== 1 ? "s" : ""}`
          : `<span style="color:var(--red)">Offline</span>`}
      </div>
    </div>`;
  }).join("");
}

function selectInstance(id) {
  selectedInstId = id;
  selectedFolderId = null;
  renderQuickCards();
  renderFolderTable();
  openDetailPanel("instance", id);
}

function renderFolderTable() {
  const inst = statusData.find(i => i.id === selectedInstId);
  const bc = $("#breadcrumb");
  const actions = $("#table-actions");
  const tbody = $("#folder-tbody");

  if (!inst) {
    bc.innerHTML = `<span class="breadcrumb-part current">Select an instance above</span>`;
    actions.style.display = "none";
    tbody.innerHTML = `<tr><td colspan="5" class="loading-row">Select an instance to view its folders.</td></tr>`;
    return;
  }

  bc.innerHTML = `
    <span class="breadcrumb-part" onclick="selectInstance('${esc(inst.id)}')">${esc(inst.name)}</span>
    <span class="breadcrumb-sep">›</span>
    <span class="breadcrumb-part current">Folders</span>`;

  actions.style.display = "flex";
  $("#btn-add-folder").onclick = () => openAddFolder(inst.id);

  if (!inst.online) {
    tbody.innerHTML = `<tr class="offline-row"><td colspan="5" style="padding:14px 20px">⚠ ${esc(inst.error || "Instance offline")}</td></tr>`;
    return;
  }

  const folders = inst.folders || [];
  if (!folders.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="loading-row">No folders on this instance.</td></tr>`;
    return;
  }

  tbody.innerHTML = folders.map(f => {
    if (f.error) {
      return `<tr><td colspan="5" class="offline-row" style="padding:11px 20px">
        <span class="mono">${esc(f.id)}</span> — ${esc(f.error)}
      </td></tr>`;
    }
    const pct  = f.completion ?? 100;
    const fillCls = pct >= 100 ? "complete" : "";
    const selCls  = selectedFolderId === f.id ? "selected" : "";
    const chipHtml = chip(f.state, f.paused);

    return `<tr class="${selCls}" onclick="selectFolder('${esc(inst.id)}', '${esc(f.id)}')">
      <td>
        <div class="td-name">
          <div class="td-icon ${f.paused ? "paused" : f.state === "syncing" ? "syncing" : ""}">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="${f.paused ? "var(--amber)" : "var(--accent)"}" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
          </div>
          <div>
            <div>${esc(f.label || f.id)}</div>
            <div class="td-meta mono">${esc(f.path || "")}</div>
          </div>
        </div>
      </td>
      <td>${chipHtml}</td>
      <td class="text-sm">${fmtBytes(f.globalBytes)}</td>
      <td>
        <div class="progress-bar" style="width:80px">
          <div class="progress-fill ${fillCls}" style="width:${pct}%"></div>
        </div>
        <div class="text-xs text-2 mt-4">${pct}%</div>
      </td>
      <td>
        <div class="flex gap-6 justify-end">
          ${f.paused
            ? `<button class="btn btn-ghost btn-sm" title="Resume folder" onclick="actFolder(event,'resume','${esc(inst.id)}','${esc(f.id)}')">Resume</button>`
            : `<button class="btn btn-ghost btn-sm" title="Pause folder — stops entire folder sync" onclick="actFolder(event,'pause','${esc(inst.id)}','${esc(f.id)}')">Pause</button>`}
          <button class="btn btn-ghost btn-sm" onclick="openPushModal(event,'${esc(inst.id)}','${esc(f.id)}')">Push →</button>
        </div>
      </td>
    </tr>`;
  }).join("");
}

function selectFolder(instId, folderId) {
  selectedFolderId = folderId;
  openDetailPanel("folder", instId, folderId);
  renderFolderTable();
}

async function actFolder(e, action, instId, folderId) {
  e.stopPropagation();
  const btn = e.target.closest("button");
  btn.disabled = true;
  try {
    await api(`api/folders/${instId}/${folderId}/${action}`, { method: "POST" });
    await poll();
  } catch (err) { alert(err.message); }
  finally { btn.disabled = false; }
}

// ── Detail panel ──────────────────────────────────────────────────────────────
function openDetailPanel(type, instId, folderId) {
  const panel = $("#detail-panel");
  panel.classList.remove("hidden");
  updateDetailPanel(type, instId, folderId);
}

function closeDetail() {
  $("#detail-panel").classList.add("hidden");
  selectedFolderId = null;
  renderFolderTable();
}

function updateDetailPanel(type, instId, folderId) {
  type     = type     ?? (selectedFolderId ? "folder" : "instance");
  instId   = instId   ?? selectedInstId;
  folderId = folderId ?? selectedFolderId;

  if (!instId) return;
  const inst = statusData.find(i => i.id === instId);
  if (!inst) return;

  if (type === "folder" && folderId) {
    renderFolderDetail(inst, folderId);
  } else {
    renderInstanceDetail(inst);
  }
}

function renderInstanceDetail(inst) {
  const body = $("#detail-body");
  const online = inst.online;

  const folders = inst.folders || [];
  const devices = inst.devices || [];
  const totalBytes = folders.reduce((s, f) => s + (f.globalBytes || 0), 0);
  const needBytes  = folders.reduce((s, f) => s + (f.needBytes  || 0), 0);

  body.innerHTML = `
    <div class="detail-title">${esc(inst.name)}</div>
    <div class="detail-meta">${fmtBytes(totalBytes)} · ${folders.length} folders · ${devices.length} devices</div>

    <div class="detail-section mt-12">
      <div class="detail-section-title">Info</div>
      <div class="detail-row"><span class="detail-key">Status</span><span>${online ? '<span class="chip synced" style="font-size:10px"><span class="chip-dot"></span>Online</span>' : '<span class="chip offline" style="font-size:10px"><span class="chip-dot"></span>Offline</span>'}</span></div>
      ${inst.version ? `<div class="detail-row"><span class="detail-key">Version</span><span class="detail-val">${esc(inst.version)}</span></div>` : ""}
      ${inst.myID    ? `<div class="detail-row"><span class="detail-key">Device ID</span><span class="detail-val mono">${esc(inst.myID.slice(0, 14))}…</span></div>` : ""}
      <div class="detail-row"><span class="detail-key">Outstanding</span><span class="detail-val">${fmtBytes(needBytes)}</span></div>
    </div>

    <div class="detail-tabs">
      <button class="detail-tab active" onclick="detailTab(this,'dt-folders')">Folders</button>
      <button class="detail-tab"        onclick="detailTab(this,'dt-devices')">Devices</button>
    </div>

    <div class="detail-tab-panel active" id="dt-folders">
      ${folders.length ? folders.map(f => `
        <div class="detail-folder-row" onclick="selectFolder('${esc(inst.id)}','${esc(f.id)}'); renderFolderTable();" style="cursor:pointer">
          <div class="detail-folder-icon">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
          </div>
          <div style="flex:1;min-width:0">
            <div class="truncate fw-600 text-sm">${esc(f.label || f.id)}</div>
            <div class="text-xs text-2">${fmtBytes(f.globalBytes)} · ${chip(f.state, f.paused)}</div>
          </div>
        </div>`).join("") : '<div class="text-sm text-2 mt-8">No folders.</div>'}
    </div>

    <div class="detail-tab-panel" id="dt-devices">
      ${devices.length ? devices.map(d => `
        <div class="detail-folder-row">
          <div class="detail-folder-icon" style="background:${d.connected ? "var(--green-lt)" : "var(--border)"}">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="${d.connected ? "var(--green)" : "var(--text-3)"}" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M20 21a8 8 0 1 0-16 0"/></svg>
          </div>
          <div style="flex:1;min-width:0">
            <div class="truncate fw-600 text-sm">${esc(d.name)}</div>
            <div class="text-xs text-2 mono">${esc(d.deviceID.slice(0,10))}…</div>
          </div>
          <div class="flex gap-6">
            ${d.paused
              ? `<button class="btn btn-ghost btn-sm" onclick="actDevice('${esc(inst.id)}','${esc(d.deviceID)}','resume')">Resume</button>`
              : `<button class="btn btn-ghost btn-sm" onclick="actDevice('${esc(inst.id)}','${esc(d.deviceID)}','pause')" title="Pauses all sync with this peer">Pause</button>`}
          </div>
        </div>`).join("") : '<div class="text-sm text-2 mt-8">No devices.</div>'}
    </div>
  `;
}

function renderFolderDetail(inst, folderId) {
  const folder = (inst.folders || []).find(f => f.id === folderId);
  if (!folder) return;
  const body = $("#detail-body");
  const pct  = folder.completion ?? 100;
  const fillCls = pct >= 100 ? "complete" : "";

  body.innerHTML = `
    <div class="detail-title">${esc(folder.label || folder.id)}</div>
    <div class="detail-meta">${fmtBytes(folder.globalBytes)} · on ${esc(inst.name)}</div>

    <div class="mt-12 mb-8">
      ${chip(folder.state, folder.paused)}
    </div>

    <div class="progress-bar" style="height:6px;border-radius:3px">
      <div class="progress-fill ${fillCls}" style="width:${pct}%"></div>
    </div>
    <div class="flex justify-between text-xs text-2 mt-4">
      <span>${fmtBytes(folder.needBytes)} remaining</span>
      <span>${pct}%</span>
    </div>

    <div class="detail-section mt-12">
      <div class="detail-section-title">Details</div>
      <div class="detail-row"><span class="detail-key">Folder ID</span><span class="detail-val mono">${esc(folder.id)}</span></div>
      <div class="detail-row"><span class="detail-key">Path</span><span class="detail-val mono">${esc(folder.path || "—")}</span></div>
      <div class="detail-row"><span class="detail-key">Total size</span><span class="detail-val">${fmtBytes(folder.globalBytes)}</span></div>
      <div class="detail-row"><span class="detail-key">In sync</span><span class="detail-val">${fmtBytes(folder.inSyncBytes)}</span></div>
      <div class="detail-row"><span class="detail-key">Outstanding</span><span class="detail-val">${fmtBytes(folder.needBytes)}</span></div>
      ${folder.pullErrors ? `<div class="detail-row"><span class="detail-key">Pull errors</span><span class="detail-val" style="color:var(--red)">${folder.pullErrors}</span></div>` : ""}
    </div>

    <div class="flex gap-8 mt-12">
      ${folder.paused
        ? `<button class="btn btn-ghost btn-sm" onclick="actFolderDetail('resume','${esc(inst.id)}','${esc(folder.id)}')">Resume</button>`
        : `<button class="btn btn-ghost btn-sm" onclick="actFolderDetail('pause','${esc(inst.id)}','${esc(folder.id)}')" title="Pauses the entire folder — not a single file">Pause</button>`}
      <button class="btn btn-primary btn-sm" onclick="openPushModal(null,'${esc(inst.id)}','${esc(folder.id)}')">Push →</button>
    </div>
  `;
}

async function actFolderDetail(action, instId, folderId) {
  try {
    await api(`api/folders/${instId}/${folderId}/${action}`, { method: "POST" });
    await poll();
  } catch (e) { alert(e.message); }
}

async function actDevice(instId, deviceId, action) {
  try {
    await api(`api/devices/${instId}/${deviceId}/${action}`, { method: "POST" });
    await poll();
  } catch (e) { alert(e.message); }
}

function detailTab(btn, panelId) {
  const body = $("#detail-body");
  body.querySelectorAll(".detail-tab").forEach(t => t.classList.remove("active"));
  body.querySelectorAll(".detail-tab-panel").forEach(p => p.classList.remove("active"));
  btn.classList.add("active");
  const panel = body.querySelector(`#${panelId}`);
  if (panel) panel.classList.add("active");
}

// ── Transfers tab ─────────────────────────────────────────────────────────────
function renderTransfers() {
  if (!transferData) { $("#transfer-stats").innerHTML = '<div class="loading-row">Loading…</div>'; return; }
  const ov = transferData.overall;
  const pct = ov.percent ?? 100;
  const fillCls = pct >= 100 ? "complete" : "";

  $("#transfer-stats").innerHTML = `
    <div class="stat-box">
      <div class="stat-label">Overall</div>
      <div class="stat-value">${pct}%</div>
      <div class="progress-bar mt-4"><div class="progress-fill ${fillCls}" style="width:${pct}%"></div></div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Download</div>
      <div class="stat-value">${fmtSpeed(ov.inSpeedBytesPerSec)}</div>
      <div class="stat-sub">${fmtBytes(ov.needBytes)} remaining</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">Upload</div>
      <div class="stat-value">${fmtSpeed(ov.outSpeedBytesPerSec)}</div>
    </div>
    <div class="stat-box">
      <div class="stat-label">ETA</div>
      <div class="stat-value">${fmtEta(ov.etaSeconds)}</div>
      <div class="stat-sub">estimate</div>
    </div>`;

  let html = "";
  for (const inst of transferData.instances || []) {
    const instName = statusData.find(i => i.id === inst.instanceId)?.name || inst.instanceId;
    if (inst.offline) {
      html += `<div class="file-table-wrap mb-12" style="margin-bottom:12px">
        <div class="file-table-header">${esc(instName)}</div>
        <div class="offline-row" style="padding:12px 20px;color:var(--red)">⚠ Offline</div>
      </div>`;
      continue;
    }
    html += `<div class="file-table-wrap" style="margin-bottom:12px">
      <div class="file-table-header"><span class="section-title">${esc(instName)}</span></div>
      <table class="table"><thead><tr>
        <th style="width:35%">Folder</th>
        <th style="width:15%">Status</th>
        <th style="width:25%">Progress</th>
        <th style="width:15%">Speed <span class="text-3">(approx)</span></th>
        <th style="width:10%">ETA</th>
      </tr></thead><tbody>`;
    for (const f of inst.folders || []) {
      if (f.error) { html += `<tr><td colspan="5" class="offline-row" style="padding:10px 20px">${esc(f.id)}: ${esc(f.error)}</td></tr>`; continue; }
      const pct2 = f.percent ?? 100;
      const fc2  = pct2 >= 100 ? "complete" : "";
      html += `<tr>
        <td><div class="td-name"><div class="td-icon"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg></div>${esc(f.label || f.id)}</div></td>
        <td>${chip(f.state, f.paused)}</td>
        <td><div class="progress-bar"><div class="progress-fill ${fc2}" style="width:${pct2}%"></div></div><div class="text-xs text-2 mt-4">${pct2}% · ${fmtBytes(f.needBytes)} left</div></td>
        <td class="text-sm">${fmtSpeed(f.speedBytesPerSec)}</td>
        <td class="text-sm">${fmtEta(f.etaSeconds)}</td>
      </tr>`;
    }
    html += `</tbody></table></div>`;
  }
  $("#transfer-folders").innerHTML = html;
}

// ── Instances tab ─────────────────────────────────────────────────────────────
function renderInstancesTab() {
  const tbody = $("#instances-tbody");
  if (!instances.length) {
    tbody.innerHTML = `<tr><td colspan="5"><div class="empty-state" style="padding:40px 20px">
      <h3>No instances</h3>
      <p>Add your first Syncthing instance to get started.</p>
      <button class="btn btn-primary" onclick="openAddInstance()">Add Instance</button>
    </div></td></tr>`;
    return;
  }

  tbody.innerHTML = instances.map(inst => {
    const s = statusData.find(i => i.id === inst.id);
    const online = s?.online;
    const folderCount = s?.folders?.length ?? "—";
    const shortId = s?.myID ? s.myID.slice(0, 12) + "…" : "—";
    const locked = inst.source === "config";
    const statusChip = online === true
      ? `<span class="chip synced" style="font-size:10px"><span class="chip-dot"></span>Online</span>`
      : online === false
        ? `<span class="chip error" style="font-size:10px"><span class="chip-dot"></span>Offline</span>`
        : `<span class="chip offline" style="font-size:10px"><span class="chip-dot"></span>—</span>`;

    return `<tr onclick="selectInstance('${esc(inst.id)}'); switchTab('overview')">
      <td>
        <div class="td-name">
          <div class="td-icon ${online === false ? "offline" : ""}">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="${online === false ? "var(--red)" : "var(--accent)"}" stroke-width="2">
              <ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>
            </svg>
          </div>
          <div>
            <div class="flex items-center gap-8">${esc(inst.name)} ${locked ? '<span class="badge-config">config</span>' : ""}</div>
            <div class="td-meta mono">${esc(inst.url)}</div>
          </div>
        </div>
      </td>
      <td>${statusChip}</td>
      <td class="text-sm">${folderCount}</td>
      <td class="text-sm mono text-2">${shortId}</td>
      <td>
        <div class="flex gap-6 justify-end" onclick="event.stopPropagation()">
          <button class="btn btn-ghost btn-sm" onclick="testInstance('${esc(inst.id)}')">Test</button>
          ${!locked ? `<button class="btn btn-ghost btn-sm" onclick="openEditInstance('${esc(inst.id)}')">Edit</button>` : ""}
          ${!locked ? `<button class="btn btn-danger btn-sm" onclick="deleteInstance('${esc(inst.id)}')">Delete</button>` : ""}
        </div>
      </td>
    </tr>`;
  }).join("");
}

// ── Instance wizard ───────────────────────────────────────────────────────────
let _editingId   = null;
let _wizInstStep = 1;
let _wizInstTestResult = null;

function _wizInstSetStep(n) {
  _wizInstStep = n;
  [1,2,3].forEach(i => {
    $(`#wiz-inst-s${i}`)?.classList.toggle("hidden", i !== n);
    const dot = $(`.wstep[data-s="${i}"]`, $("#wiz-inst-steps"));
    if (dot) {
      dot.classList.toggle("active", i === n);
      dot.classList.toggle("done",   i < n);
    }
  });
  $("#wiz-inst-back").style.display = n > 1 ? "" : "none";
  const nextBtn = $("#wiz-inst-next");
  if (n === 1) { nextBtn.textContent = "Test Connection →"; nextBtn.disabled = false; }
  if (n === 2) { nextBtn.textContent = _wizInstTestResult?.ok ? "Next →" : "Retry"; nextBtn.disabled = false; }
  if (n === 3) { nextBtn.textContent = _editingId ? "Save Changes" : "Add Instance"; nextBtn.disabled = false; }
}

function openAddInstance() {
  _editingId = null;
  _wizInstTestResult = null;
  $("#modal-inst-title").textContent = "Add Instance";
  $("#modal-inst-name").value = "";
  $("#modal-inst-url").value  = "";
  $("#modal-inst-key").value  = "";
  $("#modal-inst-error").classList.add("hidden");
  _wizInstSetStep(1);
  $("#modal-inst").classList.remove("hidden");
  setTimeout(() => $("#modal-inst-name").focus(), 60);
}

function openEditInstance(id) {
  const inst = instances.find(i => i.id === id);
  if (!inst) return;
  _editingId = id;
  _wizInstTestResult = null;
  $("#modal-inst-title").textContent = "Edit Instance";
  $("#modal-inst-name").value = inst.name;
  $("#modal-inst-url").value  = inst.url;
  $("#modal-inst-key").value  = "";
  $("#modal-inst-error").classList.add("hidden");
  _wizInstSetStep(1);
  $("#modal-inst").classList.remove("hidden");
}

async function wizInstNext() {
  $("#modal-inst-error").classList.add("hidden");
  if (_wizInstStep === 1) {
    const name = $("#modal-inst-name").value.trim();
    const url  = $("#modal-inst-url").value.trim();
    const key  = $("#modal-inst-key").value.trim();
    if (!name || !url || !key) { showErr("modal-inst-error", "All fields required."); return; }
    // Run connection test
    const btn = $("#wiz-inst-next");
    btn.disabled = true;
    btn.textContent = "Testing…";
    try {
      // Save temporarily so we can call /test (for edit) or test directly
      let testId = _editingId;
      if (!testId) {
        // Temporarily save to test, then we'll confirm on step 3
        // Instead call test inline via a temporary approach — just POST to test endpoint
        // by saving then testing then deleting if user cancels. Simpler: call the Syncthing
        // status endpoint directly via the backend test helper by temporarily registering.
        // Easiest: just call test with the current values inline.
      }
      const r = await api("api/instances/_wizard_test", {
        method: "POST",
        body: { url, api_key: key },
      });
      _wizInstTestResult = r;
    } catch (e) {
      _wizInstTestResult = { ok: false, error: e.message };
    }
    const r = _wizInstTestResult;
    const isOk = r.reachable && r.myID;
    $("#wiz-inst-test-result").innerHTML = `
      <div class="test-result">
        <div class="test-result-icon ${isOk ? "ok" : "fail"}">${isOk ? "✓" : "✗"}</div>
        <div class="test-result-title">${isOk ? "Connected successfully" : "Could not connect"}</div>
        <div class="test-result-meta">${isOk
          ? `Device ID: <span class="mono">${esc(r.myID)}</span><br>Version: ${esc(r.version || "?")}`
          : esc(r.error || "Unknown error")}</div>
      </div>`;
    _wizInstSetStep(2);
  } else if (_wizInstStep === 2) {
    if (!_wizInstTestResult?.reachable || !_wizInstTestResult?.myID) {
      // Retry — go back to step 1
      _wizInstSetStep(1);
      return;
    }
    // Show confirm summary
    const name = $("#modal-inst-name").value.trim();
    const url  = $("#modal-inst-url").value.trim();
    const myID = _wizInstTestResult.myID;
    $("#wiz-inst-summary").innerHTML = `
      <div class="confirm-summary">
        <div class="confirm-row"><span class="confirm-key">Name</span><span class="confirm-val">${esc(name)}</span></div>
        <div class="confirm-row"><span class="confirm-key">URL</span><span class="confirm-val">${esc(url)}</span></div>
        <div class="confirm-row"><span class="confirm-key">Device ID</span><span class="confirm-val">${esc(myID)}</span></div>
        <div class="confirm-row"><span class="confirm-key">Version</span><span class="confirm-val">${esc(_wizInstTestResult.version || "?")}</span></div>
      </div>
      <div class="alert alert-ok mt-8">Ready to ${_editingId ? "update" : "add"} this instance.</div>`;
    _wizInstSetStep(3);
  } else if (_wizInstStep === 3) {
    const name = $("#modal-inst-name").value.trim();
    const url  = $("#modal-inst-url").value.trim();
    const key  = $("#modal-inst-key").value.trim();
    try {
      if (_editingId) {
        await api(`api/instances/${_editingId}`, { method: "PUT", body: { name, url, api_key: key } });
      } else {
        await api("api/instances", { method: "POST", body: { name, url, api_key: key } });
      }
      closeModal("modal-inst");
      await loadInstances();
      await poll();
    } catch (e) { showErr("modal-inst-error", e.message); }
  }
}

function wizInstBack() {
  _wizInstSetStep(_wizInstStep - 1);
}

async function testInstance(id) {
  try {
    const r = await api(`api/instances/${id}/test`, { method: "POST" });
    alert(r.reachable && r.myID
      ? `✓ Connected\nDevice ID: ${r.myID}\nVersion: ${r.version || "?"}`
      : `✗ ${r.error || "Could not connect"}`);
  } catch (e) { alert(e.message); }
}

async function deleteInstance(id) {
  if (!confirm("Remove this instance from Replicarr? Syncthing is not affected.")) return;
  try {
    await api(`api/instances/${id}`, { method: "DELETE" });
    await loadInstances();
    await poll();
  } catch (e) { alert(e.message); }
}

// ── Add folder wizard ─────────────────────────────────────────────────────────
let _addFolderInstId  = null;
let _wizFolderStep    = 1;
let _storageData      = null;

function _wizFolderSetStep(n) {
  _wizFolderStep = n;
  [1,2,3].forEach(i => {
    $(`#wiz-folder-s${i}`)?.classList.toggle("hidden", i !== n);
    const dot = $(`.wstep[data-s="${i}"]`, $("#wiz-folder-steps"));
    if (dot) {
      dot.classList.toggle("active", i === n);
      dot.classList.toggle("done",   i < n);
    }
  });
  $("#wiz-folder-back").style.display = n > 1 ? "" : "none";
  const btn = $("#wiz-folder-next");
  if (n === 1) { btn.textContent = "Choose Location →"; btn.disabled = false; }
  if (n === 2) { btn.textContent = "Next →";            btn.disabled = false; }
  if (n === 3) { btn.textContent = "Add Folder";        btn.disabled = false; }
}

async function openAddFolder(instId) {
  _addFolderInstId = instId;
  const inst = statusData.find(i => i.id === instId);
  $("#modal-folder-inst").textContent = inst?.name || instId;
  $("#modal-folder-id").value    = "";
  $("#modal-folder-label").value = "";
  $("#modal-folder-path").value  = "";
  $("#modal-folder-error").classList.add("hidden");
  _wizFolderSetStep(1);
  $("#modal-folder").classList.remove("hidden");
  setTimeout(() => $("#modal-folder-id").focus(), 60);

  // Pre-fetch storage in background so step 2 is instant
  _storageData = null;
  api("api/storage").then(d => { _storageData = d; }).catch(() => {});
}

async function wizFolderNext() {
  $("#modal-folder-error").classList.add("hidden");
  if (_wizFolderStep === 1) {
    if (!$("#modal-folder-id").value.trim()) {
      showErr("modal-folder-error", "Folder ID is required.");
      return;
    }
    await _renderStoragePicker();
    _wizFolderSetStep(2);
  } else if (_wizFolderStep === 2) {
    if (!$("#modal-folder-path").value.trim()) {
      showErr("modal-folder-error", "Select or enter a path.");
      return;
    }
    _renderFolderConfirm();
    _wizFolderSetStep(3);
  } else if (_wizFolderStep === 3) {
    const folder_id = $("#modal-folder-id").value.trim();
    const label     = $("#modal-folder-label").value.trim();
    const path      = $("#modal-folder-path").value.trim();
    try {
      const r = await api(`api/instances/${_addFolderInstId}/folders`, {
        method: "POST",
        body: { folder_id, label: label || folder_id, path },
      });
      closeModal("modal-folder");
      if (r?.restartRequired) alert("Syncthing requires a restart to apply changes.");
      await poll();
    } catch (e) { showErr("modal-folder-error", e.message); }
  }
}

function wizFolderBack() {
  _wizFolderSetStep(_wizFolderStep - 1);
}

async function _renderStoragePicker() {
  const el = $("#storage-picker");
  if (!_storageData) {
    el.innerHTML = '<div class="loading-row">Loading…</div>';
    try { _storageData = await api("api/storage"); } catch(e) {
      el.innerHTML = `<div class="alert alert-error">Could not load storage: ${esc(e.message)}</div>`;
      return;
    }
  }
  if (!_storageData.length) {
    el.innerHTML = '<div class="alert alert-info">No mounted shares detected. Enter a path manually below.</div>';
    return;
  }

  const folderId = $("#modal-folder-id").value.trim();

  el.innerHTML = _storageData.map(s => `
    <div class="storage-root" id="sr-${esc(s.path.replace(/\//g,'-'))}">
      <div class="storage-root-header" onclick="toggleStorageRoot(this)">
        <div class="storage-root-icon">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        </div>
        <div style="flex:1;min-width:0">
          <div class="storage-root-label">${esc(s.label)}</div>
          <div class="storage-root-desc">${esc(s.description)}</div>
        </div>
        <span class="storage-root-path">${esc(s.path)}</span>
        <svg class="storage-root-chevron" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
      </div>
      <div class="storage-subdirs">
        <div class="storage-use-root" onclick="pickPath('${esc(s.path)}')">
          Use root: <span class="mono">${esc(s.path)}</span>
        </div>
        ${s.subdirs.map(d => `
          <div class="storage-subdir" onclick="pickPath('${esc(d)}')">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
            ${esc(d)}
          </div>`).join("")}
        ${folderId ? `
          <div class="storage-subdir" onclick="pickPath('${esc(s.path)}/${esc(folderId)}')">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
            Create: <span class="mono">${esc(s.path)}/${esc(folderId)}</span>
          </div>` : ""}
      </div>
    </div>`).join("");
}

function toggleStorageRoot(header) {
  header.closest(".storage-root").classList.toggle("open");
}

function pickPath(path) {
  $("#modal-folder-path").value = path;
  // Highlight selected
  $$(".storage-subdir, .storage-use-root").forEach(el => el.classList.remove("selected"));
  // Find and mark selected
  $$(".storage-subdir, .storage-use-root").forEach(el => {
    if (el.getAttribute("onclick")?.includes(`'${path}'`)) el.classList.add("selected");
  });
}

function _renderFolderConfirm() {
  const folder_id = $("#modal-folder-id").value.trim();
  const label     = $("#modal-folder-label").value.trim() || folder_id;
  const path      = $("#modal-folder-path").value.trim();
  const instName  = statusData.find(i => i.id === _addFolderInstId)?.name || _addFolderInstId;
  $("#wiz-folder-summary").innerHTML = `
    <div class="confirm-summary">
      <div class="confirm-row"><span class="confirm-key">Instance</span><span class="confirm-val">${esc(instName)}</span></div>
      <div class="confirm-row"><span class="confirm-key">Folder ID</span><span class="confirm-val">${esc(folder_id)}</span></div>
      <div class="confirm-row"><span class="confirm-key">Label</span><span class="confirm-val">${esc(label)}</span></div>
      <div class="confirm-row"><span class="confirm-key">Path</span><span class="confirm-val">${esc(path)}</span></div>
    </div>
    <div class="alert alert-info mt-8">
      Syncthing will create this folder at the path above. Make sure the path is writable inside the container.
    </div>`;
}

// ── Push ──────────────────────────────────────────────────────────────────────
let _pushSrcInstId = null;
let _pushFolderId  = null;

function openPushModal(e, instId, folderId) {
  if (e) e.stopPropagation();
  const targets = instances.filter(i => i.id !== instId);
  if (!targets.length) {
    alert("No other instances to push to. Add a second Syncthing instance first.");
    return;
  }
  _pushSrcInstId = instId;
  _pushFolderId  = folderId;
  const inst   = statusData.find(i => i.id === instId);
  const folder = inst?.folders?.find(f => f.id === folderId);
  $("#modal-push-folder").textContent = folder?.label || folderId;
  $("#modal-push-source").textContent = inst?.name || instId;
  const sel = $("#modal-push-target");
  sel.innerHTML = targets.map(i => `<option value="${esc(i.id)}">${esc(i.name)}</option>`).join("");
  $("#modal-push-path").value = "";
  $("#modal-push-steps").classList.add("hidden");
  $("#modal-push-steps").innerHTML = "";
  $("#modal-push-error").classList.add("hidden");
  $("#modal-push-btn").disabled = false;
  $("#modal-push").classList.remove("hidden");
}

async function executePush() {
  const targetId   = $("#modal-push-target").value;
  const targetPath = $("#modal-push-path").value.trim();
  if (!targetPath) { showErr("modal-push-error", "Target path required."); return; }
  $("#modal-push-btn").disabled = true;
  $("#modal-push-error").classList.add("hidden");
  try {
    const r = await api(`api/folders/${_pushSrcInstId}/${_pushFolderId}/push`, {
      method: "POST",
      body: { target_instance_id: targetId, target_path: targetPath },
    });
    const stepsEl = $("#modal-push-steps");
    stepsEl.classList.remove("hidden");
    stepsEl.innerHTML = `<ul class="step-list">${r.steps.map(s => `
      <li class="step-item ${s.ok ? "step-ok" : "step-fail"}">
        <div class="step-icon">${s.ok ? "✓" : "✗"}</div>
        <div>
          <div>${esc(s.description)}</div>
          ${s.error ? `<div class="text-xs text-2">${esc(s.error)}</div>` : ""}
          ${s.sourceRestartRequired ? `<div class="text-xs" style="color:var(--amber)">⚠ Source restart required</div>` : ""}
          ${s.targetRestartRequired ? `<div class="text-xs" style="color:var(--amber)">⚠ Target restart required</div>` : ""}
        </div>
      </li>`).join("")}</ul>`;
    if (!r.ok) showErr("modal-push-error", "Push stopped — see steps above.");
    else await poll();
  } catch (e) { showErr("modal-push-error", e.message); }
  finally { $("#modal-push-btn").disabled = false; }
}

// ── Helpers ───────────────────────────────────────────────────────────────────
function showErr(id, msg) {
  const el = $(`#${id}`);
  el.textContent = msg;
  el.classList.remove("hidden");
}
function closeModal(id) { $(`#${id}`).classList.add("hidden"); }

async function loadInstances() {
  instances = await api("api/instances");
  updateSidebarCount();
  renderInstancesTab();
}

// ── Dark mode ─────────────────────────────────────────────────────────────────
function applyTheme(dark) {
  document.documentElement.classList.toggle("dark", dark);
}
function toggleTheme() {
  const dark = !document.documentElement.classList.contains("dark");
  applyTheme(dark);
  localStorage.setItem("replicarr-theme", dark ? "dark" : "light");
}

// ── Boot ──────────────────────────────────────────────────────────────────────
(async () => {
  // Apply saved theme before first render to avoid flash
  const saved = localStorage.getItem("replicarr-theme");
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(saved ? saved === "dark" : prefersDark);

  await loadInstances();
  await poll();
  switchTab("overview");
  // Start polling only after boot to avoid race conditions
  setInterval(poll, 3000);
})();

// Expose for inline handlers (includes renderFolderTable used in detail panel onclick strings)
Object.assign(window, {
  toggleTheme, applyFilter,
  switchTab, selectInstance, selectFolder, renderFolderTable, closeDetail, detailTab,
  actFolder, actFolderDetail, actDevice,
  openAddInstance, openEditInstance, wizInstNext, wizInstBack,
  deleteInstance, testInstance,
  openAddFolder, wizFolderNext, wizFolderBack, toggleStorageRoot, pickPath,
  openPushModal, executePush,
  closeModal, poll,
});
