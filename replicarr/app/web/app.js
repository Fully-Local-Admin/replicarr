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
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}
// Folder/device IDs (and, indirectly, folder labels or device names) can
// originate from a remote Syncthing peer, not just this add-on's own store.
// esc() alone is not enough to safely embed them inside inline onclick="..."
// handlers: the browser HTML-decodes attribute text *before* compiling it as
// JS, so an escaped quote just becomes a literal quote again right where the
// JS parser reads it, breaking out of the string. These values must instead
// be passed via data-* attributes and read back with `this.dataset`.

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
let subfolderTransfersData = []; // from /api/subfolder-transfers
let folderOrders = {};    // { instance_id: [folder_id, ...] }, persisted server-side
let selectedInstId  = null;
let selectedFolderId = null;
let _folderDragActive = false;
let _transferTab = "progress";

// ── Polling ───────────────────────────────────────────────────────────────────
// An SSE-pushed live feed briefly replaced this polling loop, but it kept
// causing the add-on to hang on shutdown/restart (a long-lived streaming
// connection is something uvicorn's graceful shutdown can end up waiting on
// indefinitely) and was removed after repeated live failures. Every request
// here completes in milliseconds, so there's nothing for a shutdown to hang
// on. Don't reintroduce a long-lived connection without a way to verify the
// fix against a real restart — see CHANGELOG 0.3.0-0.3.3.
async function poll() {
  try {
    [statusData, transferData, subfolderTransfersData] = await Promise.all([
      api("api/status"),
      api("api/transfers"),
      api("api/subfolder-transfers"),
    ]);
    applyPoll();
  } catch (e) {
    console.warn("Poll error:", e);
  }
}

function applyPoll() {
  if (activeTab === "overview")   renderOverview();
  if (activeTab === "transfers")  renderTransfers();
  if (selectedInstId)             updateDetailPanel();
}

// ── Tab routing ───────────────────────────────────────────────────────────────
function switchTab(tab) {
  activeTab = tab;
  $$(".topbar-tab").forEach(el => {
    el.classList.toggle("active", el.dataset.tab === tab);
  });
  $$(".tab-panel").forEach(el =>
    el.classList.toggle("hidden", el.dataset.panel !== tab)
  );
  if (tab === "overview")   renderOverview();
  if (tab === "transfers")  renderTransfers();
}

// ── Overview tab ──────────────────────────────────────────────────────────────
function renderOverview() {
  renderProblemsBanner();
  renderQuickCards();
  renderFolderTable();
}

function renderProblemsBanner() {
  const el = $("#problems-banner");
  const offlineInstances = statusData.filter(i => i.online === false);
  const folderErrors = statusData.flatMap(i =>
    (i.folders || []).filter(f => f.error || f.state === "error" || f.pullErrors)
      .map(f => ({ inst: i, folder: f }))
  );
  const disconnectedDevices = statusData.flatMap(i =>
    (i.devices || []).filter(d => !d.paused && !d.connected)
      .map(d => ({ inst: i, device: d }))
  );

  const parts = [];
  if (offlineInstances.length) {
    parts.push(problemBannerItem(
      `${offlineInstances.length} instance${offlineInstances.length !== 1 ? "s" : ""} offline`,
      "Replicarr cannot reach this Syncthing instance, so its folders and devices cannot be refreshed.",
      "Check that Syncthing is running, then verify its API URL, API key, VPN/network route, and GUI/API port 8384.",
      "Open instance",
      "instances",
    ));
  }
  if (folderErrors.length) {
    parts.push(problemBannerItem(
      `${folderErrors.length} folder${folderErrors.length !== 1 ? "s" : ""} with errors`,
      "Syncthing reports an error or failed file pulls for one or more main folders.",
      "Open the affected folder, review its error details, and check its path, permissions, available space, pause state, and remote peer.",
      "Open affected folder",
      "folders",
    ));
  }
  if (disconnectedDevices.length) {
    parts.push(problemBannerItem(
      `${disconnectedDevices.length} device${disconnectedDevices.length !== 1 ? "s" : ""} disconnected`,
      "A configured Syncthing peer is not currently connected, so files cannot sync with that device.",
      "Make sure the remote device is awake and running Syncthing, then check its VPN/network connection and Syncthing sync port 22000.",
      "Open Devices",
      "devices",
    ));
  }

  if (!parts.length) {
    el.classList.add("hidden");
    el.innerHTML = "";
    delete el.dataset.problemSignature;
    return;
  }
  el.classList.remove("hidden");
  const signature = [
    ...offlineInstances.map(inst => `i:${inst.id}`),
    ...folderErrors.map(({ inst, folder }) => `f:${inst.id}:${folder.id}`),
    ...disconnectedDevices.map(({ inst, device }) => `d:${inst.id}:${device.deviceID}`),
  ].join("|");
  const html = `<div class="alert alert-error problems-banner">
    <span class="problems-warning">⚠</span>
    ${parts.map((part, index) => `${index ? '<span class="problem-separator">·</span>' : ""}${part}`).join("")}
  </div>`;
  if (el.dataset.problemSignature !== signature) {
    el.innerHTML = html;
    el.dataset.problemSignature = signature;
  }
}

function problemBannerItem(label, meaning, resolution, actionLabel, action) {
  return `<span class="problem-item">
    <span>${label}</span>
    <button class="problem-info" type="button" aria-label="Help: ${esc(label)}"
            onmouseenter="openProblemTooltip(this)" onmouseleave="scheduleProblemTooltipClose(this)">i</button>
    <span class="problem-tooltip" role="tooltip"
          onmouseenter="openProblemTooltip(this)" onmouseleave="scheduleProblemTooltipClose(this)">
      <strong>What it means</strong>
      <span>${esc(meaning)}</span>
      <strong>How to resolve it</strong>
      <span>${esc(resolution)}</span>
      <button class="btn btn-primary btn-sm" type="button" onclick="openProblemResolution('${action}')">${actionLabel} →</button>
    </span>
  </span>`;
}

let _problemTooltipCloseTimer = null;

function openProblemTooltip(element) {
  clearTimeout(_problemTooltipCloseTimer);
  const item = element.closest(".problem-item");
  $$(".problem-item.tooltip-open").forEach(openItem => {
    if (openItem !== item) openItem.classList.remove("tooltip-open");
  });
  item?.classList.add("tooltip-open");
}

function scheduleProblemTooltipClose(element) {
  clearTimeout(_problemTooltipCloseTimer);
  const item = element.closest(".problem-item");
  _problemTooltipCloseTimer = setTimeout(() => item?.classList.remove("tooltip-open"), 350);
}

function openProblemResolution(type) {
  if (type === "instances") {
    const affected = statusData.find(inst => !inst.online);
    if (affected) openInstanceManagement(affected.id);
    return;
  }

  if (type === "folders") {
    const affected = statusData.flatMap(inst =>
      (inst.folders || [])
        .filter(folder => folder.error || folder.state === "error" || folder.pullErrors)
        .map(folder => ({ inst, folder }))
    )[0];
    if (!affected) return;
    switchTab("overview");
    selectedInstId = affected.inst.id;
    selectedFolderId = affected.folder.id;
    renderQuickCards();
    renderFolderTable();
    openDetailPanel("folder", affected.inst.id, affected.folder.id);
    return;
  }

  const affected = statusData.flatMap(inst =>
    (inst.devices || [])
      .filter(device => !device.paused && !device.connected)
      .map(device => ({ inst, device }))
  )[0];
  if (!affected) return;
  switchTab("overview");
  selectedInstId = affected.inst.id;
  selectedFolderId = null;
  renderQuickCards();
  renderFolderTable();
  openDetailPanel("instance", affected.inst.id);
  const devicesTab = [...$("#detail-body").querySelectorAll(".detail-tab")]
    .find(tab => tab.textContent.trim() === "Devices");
  if (devicesTab) detailTab(devicesTab, "dt-devices");
  const deviceRow = [...$("#dt-devices").querySelectorAll("[data-device-id]")]
    .find(row => row.dataset.deviceId === affected.device.deviceID);
  if (deviceRow) {
    deviceRow.classList.add("problem-highlight");
    deviceRow.scrollIntoView({ behavior: "smooth", block: "center" });
    setTimeout(() => deviceRow.classList.remove("problem-highlight"), 3000);
  }
}

function renderQuickCards() {
  const el = $("#quick-cards");
  const visible = statusData;
  if (!statusData.length) {
    el.innerHTML = `<div class="loading-row" style="grid-column:1/-1">
      No instances. <button class="btn btn-primary btn-sm" onclick="openAddInstance()">Add Instance</button>
    </div>`;
    return;
  }

  el.innerHTML = visible.map(inst => {
    const online = inst.online;
    const folderCount = inst.folders?.length ?? 0;
    const totalBytes = (inst.folders || []).reduce((s, f) => s + (f.globalBytes || 0), 0);
    const iconCls = online ? "" : "offline";
    const selCls  = selectedInstId === inst.id ? "selected" : "";

    return `<div class="quick-card ${selCls}" data-instance-id="${esc(inst.id)}" onclick="selectInstance(this.dataset.instanceId)">
      <button class="quick-card-edit" type="button" title="Edit instance" aria-label="Edit ${esc(inst.name)}"
              onclick="event.stopPropagation(); openInstanceManagement(this.closest('.quick-card').dataset.instanceId)">
        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 20h9"/><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z"/>
        </svg>
      </button>
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

function openInstanceManagement(id) {
  const configured = instances.find(inst => inst.id === id);
  const status = statusData.find(inst => inst.id === id);
  if (!configured) return;

  selectedInstId = id;
  selectedFolderId = null;
  renderQuickCards();
  renderFolderTable();
  if (!$("#detail-panel").classList.contains("hidden")) {
    updateDetailPanel("instance", id);
  }

  const locked = configured.source === "config";
  const folderCount = status?.folders?.length ?? "—";
  const statusChip = status?.online === true
    ? '<span class="chip synced" style="font-size:10px"><span class="chip-dot"></span>Online</span>'
    : status?.online === false
      ? '<span class="chip error" style="font-size:10px"><span class="chip-dot"></span>Offline</span>'
      : '<span class="chip offline" style="font-size:10px"><span class="chip-dot"></span>Unknown</span>';

  $("#instance-manage-body").innerHTML = `
    <div class="flex items-center gap-8 mb-12">
      <span class="detail-title">${esc(configured.name)}</span>
      ${locked ? '<span class="badge-config">config locked</span>' : ""}
    </div>
    <div class="confirm-summary">
      <div class="confirm-row"><span class="confirm-key">Status</span><span class="confirm-val">${statusChip}</span></div>
      <div class="confirm-row"><span class="confirm-key">Configured API URL</span><span class="confirm-val">${esc(configured.url)}</span></div>
      <div class="confirm-row"><span class="confirm-key">Folders</span><span class="confirm-val">${folderCount}</span></div>
      <div class="confirm-row"><span class="confirm-key">Device ID</span><span class="confirm-val">${esc(status?.myID || "—")}</span></div>
      <div class="confirm-row"><span class="confirm-key">Version</span><span class="confirm-val">${esc(status?.version || "—")}</span></div>
    </div>
    ${locked ? '<div class="alert alert-info mt-12">This instance is defined in the Home Assistant add-on configuration. Edit or remove it from the add-on Configuration page.</div>' : ""}
    <div class="flex gap-8 justify-end mt-12">
      <button class="btn btn-ghost" onclick="testInstance('${esc(id)}')">Test</button>
      ${!locked ? `<button class="btn btn-ghost" onclick="closeModal('modal-instance-manage'); openEditInstance('${esc(id)}')">Edit</button>` : ""}
      ${!locked ? `<button class="btn btn-danger" onclick="deleteInstance('${esc(id)}')">Delete</button>` : ""}
    </div>`;
  $("#modal-instance-manage").classList.remove("hidden");
}

function renderFolderTable() {
  if (_folderDragActive) return;
  const inst = statusData.find(i => i.id === selectedInstId);
  const bc = $("#breadcrumb");
  const tbody = $("#folder-tbody");

  if (!inst) {
    bc.innerHTML = `<span class="breadcrumb-part current">Select an instance above</span>`;
    $("#btn-add-folder").classList.add("hidden");
    tbody.innerHTML = `<tr><td colspan="5" class="loading-row">Select an instance to view its folders.</td></tr>`;
    return;
  }

  bc.innerHTML = `
    <span class="breadcrumb-part" onclick="selectInstance('${esc(inst.id)}')">${esc(inst.name)}</span>
    <span class="breadcrumb-sep">›</span>
    <span class="breadcrumb-part current">Folders</span>`;

  $("#btn-add-folder").classList.remove("hidden");
  $("#btn-add-folder").onclick = () => openAddFolder(inst.id);

  if (!inst.online) {
    tbody.innerHTML = `<tr class="offline-row"><td colspan="5" style="padding:14px 20px">⚠ ${esc(inst.error || "Instance offline")}</td></tr>`;
    return;
  }

  const originalFolders = inst.folders || [];
  const savedOrder = folderOrders[inst.id] || [];
  const savedRanks = new Map(savedOrder.map((id, index) => [id, index]));
  const originalRanks = new Map(originalFolders.map((folder, index) => [folder.id, index]));
  const folders = [...originalFolders].sort((a, b) => {
    const aRank = savedRanks.has(a.id) ? savedRanks.get(a.id) : savedOrder.length + originalRanks.get(a.id);
    const bRank = savedRanks.has(b.id) ? savedRanks.get(b.id) : savedOrder.length + originalRanks.get(b.id);
    return aRank - bRank;
  });
  if (!folders.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="loading-row">No folders on this instance.</td></tr>`;
    return;
  }

  tbody.innerHTML = folders.map(f => {
    const dragHandle = `
      <span class="folder-drag-handle" draggable="true"
            title="Drag to reorder"
            onclick="event.stopPropagation()"
            ondragstart="startFolderDrag(event)"
            ondragend="finishFolderDrag(event)">
        <svg width="10" height="16" viewBox="0 0 10 16" fill="currentColor" aria-hidden="true">
          <circle cx="2" cy="3" r="1.2"/><circle cx="8" cy="3" r="1.2"/>
          <circle cx="2" cy="8" r="1.2"/><circle cx="8" cy="8" r="1.2"/>
          <circle cx="2" cy="13" r="1.2"/><circle cx="8" cy="13" r="1.2"/>
        </svg>
      </span>`;
    if (f.error) {
      return `<tr data-folder-id="${esc(f.id)}" data-instance-id="${esc(inst.id)}"
                  ondragover="dragOverFolderRow(event)">
        <td colspan="5" class="offline-row" style="padding:11px 20px">
        <span class="flex items-center gap-8">${dragHandle}<span><span class="mono">${esc(f.id)}</span> — ${esc(f.error)}</span></span>
      </td></tr>`;
    }
    const pct  = f.completion ?? 100;
    const fillCls = pct >= 100 ? "complete" : "";
    const selCls  = selectedFolderId === f.id ? "selected" : "";
    const chipHtml = chip(f.state, f.paused);

    return `<tr class="${selCls}" data-folder-id="${esc(f.id)}" data-instance-id="${esc(inst.id)}"
               ondragover="dragOverFolderRow(event)"
               onclick="selectFolder('${esc(inst.id)}', this.dataset.folderId)">
      <td>
        <div class="td-name">
          ${dragHandle}
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
            ? `<button class="btn btn-ghost btn-sm" title="Resume folder" onclick="actFolder(event,'resume','${esc(inst.id)}',this.closest('tr').dataset.folderId)">Resume</button>`
            : `<button class="btn btn-ghost btn-sm" title="Pause folder — stops entire folder sync" onclick="actFolder(event,'pause','${esc(inst.id)}',this.closest('tr').dataset.folderId)">Pause</button>`}
          <button class="btn btn-danger btn-sm" title="Remove folder from Syncthing" onclick="removeFolder(event,'${esc(inst.id)}',this.closest('tr').dataset.folderId)">Remove</button>
        </div>
      </td>
    </tr>`;
  }).join("");
}

function startFolderDrag(event) {
  const row = event.currentTarget.closest("tr");
  _folderDragActive = true;
  row.classList.add("folder-dragging");
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", row.dataset.folderId);
}

function dragOverFolderRow(event) {
  event.preventDefault();
  const target = event.currentTarget;
  const dragging = $("#folder-tbody .folder-dragging");
  if (!dragging || dragging === target) return;
  const insertAfter = event.clientY > target.getBoundingClientRect().top + target.offsetHeight / 2;
  target.parentElement.insertBefore(dragging, insertAfter ? target.nextSibling : target);
}

async function finishFolderDrag(event) {
  const row = event.currentTarget.closest("tr");
  const instId = row.dataset.instanceId;
  row.classList.remove("folder-dragging");
  _folderDragActive = false;
  const folderIds = [...$("#folder-tbody").querySelectorAll("tr[data-folder-id]")]
    .map(folderRow => folderRow.dataset.folderId);
  folderOrders[instId] = folderIds;
  try {
    await api(`api/folder-orders/${instId}`, {
      method: "PUT",
      body: { folder_ids: folderIds },
    });
  } catch (e) {
    await loadFolderOrders();
    renderFolderTable();
    alert(`Could not save folder order: ${e.message}`);
  }
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
  syncDetailToggle();
  updateDetailPanel(type, instId, folderId);
}

function closeDetail() {
  $("#detail-panel").classList.add("hidden");
  syncDetailToggle();
  selectedFolderId = null;
  renderFolderTable();
}

function syncDetailToggle() {
  const open = !$("#detail-panel").classList.contains("hidden");
  const button = $("#btn-detail-toggle");
  button.classList.toggle("active", open);
  button.setAttribute("aria-expanded", String(open));
}

function toggleDetailPanel() {
  const panel = $("#detail-panel");
  if (!panel.classList.contains("hidden")) {
    panel.classList.add("hidden");
    syncDetailToggle();
    return;
  }
  if (!selectedInstId) return;
  openDetailPanel(selectedFolderId ? "folder" : "instance", selectedInstId, selectedFolderId);
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
  const sameInstance = body.dataset.instanceId === inst.id;
  const activeTab = sameInstance
    ? (body.dataset.instanceTab || "dt-folders")
    : "dt-folders";
  const scrollTop = sameInstance ? body.scrollTop : 0;
  body.dataset.instanceId = inst.id;
  body.dataset.instanceTab = activeTab;
  delete body.dataset.folderKey; // invalidate renderFolderDetail's same-folder check
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
      <button class="detail-tab ${activeTab === "dt-folders" ? "active" : ""}" onclick="detailTab(this,'dt-folders')">Folders</button>
      <button class="detail-tab ${activeTab === "dt-devices" ? "active" : ""}" onclick="detailTab(this,'dt-devices')">Devices</button>
    </div>

    <div class="detail-tab-panel ${activeTab === "dt-folders" ? "active" : ""}" id="dt-folders">
      ${folders.length ? folders.map(f => `
        <div class="detail-folder-row" data-folder-id="${esc(f.id)}" onclick="selectFolder('${esc(inst.id)}',this.dataset.folderId); renderFolderTable();" style="cursor:pointer">
          <div class="detail-folder-icon">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
          </div>
          <div style="flex:1;min-width:0">
            <div class="truncate fw-600 text-sm">${esc(f.label || f.id)}</div>
            <div class="text-xs text-2">${fmtBytes(f.globalBytes)} · ${chip(f.state, f.paused)}</div>
          </div>
        </div>`).join("") : '<div class="text-sm text-2 mt-8">No folders.</div>'}
    </div>

    <div class="detail-tab-panel ${activeTab === "dt-devices" ? "active" : ""}" id="dt-devices">
      ${devices.length ? devices.map(d => `
        <div class="detail-folder-row" data-device-id="${esc(d.deviceID)}" data-device-name="${esc(d.name)}" data-device-address="${esc(d.address)}">
          <div class="detail-folder-icon" style="background:${d.connected ? "var(--green-lt)" : "var(--border)"}">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="${d.connected ? "var(--green)" : "var(--text-3)"}" stroke-width="2"><circle cx="12" cy="8" r="4"/><path d="M20 21a8 8 0 1 0-16 0"/></svg>
          </div>
          <div style="flex:1;min-width:0">
            <div class="truncate fw-600 text-sm">${esc(d.name)}</div>
            <div class="text-xs text-2 mono">${esc(d.deviceID.slice(0,10))}…</div>
            ${d.address ? `<div class="text-xs text-2 mono">Address: ${esc(d.address)}</div>` : ""}
          </div>
          <div class="flex gap-6">
            ${d.address && !isManagedSyncthingDevice(d.deviceID)
              ? `<button class="btn btn-primary btn-sm" onclick="openAddDiscoveredDevice(this.closest('[data-device-id]').dataset.deviceId,this.closest('[data-device-id]').dataset.deviceName,this.closest('[data-device-id]').dataset.deviceAddress)">Add to Replicarr</button>`
              : ""}
            ${d.paused
              ? `<button class="btn btn-ghost btn-sm" onclick="actDevice('${esc(inst.id)}',this.closest('[data-device-id]').dataset.deviceId,'resume')">Resume</button>`
              : `<button class="btn btn-ghost btn-sm" onclick="actDevice('${esc(inst.id)}',this.closest('[data-device-id]').dataset.deviceId,'pause')" title="Pauses all sync with this peer">Pause</button>`}
            <button class="btn btn-danger btn-sm" title="Unshare this device" onclick="removeDevice('${esc(inst.id)}',this.closest('[data-device-id]').dataset.deviceId)">Unshare</button>
          </div>
        </div>`).join("") : '<div class="text-sm text-2 mt-8">No devices.</div>'}
    </div>
  `;
  body.scrollTop = scrollTop;
}

function renderFolderDetail(inst, folderId) {
  const folder = (inst.folders || []).find(f => f.id === folderId);
  if (!folder) return;
  const body = $("#detail-body");
  const key = `${inst.id}:${folderId}`;

  // Only rebuild the chrome (and the Subfolders tree inside it) when the
  // selected folder actually changes. This runs again every ~3s on poll —
  // rebuilding the tree every time collapsed it back to "Loading…" and
  // repopulated it, which reset scroll position and any expanded rows.
  // renderFolderDetailLive() below still refreshes progress/status/buttons
  // every poll; only the tree itself is left alone.
  if (body.dataset.folderKey !== key) {
    body.dataset.folderKey = key;
    body.innerHTML = `
      <div class="detail-title">${esc(folder.label || folder.id)}</div>
      <div class="detail-meta">${fmtBytes(folder.globalBytes)} · on ${esc(inst.name)}</div>

      <div id="folder-detail-live"></div>

      <div class="detail-section mt-12">
        <div class="detail-section-title">Subfolders</div>
        <div class="text-xs text-2 mb-8">Push individual subfolders to another instance — the whole folder is never pushed as one unit.</div>
        <div class="subfolder-browser" id="subfolder-browser"></div>
      </div>
    `;
    renderSubfolderBrowser(inst.id, folder.id, $("#subfolder-browser"), "");
  }

  renderFolderDetailLive(inst, folder);
}

function renderFolderDetailLive(inst, folder) {
  const pct = folder.completion ?? 100;
  const fillCls = pct >= 100 ? "complete" : "";

  $("#folder-detail-live").innerHTML = `
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

    <div class="flex gap-8 mt-12" data-folder-id="${esc(folder.id)}">
      ${folder.paused
        ? `<button class="btn btn-ghost btn-sm" onclick="actFolderDetail('resume','${esc(inst.id)}',this.parentElement.dataset.folderId)">Resume</button>`
        : `<button class="btn btn-ghost btn-sm" onclick="actFolderDetail('pause','${esc(inst.id)}',this.parentElement.dataset.folderId)" title="Pauses the entire folder — not a single file">Pause</button>`}
      <button class="btn btn-danger btn-sm" title="Remove folder from Syncthing" onclick="removeFolder(null,'${esc(inst.id)}',this.parentElement.dataset.folderId)">Remove</button>
    </div>
  `;
}

async function actFolderDetail(action, instId, folderId) {
  try {
    await api(`api/folders/${instId}/${folderId}/${action}`, { method: "POST" });
    await poll();
  } catch (e) { alert(e.message); }
}

// ── Subfolder browser ─────────────────────────────────────────────────────────
// Lists subfolders inside a main folder, one level at a time, so individual
// subfolders can be pushed — Syncthing folders sync as a whole, so a
// subfolder push works via selective sync on the target rather than
// creating a second, independently-registered folder inside the first.
async function renderSubfolderBrowser(instId, folderId, containerEl, prefix) {
  containerEl.innerHTML = '<div class="loading-row">Loading…</div>';
  try {
    const r = await api(`api/folders/${instId}/${folderId}/browse?prefix=${encodeURIComponent(prefix)}`);
    if (!r.entries.length) {
      containerEl.innerHTML = '<div class="text-sm text-2">No subfolders here.</div>';
      return;
    }
    containerEl.innerHTML = r.entries.map(entry => `
      <div class="subfolder-row" data-path="${esc(entry.path)}">
        <div class="subfolder-row-main">
          <div class="subfolder-row-toggle" onclick="toggleSubfolderRow(this,'${esc(instId)}','${esc(folderId)}')">
            <svg class="subfolder-chevron" width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
            <span>${esc(entry.name)}</span>
          </div>
          <button class="btn btn-ghost btn-sm" onclick="openPushModal(event,'${esc(instId)}','${esc(folderId)}',this.closest('.subfolder-row').dataset.path)">Push →</button>
        </div>
        <div class="subfolder-children hidden"></div>
      </div>`).join("");
  } catch (e) {
    containerEl.innerHTML = `<div class="alert alert-error">Could not browse: ${esc(e.message)}</div>`;
  }
}

async function searchSubfolders() {
  const query = $("#subfolder-search-query").value.trim();
  const resultsEl = $("#subfolder-search-results");
  $("#modal-search").classList.remove("hidden");
  if (query.length < 2) {
    resultsEl.innerHTML = '<div class="alert alert-info">Enter at least 2 characters.</div>';
    return;
  }

  resultsEl.innerHTML = '<div class="loading-row">Searching subfolders…</div>';
  try {
    const response = await api(`api/search/subfolders?q=${encodeURIComponent(query)}`);
    if (!response.results.length) {
      resultsEl.innerHTML = `
        <div class="empty-state" style="padding:28px 12px">
          <h3>No matching subfolders</h3>
          <p>Searched ${response.searchedFolders} online folder${response.searchedFolders === 1 ? "" : "s"}.</p>
        </div>`;
      return;
    }

    resultsEl.innerHTML = `
      ${response.failedFolders ? `<div class="alert alert-info mb-12">${response.failedFolders} folder${response.failedFolders === 1 ? "" : "s"} could not be searched.</div>` : ""}
      ${response.truncated ? '<div class="alert alert-info mb-12">Showing the first 100 matches. Use a more specific search to narrow the results.</div>' : ""}
      <table class="search-results-table">
        <thead>
          <tr>
            <th>Subfolder</th>
            <th>Location</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          ${response.results.map(result => `
          <tr class="search-result"
              data-instance-id="${esc(result.instanceId)}"
              data-folder-id="${esc(result.folderId)}"
              data-path="${esc(result.path)}">
            <td>
              <div class="search-result-main">
                <div class="search-result-name">${esc(result.name)}</div>
                <div class="search-result-path">${esc(result.path)}</div>
              </div>
            </td>
            <td>
              <div class="search-result-location">${esc(result.instanceName)}</div>
              <div class="search-result-folder">${esc(result.folderLabel)}</div>
            </td>
            <td class="search-result-actions">
              <button class="btn btn-ghost btn-sm" onclick="revealSearchResult(this.closest('.search-result'))">Show in folder</button>
              <button class="btn btn-primary btn-sm" onclick="pushSearchResult(this.closest('.search-result'))">Push →</button>
            </td>
          </tr>`).join("")}
        </tbody>
      </table>
    `;
  } catch (e) {
    resultsEl.innerHTML = `<div class="alert alert-error">Search failed: ${esc(e.message)}</div>`;
  }
}

function pushSearchResult(row) {
  closeModal("modal-search");
  openPushModal(null, row.dataset.instanceId, row.dataset.folderId, row.dataset.path);
}

async function revealSearchResult(row) {
  const instId = row.dataset.instanceId;
  const folderId = row.dataset.folderId;
  const path = row.dataset.path;
  closeModal("modal-search");
  switchTab("overview");
  selectedInstId = instId;
  selectedFolderId = folderId;
  renderQuickCards();
  renderFolderTable();
  openDetailPanel("folder", instId, folderId);

  const root = $("#subfolder-browser");
  if (!root) return;
  await renderSubfolderBrowser(instId, folderId, root, "");

  let container = root;
  let currentPath = "";
  const parts = path.split("/").filter(Boolean);
  for (let index = 0; index < parts.length; index++) {
    currentPath = currentPath ? `${currentPath}/${parts[index]}` : parts[index];
    const match = [...container.children].find(
      child => child.classList.contains("subfolder-row") && child.dataset.path === currentPath
    );
    if (!match) return;
    if (index === parts.length - 1) {
      match.classList.add("search-highlight");
      match.scrollIntoView({ behavior: "smooth", block: "center" });
      setTimeout(() => match.classList.remove("search-highlight"), 3000);
      return;
    }

    const children = match.querySelector(".subfolder-children");
    const chevron = match.querySelector(".subfolder-chevron");
    children.classList.remove("hidden");
    chevron?.classList.add("open");
    if (!children.dataset.loaded) {
      children.dataset.loaded = "1";
      await renderSubfolderBrowser(instId, folderId, children, currentPath);
    }
    container = children;
  }
}

function toggleSubfolderRow(toggleEl, instId, folderId) {
  const row = toggleEl.closest(".subfolder-row");
  const childrenEl = row.querySelector(".subfolder-children");
  const chevron = toggleEl.querySelector(".subfolder-chevron");
  const opening = childrenEl.classList.contains("hidden");
  childrenEl.classList.toggle("hidden", !opening);
  chevron.classList.toggle("open", opening);
  if (opening && !childrenEl.dataset.loaded) {
    childrenEl.dataset.loaded = "1";
    renderSubfolderBrowser(instId, folderId, childrenEl, row.dataset.path);
  }
}

async function actDevice(instId, deviceId, action) {
  try {
    await api(`api/devices/${instId}/${deviceId}/${action}`, { method: "POST" });
    await poll();
  } catch (e) { alert(e.message); }
}

async function removeFolder(e, instId, folderId) {
  if (e) e.stopPropagation();
  if (!confirm("Remove this folder from Syncthing? Files already on disk are not deleted, but the folder stops syncing and is removed from Syncthing's configuration.")) return;
  try {
    await api(`api/folders/${instId}/${folderId}`, { method: "DELETE" });
    if (selectedFolderId === folderId) closeDetail();
    await poll();
  } catch (err) { alert(err.message); }
}

async function removeDevice(instId, deviceId) {
  if (!confirm("Unshare this device? It will be removed from every folder on this instance and disconnected.")) return;
  try {
    await api(`api/devices/${instId}/${deviceId}`, { method: "DELETE" });
    await poll();
  } catch (err) { alert(err.message); }
}

function detailTab(btn, panelId) {
  const body = $("#detail-body");
  body.dataset.instanceTab = panelId;
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

  renderSubfolderTransfers();
}

function renderSubfolderTransfers() {
  const el = $("#subfolder-transfers");

  const row = (t) => {
    if (t.state === "error") {
      return `<tr><td colspan="6" class="offline-row" style="padding:10px 20px">${esc(t.subfolderPath)} (${esc(t.sourceInstanceName)} → ${esc(t.targetInstanceName)}): ${esc(t.error || "Error")}</td></tr>`;
    }
    const pct = t.percent ?? (t.state === "complete" ? 100 : 0);
    const fc  = pct >= 100 ? "complete" : "";
    return `<tr>
      <td><div class="td-name"><div class="td-icon"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg></div>
        <div><div>${esc(t.subfolderPath)}</div><div class="td-meta mono">${esc(t.folderLabel)}</div></div>
      </div></td>
      <td class="text-sm">${esc(t.sourceInstanceName)} → ${esc(t.targetInstanceName)}</td>
      <td class="text-sm">${fmtBytes(t.totalBytes)}</td>
      <td><div class="progress-bar"><div class="progress-fill ${fc}" style="width:${pct}%"></div></div><div class="text-xs text-2 mt-4">${pct}% · ${fmtBytes(t.needBytes)} left</div></td>
      <td class="text-sm">${fmtSpeed(t.speedBytesPerSec)}</td>
      <td class="text-sm">${fmtEta(t.etaSeconds)}</td>
    </tr>`;
  };

  const table = (rows, emptyMessage) => !rows.length
    ? `<div class="empty-state transfer-empty"><h3>${emptyMessage}</h3></div>`
    : `<table class="table"><thead><tr>
        <th style="width:26%">Subfolder</th>
        <th style="width:18%">From → To</th>
        <th style="width:10%">Size</th>
        <th style="width:24%">Progress</th>
        <th style="width:12%">Speed <span class="text-3">(approx)</span></th>
        <th style="width:10%">ETA</th>
      </tr></thead><tbody>${rows.map(row).join("")}</tbody></table>`;

  const inProgress = subfolderTransfersData.filter(t => t.state !== "complete");
  const completed  = subfolderTransfersData.filter(t => t.state === "complete");
  const visibleRows = _transferTab === "completed" ? completed : inProgress;
  const emptyMessage = _transferTab === "completed"
    ? "No completed subfolder transfers"
    : "No subfolder transfers in progress";
  el.innerHTML = `
    <div class="file-table-wrap transfer-view">
      <div class="transfer-tabs">
        <button class="transfer-tab ${_transferTab === "progress" ? "active" : ""}" onclick="setTransferTab('progress')">
          <span>Subfolder Transfers — In Progress</span>
          <span class="transfer-tab-badge">${inProgress.length}</span>
        </button>
        <button class="transfer-tab ${_transferTab === "completed" ? "active" : ""}" onclick="setTransferTab('completed')">
          <span>Subfolder Transfers — Completed</span>
          <span class="transfer-tab-badge">${completed.length}</span>
        </button>
      </div>
      ${table(visibleRows, emptyMessage)}
    </div>`;
}

function setTransferTab(tab) {
  _transferTab = tab === "completed" ? "completed" : "progress";
  renderSubfolderTransfers();
}

// ── Instance wizard ───────────────────────────────────────────────────────────
let _editingId   = null;
let _wizInstStep = 1;
let _wizInstTestResult = null;
let _discoveredDevice = null;

function isManagedSyncthingDevice(deviceId) {
  return statusData.some(inst => inst.myID === deviceId);
}

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

function openAddInstance(prefill = {}) {
  _editingId = null;
  _discoveredDevice = prefill.discovered || null;
  _wizInstTestResult = null;
  $("#modal-inst-title").textContent = _discoveredDevice ? "Add Discovered Device" : "Add Instance";
  $("#modal-inst-name").value = prefill.name || "";
  $("#modal-inst-url").value  = prefill.url || "";
  $("#modal-inst-key").value  = "";
  $("#modal-inst-url-field").classList.toggle("hidden", Boolean(_discoveredDevice));
  $("#modal-inst-error").classList.add("hidden");
  _wizInstSetStep(1);
  $("#modal-inst").classList.remove("hidden");
  setTimeout(() => $("#modal-inst-name").focus(), 60);
}

function openAddDiscoveredDevice(deviceId, name, address) {
  openAddInstance({
    discovered: { deviceId, address },
    name,
  });
}

function openEditInstance(id) {
  const inst = instances.find(i => i.id === id);
  if (!inst) return;
  _editingId = id;
  _discoveredDevice = null;
  _wizInstTestResult = null;
  $("#modal-inst-title").textContent = "Edit Instance";
  $("#modal-inst-name").value = inst.name;
  $("#modal-inst-url").value  = inst.url;
  $("#modal-inst-key").value  = "";
  $("#modal-inst-url-field").classList.remove("hidden");
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
    if (!name || !key || (!_discoveredDevice && !url)) {
      showErr("modal-inst-error", _discoveredDevice ? "Name and API key are required." : "All fields required.");
      return;
    }
    // Run connection test
    const btn = $("#wiz-inst-next");
    btn.disabled = true;
    btn.textContent = "Testing…";
    try {
      const r = _discoveredDevice
        ? await api("api/instances/_discover_test", {
            method: "POST",
            body: {
              address: _discoveredDevice.address,
              api_key: key,
              expected_device_id: _discoveredDevice.deviceId,
            },
          })
        : await api("api/instances/_wizard_test", {
            method: "POST",
            body: { url, api_key: key },
          });
      _wizInstTestResult = r;
      if (r.ok && r.url) $("#modal-inst-url").value = r.url;
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
    closeModal("modal-instance-manage");
    if (selectedInstId === id) {
      selectedInstId = null;
      selectedFolderId = null;
      closeDetail();
    }
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
        <div class="storage-use-root" data-path="${esc(s.path)}" onclick="pickPath(this.dataset.path)">
          Use root: <span class="mono">${esc(s.path)}</span>
        </div>
        ${s.subdirs.map(d => `
          <div class="storage-subdir" data-path="${esc(d)}" onclick="pickPath(this.dataset.path)">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
            ${esc(d)}
          </div>`).join("")}
        ${folderId ? `
          <div class="storage-subdir" data-path="${esc(s.path + "/" + folderId)}" onclick="pickPath(this.dataset.path)">
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
  $$(".storage-subdir, .storage-use-root").forEach(el => {
    el.classList.toggle("selected", el.dataset.path === path);
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

// ── Push (subfolder → selective sync on target) ────────────────────────────────
let _pushSrcInstId    = null;
let _pushFolderId     = null;
let _pushSubfolderPath = null;

function openPushModal(e, instId, folderId, subfolderPath) {
  if (e) e.stopPropagation();
  const targets = instances.filter(i => i.id !== instId);
  if (!targets.length) {
    alert("No other instances to push to. Add a second Syncthing instance first.");
    return;
  }
  _pushSrcInstId     = instId;
  _pushFolderId      = folderId;
  _pushSubfolderPath = subfolderPath;
  const inst = statusData.find(i => i.id === instId);
  $("#modal-push-folder").textContent = subfolderPath;
  $("#modal-push-source").textContent = inst?.name || instId;
  const sel = $("#modal-push-target");
  sel.innerHTML = targets.map(i => `<option value="${esc(i.id)}">${esc(i.name)}</option>`).join("");
  sel.onchange = renderPushPathChoices;
  renderPushPathChoices();
  $("#modal-push-steps").classList.add("hidden");
  $("#modal-push-steps").innerHTML = "";
  $("#modal-push-error").classList.add("hidden");
  $("#modal-push-btn").disabled = false;
  $("#modal-push").classList.remove("hidden");
}

function renderPushPathChoices() {
  const targetId = $("#modal-push-target").value;
  const paths = [...new Set(
    subfolderTransfersData
      .filter(t => t.targetInstanceId === targetId && t.targetPath)
      .map(t => t.targetPath)
  )].sort();
  const saved = $("#modal-push-path-saved");
  const input = $("#modal-push-path");

  input.value = "";
  if (!paths.length) {
    saved.classList.add("hidden");
    input.classList.remove("hidden");
    return;
  }

  saved.innerHTML = `
    <option value="">Leave blank / use existing mapping</option>
    ${paths.map(path => `<option value="${esc(path)}">${esc(path)}</option>`).join("")}
    <option value="__custom__">Enter another path…</option>
  `;
  saved.value = "";
  saved.classList.remove("hidden");
  input.classList.add("hidden");
}

function selectPushTargetPath() {
  const saved = $("#modal-push-path-saved");
  const input = $("#modal-push-path");
  if (saved.value === "__custom__") {
    input.value = "";
    input.classList.remove("hidden");
    input.focus();
    return;
  }
  input.value = saved.value;
  input.classList.add("hidden");
}

async function executePush() {
  const targetId   = $("#modal-push-target").value;
  const targetPath = $("#modal-push-path").value.trim();
  $("#modal-push-btn").disabled = true;
  $("#modal-push-error").classList.add("hidden");
  try {
    const r = await api(`api/folders/${_pushSrcInstId}/${_pushFolderId}/push-subfolder`, {
      method: "POST",
      body: {
        subfolder_path: _pushSubfolderPath,
        target_instance_id: targetId,
        target_path: targetPath || null,
      },
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
      </li>`).join("")}</ul>
      ${r.rollback ? `
        <div class="text-xs text-2 mt-8" style="text-transform:uppercase;letter-spacing:.03em">Rollback of completed steps</div>
        <ul class="step-list">${r.rollback.map(rb => `
          <li class="step-item ${rb.ok ? "step-ok" : "step-fail"}">
            <div class="step-icon">${rb.ok ? "✓" : "✗"}</div>
            <div>
              <div>${esc(rb.description)}</div>
              ${rb.error ? `<div class="text-xs text-2">${esc(rb.error)}</div>` : ""}
            </div>
          </li>`).join("")}</ul>
        ${r.rollback.some(rb => !rb.ok) ? `<div class="alert alert-error mt-8">Some rollback steps failed — you may need to clean up manually in Syncthing's own UI.</div>` : ""}
      ` : ""}`;
    if (!r.ok) showErr("modal-push-error", r.rollback ? "Push stopped and prior steps were rolled back — see details above." : "Push stopped — see steps above.");
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
}

async function loadFolderOrders() {
  folderOrders = await api("api/folder-orders");
}

async function loadAuthSession() {
  const auth = await api("api/auth/session");
  $("#btn-logout").classList.toggle("hidden", !auth.direct);
}

async function logoutDirectAccess() {
  try {
    await api("logout", { method: "POST" });
  } finally {
    window.location.href = "login";
  }
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
  // Apply the saved theme before first render to avoid a flash.
  const saved = localStorage.getItem("replicarr-theme");
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  applyTheme(saved ? saved === "dark" : prefersDark);

  await Promise.all([loadInstances(), loadFolderOrders(), loadAuthSession()]);
  await poll();
  switchTab("overview");
  // Poll on a timer — see the note above poll()'s definition for why this
  // isn't a push-based live feed.
  setInterval(poll, 3000);
})();

// Expose for inline handlers (includes renderFolderTable used in detail panel onclick strings)
Object.assign(window, {
  toggleTheme,
  toggleDetailPanel,
  logoutDirectAccess,
  switchTab, selectInstance, openInstanceManagement, selectFolder, renderFolderTable, closeDetail, detailTab,
  openProblemResolution, openProblemTooltip, scheduleProblemTooltipClose,
  startFolderDrag, dragOverFolderRow, finishFolderDrag,
  actFolder, actFolderDetail, actDevice, removeFolder, removeDevice,
  openAddInstance, openEditInstance, wizInstNext, wizInstBack,
  openAddDiscoveredDevice,
  deleteInstance, testInstance,
  openAddFolder, wizFolderNext, wizFolderBack, toggleStorageRoot, pickPath,
  searchSubfolders, revealSearchResult, pushSearchResult, toggleSubfolderRow,
  openPushModal, renderPushPathChoices, selectPushTargetPath, executePush,
  setTransferTab,
  closeModal, poll,
});
