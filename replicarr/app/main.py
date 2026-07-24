"""
Replicarr — FastAPI backend.

All Syncthing API keys stay server-side.
The browser talks only to /api/* and the static web/ directory.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import store
import syncthing as st

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("replicarr")

# ── Direct-access Basic Auth ────────────────────────────────────────────────────
# Requests that arrive via Home Assistant Ingress are already authenticated by
# HA (identified by the X-Ingress-Path header, which only the Supervisor's
# proxy sets). Requests hitting the add-on's directly-published port carry no
# such header and no HA session, so they're gated behind Basic Auth instead.
BASIC_AUTH_USERNAME = os.environ.get("BASIC_AUTH_USERNAME", "")
BASIC_AUTH_PASSWORD = os.environ.get("BASIC_AUTH_PASSWORD", "")

# ── Shared status/transfers cache ───────────────────────────────────────────────
# A single background refresh cycle owns all outbound Syncthing REST calls.
# /api/status, /api/transfers, and /api/stream all read from these caches
# instead of independently re-fetching from every instance on every request —
# previously each browser tab polling every 3s multiplied the load on every
# configured Syncthing instance on top of this same sampler's own cycle.
_status_cache: list[dict[str, Any]] = []
_transfers_cache: dict[str, Any] = {
    "instances": [],
    "overall": {
        "totalBytes": 0, "needBytes": 0, "percent": 100,
        "inSpeedBytesPerSec": 0.0, "outSpeedBytesPerSec": 0.0, "etaSeconds": None,
    },
}
_subfolder_transfers_cache: list[dict[str, Any]] = []
_sampler_task: asyncio.Task | None = None
REFRESH_INTERVAL_SECONDS = 2

# { instance_id: { "ts": float, "inBytes": int, "outBytes": int } }
_byte_samples: dict[str, dict[str, Any]] = {}
# { instance_id + folder_id: { "ts": float, "needBytes": int } }
_folder_samples: dict[str, dict[str, Any]] = {}
# { push key: { "ts": float, "needBytes": int } } — see _subfolder_push_key
_subfolder_samples: dict[str, dict[str, Any]] = {}

EWMA_ALPHA = 0.3  # smoothing factor for byte-rate EWMA
# { key: smoothed_rate_bytes_per_sec }
_smoothed_rates: dict[str, float] = {}


def _ewma(key: str, new_rate: float) -> float:
    prev = _smoothed_rates.get(key, new_rate)
    smoothed = EWMA_ALPHA * new_rate + (1 - EWMA_ALPHA) * prev
    _smoothed_rates[key] = smoothed
    return smoothed


async def _refresh_all() -> None:
    """Fetches fresh data for every instance and rebuilds both caches."""
    global _status_cache, _transfers_cache, _subfolder_transfers_cache
    instances = store.load_instances()
    results = await asyncio.gather(*[_refresh_instance(i) for i in instances])

    _status_cache = [r["status"] for r in results]

    overall_need = overall_total = 0
    overall_in_speed = overall_out_speed = 0.0
    transfer_instances = []
    for r in results:
        t = r["transfers"]
        overall_in_speed += t.get("_in_speed", 0.0)
        overall_out_speed += t.get("_out_speed", 0.0)
        for f in t.get("folders", []):
            overall_need += f.get("needBytes", 0)
            overall_total += f.get("totalBytes", 0)
        transfer_instances.append({k: v for k, v in t.items() if not k.startswith("_")})

    overall_pct = round(
        ((overall_total - overall_need) / overall_total * 100) if overall_total else 100, 1
    )
    overall_eta = (
        int(overall_need / overall_in_speed) if overall_in_speed > 0 and overall_need > 0 else None
    )
    _transfers_cache = {
        "instances": transfer_instances,
        "overall": {
            "totalBytes": overall_total,
            "needBytes": overall_need,
            "percent": overall_pct,
            "inSpeedBytesPerSec": round(overall_in_speed, 1),
            "outSpeedBytesPerSec": round(overall_out_speed, 1),
            "etaSeconds": overall_eta,
        },
    }

    inst_by_id = {i["id"]: i for i in instances}
    _subfolder_transfers_cache = await asyncio.gather(*[
        _refresh_subfolder_transfer(p, inst_by_id) for p in store.load_subfolder_pushes()
    ])


def _subfolder_push_key(push: dict[str, Any]) -> str:
    return f"{push['source_instance_id']}:{push['folder_id']}:{push['subfolder_path']}:{push['target_instance_id']}"


async def _refresh_subfolder_transfer(
    push: dict[str, Any], inst_by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    key = _subfolder_push_key(push)
    source_inst = inst_by_id.get(push["source_instance_id"])
    target_inst = inst_by_id.get(push["target_instance_id"])
    base = {
        "sourceInstanceId": push["source_instance_id"],
        "sourceInstanceName": source_inst["name"] if source_inst else push["source_instance_id"],
        "targetInstanceId": push["target_instance_id"],
        "targetInstanceName": target_inst["name"] if target_inst else push["target_instance_id"],
        "folderId": push["folder_id"],
        "folderLabel": push.get("folder_label", push["folder_id"]),
        "subfolderPath": push["subfolder_path"],
        "totalBytes": push.get("total_bytes", 0),
    }
    if target_inst is None:
        return {**base, "state": "error", "error": "Target instance no longer exists"}

    try:
        need = await st.get_db_need(target_inst["url"], target_inst["api_key"], push["folder_id"])
        prefix = push["subfolder_path"].rstrip("/") + "/"
        exact = push["subfolder_path"]
        need_files = [
            f for group in ("progress", "queued", "rest") for f in need.get(group, [])
            if f.get("name") == exact or str(f.get("name", "")).startswith(prefix)
        ]
        need_bytes = sum(f.get("size", 0) or 0 for f in need_files)

        now = time.monotonic()
        prev = _subfolder_samples.get(key)
        if prev and now > prev["ts"]:
            dt = now - prev["ts"]
            delta = prev["needBytes"] - need_bytes  # falling = progress
            _ewma(f"subfolder:{key}:speed", max(0, delta / dt))
        _subfolder_samples[key] = {"ts": now, "needBytes": need_bytes}
        speed = _smoothed_rates.get(f"subfolder:{key}:speed", 0.0)

        total_bytes = base["totalBytes"] or need_bytes
        pct = round(((total_bytes - need_bytes) / total_bytes * 100) if total_bytes else 100, 1)
        eta = int(need_bytes / speed) if speed > 0 and need_bytes > 0 else None

        return {
            **base,
            "totalBytes": total_bytes,
            "needBytes": need_bytes,
            "percent": pct,
            "speedBytesPerSec": round(speed, 1),
            "speedApproximate": True,
            "etaSeconds": eta,
            "state": "complete" if need_bytes == 0 else "syncing",
        }
    except Exception as e:
        logger.debug("Refresh failed for subfolder push %s: %s", key, e)
        return {**base, "state": "error", "error": str(e)}


async def _refresh_instance(inst: dict) -> dict[str, Any]:
    url, key, iid = inst["url"], inst["api_key"], inst["id"]
    base = {"id": iid, "name": inst["name"], "source": inst["source"]}
    now = time.monotonic()
    try:
        system_status, folders, devices, connections = await asyncio.gather(
            st.get_system_status(url, key),
            st.get_config_folders(url, key),
            st.get_config_devices(url, key),
            st.get_system_connections(url, key),
        )
        my_id = system_status.get("myID", "")
        conn_map = connections.get("connections", {})

        total = connections.get("total", {})
        in_b, out_b = total.get("inBytesTotal", 0), total.get("outBytesTotal", 0)
        prev = _byte_samples.get(iid)
        if prev and now > prev["ts"]:
            dt = now - prev["ts"]
            _ewma(f"{iid}:in", max(0, (in_b - prev["inBytes"]) / dt))
            _ewma(f"{iid}:out", max(0, (out_b - prev["outBytes"]) / dt))
        _byte_samples[iid] = {"ts": now, "inBytes": in_b, "outBytes": out_b}

        folder_results = await asyncio.gather(*[
            _refresh_folder(url, key, f, iid, now) for f in folders
        ])

        return {
            "status": {
                **base,
                "online": True,
                "myID": my_id,
                "version": system_status.get("version"),
                "folders": [fr["status"] for fr in folder_results],
                "devices": [_device_info(d, conn_map) for d in devices],
            },
            "transfers": {
                "instanceId": iid,
                "folders": [fr["transfer"] for fr in folder_results],
                "_in_speed": _smoothed_rates.get(f"{iid}:in", 0.0),
                "_out_speed": _smoothed_rates.get(f"{iid}:out", 0.0),
            },
        }
    except httpx.HTTPStatusError as e:
        logger.debug("Refresh failed for instance %s: HTTP %s", iid, e.response.status_code)
        return {
            "status": {**base, "online": False, "error": f"HTTP {e.response.status_code}"},
            "transfers": {"instanceId": iid, "folders": [], "offline": True, "_in_speed": 0.0, "_out_speed": 0.0},
        }
    except Exception as e:
        logger.debug("Refresh failed for instance %s: %s", iid, e)
        return {
            "status": {**base, "online": False, "error": str(e)},
            "transfers": {"instanceId": iid, "folders": [], "offline": True, "_in_speed": 0.0, "_out_speed": 0.0},
        }


async def _refresh_folder(
    url: str, key: str, folder: dict, iid: str, now: float
) -> dict[str, Any]:
    fid = folder["id"]
    fkey = f"{iid}:{fid}"
    try:
        dbs = await st.get_db_status(url, key, fid)
        state = dbs.get("state", "unknown")
        global_bytes = dbs.get("globalBytes", 0)
        need_bytes = dbs.get("needBytes", 0)
        in_sync = dbs.get("inSyncBytes", 0)
        pct = round((in_sync / global_bytes * 100) if global_bytes else 100, 1)

        prev_f = _folder_samples.get(fkey)
        if prev_f and now > prev_f["ts"]:
            dt = now - prev_f["ts"]
            delta = prev_f["needBytes"] - need_bytes  # falling = progress
            _ewma(f"{fkey}:speed", max(0, delta / dt))
        _folder_samples[fkey] = {"ts": now, "needBytes": need_bytes}
        speed = _smoothed_rates.get(f"{fkey}:speed", 0.0)
        eta = int(need_bytes / speed) if speed > 0 and need_bytes > 0 else None

        return {
            "status": {
                "id": fid,
                "label": folder.get("label", fid),
                "path": folder.get("path", ""),
                "paused": folder.get("paused", False),
                "state": state,
                "globalBytes": global_bytes,
                "needBytes": need_bytes,
                "inSyncBytes": in_sync,
                "completion": pct,
                "pullErrors": dbs.get("pullErrors", 0),
                "devices": [d["deviceID"] for d in folder.get("devices", [])],
            },
            "transfer": {
                "id": fid,
                "label": folder.get("label", fid),
                "paused": folder.get("paused", False),
                "state": state,
                "percent": pct,
                "totalBytes": global_bytes,
                "needBytes": need_bytes,
                "speedBytesPerSec": round(speed, 1),
                "speedApproximate": True,
                "etaSeconds": eta,
            },
        }
    except Exception as e:
        logger.debug("Refresh failed for folder %s: %s", fkey, e)
        return {
            "status": {"id": fid, "label": folder.get("label", fid), "paused": folder.get("paused", False), "error": str(e)},
            "transfer": {"id": fid, "error": str(e)},
        }


async def _sample_loop() -> None:
    while True:
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)
        try:
            await _refresh_all()
        except Exception:
            logger.debug("Refresh cycle failed", exc_info=True)


# ── Lifespan ───────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sampler_task
    # Merge config instances on startup
    cfg_path = Path("/tmp/config_instances.json")
    config_instances: list[dict] = []
    if cfg_path.exists():
        try:
            raw = cfg_path.read_text().strip()
            if raw and raw != "null":
                config_instances = json.loads(raw)
        except Exception:
            logger.warning("Could not parse %s — ignoring config-defined instances", cfg_path)
    store.merge_config_instances(config_instances)
    logger.info("Replicarr started. Instances loaded.")

    await _refresh_all()  # populate the cache before serving the first request
    _sampler_task = asyncio.create_task(_sample_loop())
    yield
    _sampler_task.cancel()


# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Replicarr", lifespan=lifespan)


def _unauthorized(detail: str, challenge: bool) -> JSONResponse:
    headers = {"WWW-Authenticate": 'Basic realm="Replicarr"'} if challenge else None
    return JSONResponse({"detail": detail}, status_code=401 if challenge else 403, headers=headers)


@app.middleware("http")
async def ingress_or_basic_auth(request: Request, call_next):
    ingress_path = request.headers.get("X-Ingress-Path", "")
    if ingress_path:
        # Trusted: only the Supervisor's Ingress proxy sets this header, and
        # HA has already authenticated the user's session to get here.
        request.scope["root_path"] = ingress_path
        return await call_next(request)

    if request.url.path == "/health":
        return await call_next(request)

    if not BASIC_AUTH_USERNAME or not BASIC_AUTH_PASSWORD:
        return _unauthorized(
            "Direct access is disabled. Set 'basic_auth_username' and "
            "'basic_auth_password' in the add-on configuration to allow "
            "access outside Home Assistant Ingress.",
            challenge=False,
        )

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        return _unauthorized("Authentication required.", challenge=True)

    try:
        decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
        user, _, pw = decoded.partition(":")
    except Exception:
        return _unauthorized("Invalid Authorization header.", challenge=True)

    user_ok = secrets.compare_digest(user, BASIC_AUTH_USERNAME)
    pw_ok = secrets.compare_digest(pw, BASIC_AUTH_PASSWORD)
    if not (user_ok and pw_ok):
        return _unauthorized("Invalid credentials.", challenge=True)

    return await call_next(request)


# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}


# ── HA storage discovery ───────────────────────────────────────────────────────
@app.get("/api/storage")
async def list_storage():
    """
    Returns local storage paths available inside the container:
    - /data          — add-on persistent volume (always present)
    - /media         — HA media share (if mounted)
    - /share         — HA general share (if mounted)
    - /backup        — HA backup share (if mounted)
    - /config        — HA config share (if mounted, read-only usually)
    Also returns subdirectories one level deep for each present path
    so the wizard can let users pick a subfolder.
    """
    candidates = [
        {"path": "/data",   "label": "Add-on data",     "description": "Persistent storage for this add-on"},
        {"path": "/media",  "label": "HA Media",         "description": "/media share"},
        {"path": "/share",  "label": "HA Share",         "description": "/share share"},
        {"path": "/backup", "label": "HA Backup",        "description": "/backup share"},
        {"path": "/config", "label": "HA Config",        "description": "/config share (usually read-only)"},
    ]
    result = []
    for c in candidates:
        p = Path(c["path"])
        if not p.exists():
            continue
        subdirs = []
        try:
            subdirs = sorted([
                str(child) for child in p.iterdir()
                if child.is_dir() and not child.name.startswith(".")
            ])[:50]  # cap at 50 to avoid huge responses
        except PermissionError:
            pass
        result.append({**c, "present": True, "subdirs": subdirs})
    return result


# ── Pydantic models ────────────────────────────────────────────────────────────
class InstanceCreate(BaseModel):
    name: str
    url: str
    api_key: str


class InstanceUpdate(BaseModel):
    name: str
    url: str
    api_key: str


class FolderCreate(BaseModel):
    folder_id: str
    label: str
    path: str


class PushRequest(BaseModel):
    target_instance_id: str
    target_path: str


class PushSubfolderRequest(BaseModel):
    subfolder_path: str
    target_instance_id: str
    # Only required the first time this main folder is pushed to this target —
    # later subfolder pushes to the same (folder, target) pair reuse the path
    # recorded from that first push.
    target_path: str | None = None


# ── Instance endpoints ──────────────────────────────────────────────────────────
@app.get("/api/instances")
async def list_instances():
    instances = store.load_instances()
    # Never return api_key to the browser
    return [_redact(i) for i in instances]


@app.post("/api/instances", status_code=201)
async def create_instance(body: InstanceCreate):
    try:
        inst = store.add_instance(body.name, body.url, body.api_key)
        await _refresh_all()
        return _redact(inst)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/instances/{inst_id}")
async def update_instance(inst_id: str, body: InstanceUpdate):
    try:
        inst = store.update_instance(inst_id, body.name, body.url, body.api_key)
        await _refresh_all()
        return _redact(inst)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/instances/{inst_id}", status_code=204)
async def delete_instance(inst_id: str):
    try:
        store.delete_instance(inst_id)
        await _refresh_all()
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))


class WizardTestRequest(BaseModel):
    url: str
    api_key: str


@app.post("/api/instances/_wizard_test")
async def wizard_test(body: WizardTestRequest):
    """Test a Syncthing connection without persisting — used by the Add Instance wizard."""
    try:
        status = await st.get_system_status(body.url.rstrip("/"), body.api_key)
        return {"reachable": True, "ok": True, "myID": status.get("myID"), "version": status.get("version")}
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            return {"reachable": True, "ok": False, "error": "Invalid API key"}
        return {"reachable": False, "ok": False, "error": f"HTTP {e.response.status_code}"}
    except Exception as e:
        return {"reachable": False, "ok": False, "error": str(e)}


@app.post("/api/instances/{inst_id}/test")
async def test_instance(inst_id: str):
    inst = _get_instance(inst_id)
    try:
        status = await st.get_system_status(inst["url"], inst["api_key"])
        return {"reachable": True, "myID": status.get("myID"), "version": status.get("version")}
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        if code in (401, 403):
            return {"reachable": True, "auth_ok": False, "error": "Invalid API key"}
        return {"reachable": False, "error": str(e)}
    except Exception as e:
        return {"reachable": False, "error": str(e)}


# ── Status / overview ──────────────────────────────────────────────────────────
@app.get("/api/status")
async def get_status():
    """Per-instance status: folders, devices, sync state, sizes — served from cache."""
    return _status_cache


def _device_info(device: dict, conn_map: dict) -> dict:
    did = device["deviceID"]
    conn = conn_map.get(did, {})
    return {
        "deviceID": did,
        "name": device.get("name", did[:8]),
        "paused": device.get("paused", False),
        "connected": conn.get("connected", False),
        "address": conn.get("address", ""),
        "inBytesTotal": conn.get("inBytesTotal", 0),
        "outBytesTotal": conn.get("outBytesTotal", 0),
    }


# ── Transfer metrics ───────────────────────────────────────────────────────────
@app.get("/api/transfers")
async def get_transfers():
    return _transfers_cache


@app.get("/api/subfolder-transfers")
async def get_subfolder_transfers():
    return _subfolder_transfers_cache


# ── Live updates ────────────────────────────────────────────────────────────────
# uvicorn only delivers the ASGI lifespan "shutdown" event after all in-flight
# connections have closed on their own — so a signal set from that lifespan
# handler (like _shutdown_event) can never reach an SSE loop that is only
# waiting to be told to close: neither side moves first. Confirmed live (see
# CHANGELOG 0.3.1/0.3.2): the "waiting on _shutdown_event" fix from 0.3.1
# didn't help, and the process hung until Docker's ~10s SIGKILL grace period
# forcibly killed it, corrupting s6's supervision state for the next boot.
# Instead of relying on being told to stop, the stream now bounds its own
# lifetime and ends normally well within that window — EventSource
# reconnects automatically, and once shutdown begins uvicorn stops accepting
# new connections, so the cycled-out connection is simply never replaced.
STREAM_MAX_LIFETIME_SECONDS = 8


@app.get("/api/stream")
async def stream(request: Request):
    """
    Server-Sent Events feed of the same data as /api/status + /api/transfers,
    pushed on every refresh cycle so the frontend doesn't need to poll.
    """
    async def event_generator():
        last_payload = None
        started = time.monotonic()
        while time.monotonic() - started < STREAM_MAX_LIFETIME_SECONDS:
            if await request.is_disconnected():
                break
            payload = json.dumps({
                "status": _status_cache,
                "transfers": _transfers_cache,
                "subfolderTransfers": _subfolder_transfers_cache,
            })
            if payload != last_payload:
                yield f"data: {payload}\n\n"
                last_payload = payload
            else:
                yield ": keep-alive\n\n"
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Pause / resume folder ─────────────────────────────────────────────────────
@app.post("/api/folders/{inst_id}/{folder_id}/pause")
async def pause_folder(inst_id: str, folder_id: str):
    inst = _get_instance(inst_id)
    try:
        await st.pause_folder(inst["url"], inst["api_key"], folder_id)
        await _refresh_all()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/folders/{inst_id}/{folder_id}/resume")
async def resume_folder(inst_id: str, folder_id: str):
    inst = _get_instance(inst_id)
    try:
        await st.resume_folder(inst["url"], inst["api_key"], folder_id)
        await _refresh_all()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/api/folders/{inst_id}/{folder_id}", status_code=204)
async def remove_folder(inst_id: str, folder_id: str):
    """Removes the folder from Syncthing's config. Files on disk are untouched."""
    inst = _get_instance(inst_id)
    try:
        await st.delete_config_folder(inst["url"], inst["api_key"], folder_id)
        await _refresh_all()
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, e.response.text)
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Pause / resume device ─────────────────────────────────────────────────────
@app.post("/api/devices/{inst_id}/{device_id}/pause")
async def pause_device(inst_id: str, device_id: str):
    inst = _get_instance(inst_id)
    try:
        await st.pause_device(inst["url"], inst["api_key"], device_id)
        await _refresh_all()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/devices/{inst_id}/{device_id}/resume")
async def resume_device(inst_id: str, device_id: str):
    inst = _get_instance(inst_id)
    try:
        await st.resume_device(inst["url"], inst["api_key"], device_id)
        await _refresh_all()
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/api/devices/{inst_id}/{device_id}", status_code=204)
async def remove_device(inst_id: str, device_id: str):
    """Unshares the device from every folder on this instance, then removes it."""
    inst = _get_instance(inst_id)
    url, key = inst["url"], inst["api_key"]
    try:
        folders = await st.get_config_folders(url, key)
        for fdr in folders:
            devices = fdr.get("devices", [])
            if any(d["deviceID"] == device_id for d in devices):
                fdr["devices"] = [d for d in devices if d["deviceID"] != device_id]
                await st.put_config_folder(url, key, fdr["id"], fdr)
        await st.delete_config_device(url, key, device_id)
        await _refresh_all()
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, e.response.text)
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Add folder ────────────────────────────────────────────────────────────────
@app.post("/api/instances/{inst_id}/folders", status_code=201)
async def add_folder(inst_id: str, body: FolderCreate):
    inst = _get_instance(inst_id)
    url, key = inst["url"], inst["api_key"]
    try:
        defaults = await st.get_default_folder(url, key)
        folder_cfg = {
            **defaults,
            "id": body.folder_id,
            "label": body.label,
            "path": body.path,
            "devices": [],
        }
        await st.put_config_folder(url, key, body.folder_id, folder_cfg)
        rr = await st.get_restart_required(url, key)
        await _refresh_all()
        return {"ok": True, "restartRequired": rr.get("requiresRestart", False)}
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, e.response.text)
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Push (share folder to another instance) ───────────────────────────────────
async def _ensure_folder_shared(
    source_inst: dict, target_inst: dict, folder_id: str, target_path: str,
    create_paused: bool = False,
) -> dict[str, Any]:
    """
    Steps 1-4 of the push flow: get device IDs, register each instance as a
    device on the other, share the folder from source, and create it on
    target at target_path. Shared by the whole-folder push endpoint and the
    subfolder push endpoint's first-time-sharing path.

    Each mutating step registers a matching "undo" action; if a later step
    fails, everything undoable so far is rolled back (in reverse order, so
    folder-share references are removed before the device entries they
    depend on) and the rollback outcome is reported alongside the steps, so
    a failure doesn't leave either instance half-configured with only a raw
    error message to act on.

    create_paused=True creates the target folder paused instead of letting
    it sync immediately — used by the subfolder push flow so the folder
    can't start pulling everything in the brief window before selective-
    sync ignore patterns are applied; the caller is responsible for
    resuming it once those patterns are safely in place.

    Returns {"ok": bool, "steps": [...], "rollback": [...] (on failure only),
    "sourceDeviceID": str, "targetDeviceID": str (on success)}.
    """
    steps: list[dict] = []
    undo_actions: list[tuple[str, Any]] = []

    async def _rollback() -> list[dict]:
        results = []
        for description, action in reversed(undo_actions):
            try:
                await action()
                results.append({"description": description, "ok": True})
            except Exception as e:
                results.append({"description": description, "ok": False, "error": str(e)})
        return results

    async def _fail(step: int, description: str, error: str) -> dict[str, Any]:
        steps.append({"step": step, "description": description, "ok": False, "error": error})
        result: dict[str, Any] = {"ok": False, "steps": steps}
        if undo_actions:
            result["rollback"] = await _rollback()
        return result

    try:
        # Step 1: Get device IDs (no side effects — nothing to roll back if this fails)
        src_status = await st.get_system_status(source_inst["url"], source_inst["api_key"])
        tgt_status = await st.get_system_status(target_inst["url"], target_inst["api_key"])
        src_id = src_status["myID"]
        tgt_id = tgt_status["myID"]
        steps.append({"step": 1, "description": "Got device IDs", "ok": True,
                       "sourceDeviceID": src_id, "targetDeviceID": tgt_id})
    except Exception as e:
        return await _fail(1, "Get device IDs", str(e))

    try:
        # Step 2a: Add target as device on source
        tgt_dev_cfg = {"deviceID": tgt_id, "name": target_inst["name"],
                       "addresses": ["dynamic"], "compression": "metadata",
                       "introducer": False, "skipIntroductionRemovals": False,
                       "introducedBy": "", "paused": False, "allowedNetworks": [],
                       "autoAcceptFolders": False, "maxSendKbps": 0, "maxRecvKbps": 0,
                       "ignoredFolders": [], "maxRequestKiB": 0, "untrustedIntroducer": False}
        await st.put_config_device(source_inst["url"], source_inst["api_key"], tgt_id, tgt_dev_cfg)
        undo_actions.append((
            "Remove target device from source",
            lambda: st.delete_config_device(source_inst["url"], source_inst["api_key"], tgt_id),
        ))
        steps.append({"step": 2, "description": "Registered target device on source", "ok": True})
    except Exception as e:
        return await _fail(2, "Register target on source", str(e))

    try:
        # Step 2b: Add source as device on target
        src_dev_cfg = {"deviceID": src_id, "name": source_inst["name"],
                       "addresses": ["dynamic"], "compression": "metadata",
                       "introducer": False, "skipIntroductionRemovals": False,
                       "introducedBy": "", "paused": False, "allowedNetworks": [],
                       "autoAcceptFolders": False, "maxSendKbps": 0, "maxRecvKbps": 0,
                       "ignoredFolders": [], "maxRequestKiB": 0, "untrustedIntroducer": False}
        await st.put_config_device(target_inst["url"], target_inst["api_key"], src_id, src_dev_cfg)
        undo_actions.append((
            "Remove source device from target",
            lambda: st.delete_config_device(target_inst["url"], target_inst["api_key"], src_id),
        ))
        steps.append({"step": 2, "description": "Registered source device on target", "ok": True})
    except Exception as e:
        return await _fail(2, "Register source on target", str(e))

    try:
        # Step 3: Add target to folder's device list on source (read-modify-write)
        folder_cfg = await st.get_config_folder(source_inst["url"], source_inst["api_key"], folder_id)
        existing_ids = [d["deviceID"] for d in folder_cfg.get("devices", [])]
        if tgt_id not in existing_ids:
            folder_cfg.setdefault("devices", []).append(
                {"deviceID": tgt_id, "introducedBy": "", "encryptionPassword": ""}
            )
            await st.put_config_folder(source_inst["url"], source_inst["api_key"], folder_id, folder_cfg)

            async def _unshare_from_source():
                cfg = await st.get_config_folder(source_inst["url"], source_inst["api_key"], folder_id)
                cfg["devices"] = [d for d in cfg.get("devices", []) if d["deviceID"] != tgt_id]
                await st.put_config_folder(source_inst["url"], source_inst["api_key"], folder_id, cfg)

            undo_actions.append(("Unshare folder from target on source", _unshare_from_source))
        steps.append({"step": 3, "description": "Shared folder with target device on source", "ok": True})
    except Exception as e:
        return await _fail(3, "Share folder on source", str(e))

    try:
        # Step 4: Recreate folder on target with same ID
        defaults = await st.get_default_folder(target_inst["url"], target_inst["api_key"])
        src_folder = await st.get_config_folder(source_inst["url"], source_inst["api_key"], folder_id)
        new_folder = {
            **defaults,
            "id": folder_id,
            "label": src_folder.get("label", folder_id),
            "path": target_path,
            "devices": [
                {"deviceID": src_id, "introducedBy": "", "encryptionPassword": ""}
            ],
            "type": src_folder.get("type", "sendreceive"),
            "paused": create_paused,
        }
        await st.put_config_folder(target_inst["url"], target_inst["api_key"], folder_id, new_folder)
        steps.append({"step": 4, "description": "Created folder on target", "ok": True})
    except Exception as e:
        return await _fail(4, "Create folder on target", str(e))

    return {"ok": True, "steps": steps, "sourceDeviceID": src_id, "targetDeviceID": tgt_id}


@app.post("/api/folders/{inst_id}/{folder_id}/push")
async def push_folder(inst_id: str, folder_id: str, body: PushRequest):
    """5-step folder-sharing flow from source (inst_id) to target — see _ensure_folder_shared."""
    source_inst = _get_instance(inst_id)
    target_inst = _get_instance(body.target_instance_id)

    result = await _ensure_folder_shared(source_inst, target_inst, folder_id, body.target_path)
    if not result["ok"]:
        return result
    steps = result["steps"]

    try:
        # Step 5: Check restart required on both (read-only — no rollback needed)
        rr_src = await st.get_restart_required(source_inst["url"], source_inst["api_key"])
        rr_tgt = await st.get_restart_required(target_inst["url"], target_inst["api_key"])
        steps.append({
            "step": 5,
            "description": "Checked restart requirements",
            "ok": True,
            "sourceRestartRequired": rr_src.get("requiresRestart", False),
            "targetRestartRequired": rr_tgt.get("requiresRestart", False),
        })
    except Exception as e:
        steps.append({"step": 5, "description": "Check restart", "ok": False, "error": str(e)})

    await _refresh_all()
    return {"ok": True, "steps": steps}


# ── Subfolder browsing & selective-sync push ──────────────────────────────────
def _normalize_browse_entries(raw: Any, prefix: str) -> list[dict[str, Any]]:
    """
    Normalizes Syncthing's /rest/db/browse response into a flat list of
    {"name": leaf, "path": full relative path, "type": "dir"|"file"}.
    NOT independently verified against a live Syncthing instance — if
    subfolder browsing looks wrong or empty in practice, this is the first
    place to check against what Syncthing actually returns.
    """
    entries: list[dict[str, Any]] = []
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("Name") or ""
        if not name:
            continue
        full_path = name if name.startswith(prefix) and prefix else (
            f"{prefix}/{name}".strip("/") if prefix else name
        )
        item_type = str(item.get("type") or item.get("Type") or "").upper()
        is_dir = "DIR" in item_type or "children" in item or "Children" in item
        entries.append({
            "name": full_path.rsplit("/", 1)[-1],
            "path": full_path,
            "type": "dir" if is_dir else "file",
        })
    return entries


@app.get("/api/folders/{inst_id}/{folder_id}/browse")
async def browse_folder(inst_id: str, folder_id: str, prefix: str = ""):
    """Lists subfolders (not files) one level under `prefix` — used to pick a subfolder to push."""
    inst = _get_instance(inst_id)
    try:
        raw = await st.get_db_browse(inst["url"], inst["api_key"], folder_id, prefix=prefix, levels=1)
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, e.response.text)
    except Exception as e:
        raise HTTPException(500, str(e))
    entries = [e for e in _normalize_browse_entries(raw, prefix) if e["type"] == "dir"]
    return {"prefix": prefix, "entries": entries}


def _sum_browse_size(raw: Any) -> int:
    """Recursively sums file sizes from a /rest/db/browse response, best-effort."""
    total = 0
    items = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    for item in items:
        if not isinstance(item, dict):
            continue
        children = item.get("children") or item.get("Children")
        if children:
            total += _sum_browse_size(children)
        else:
            total += item.get("size") or item.get("Size") or 0
    return total


def _selective_sync_ignores(existing: list[str], subfolder_path: str) -> list[str]:
    """
    Adds `subfolder_path` to the set of selectively-synced items in an
    existing ignore list, keeping include ("!"-prefixed) patterns ahead of
    the catch-all deny so Syncthing's first-match-wins evaluation lets the
    included subfolders through. Convention taken from Syncthing's own
    documented "sync only these items" recipe — not independently verified
    against a live instance.
    """
    includes = [p for p in existing if p.startswith("!")]
    denies = [p for p in existing if not p.startswith("!")]
    for pattern in (f"!/{subfolder_path}", f"!/{subfolder_path}/**"):
        if pattern not in includes:
            includes.append(pattern)
    if "/*" not in denies:
        denies.append("/*")
    return includes + denies


@app.post("/api/folders/{inst_id}/{folder_id}/push-subfolder")
async def push_subfolder(inst_id: str, folder_id: str, body: PushSubfolderRequest):
    """
    Pushes a single subfolder of an already-synced main folder to another
    instance via Syncthing's selective sync, rather than creating a second,
    independently-registered folder (which Syncthing would reject as nested
    inside the main folder's own path). The first subfolder pushed for a
    given (folder, target) pair shares the whole main folder and sets it to
    sync nothing by default; every subsequent subfolder pushed to the same
    target just widens what's selectively synced there.
    """
    source_inst = _get_instance(inst_id)
    target_inst = _get_instance(body.target_instance_id)
    steps: list[dict] = []

    existing = store.find_subfolder_push(inst_id, folder_id, body.target_instance_id)
    first_share = existing is None

    if first_share:
        if not body.target_path:
            raise HTTPException(
                400,
                "target_path is required the first time a subfolder of this "
                "folder is pushed to this instance",
            )
        # Created paused: without this, the target would sync the whole
        # folder in the window between it being created here and the
        # selective-sync ignore patterns being applied below.
        result = await _ensure_folder_shared(
            source_inst, target_inst, folder_id, body.target_path, create_paused=True,
        )
        steps.extend(result["steps"])
        if not result["ok"]:
            response: dict[str, Any] = {"ok": False, "steps": steps}
            if "rollback" in result:
                response["rollback"] = result["rollback"]
            return response
        target_path = body.target_path
    else:
        target_path = existing["target_path"]
        steps.append({
            "description": f"Reusing existing share of '{folder_id}' with {target_inst['name']}",
            "ok": True,
        })

    try:
        current_ignores = await st.get_ignores(target_inst["url"], target_inst["api_key"], folder_id)
        new_ignores = _selective_sync_ignores(current_ignores, body.subfolder_path)
        await st.set_ignores(target_inst["url"], target_inst["api_key"], folder_id, new_ignores)
        steps.append({
            "description": f"Enabled '{body.subfolder_path}' for selective sync on target",
            "ok": True,
        })
    except Exception as e:
        detail = "Folder was left paused on target so nothing synced before this was fixed." if first_share else ""
        steps.append({
            "description": "Update selective-sync patterns on target",
            "ok": False, "error": f"{e} {detail}".strip(),
        })
        return {"ok": False, "steps": steps}

    if first_share:
        try:
            await st.resume_folder(target_inst["url"], target_inst["api_key"], folder_id)
            steps.append({"description": "Resumed folder on target now that selective sync is set", "ok": True})
        except Exception as e:
            steps.append({
                "description": "Resume folder on target", "ok": False,
                "error": f"{e} Selective-sync patterns are set, but you'll need to resume the "
                         f"'{folder_id}' folder on {target_inst['name']} manually.",
            })
            return {"ok": False, "steps": steps}

    try:
        src_folder = await st.get_config_folder(source_inst["url"], source_inst["api_key"], folder_id)
        folder_label = src_folder.get("label", folder_id)
        browse_raw = await st.get_db_browse(
            source_inst["url"], source_inst["api_key"], folder_id,
            prefix=body.subfolder_path, levels=100,
        )
        total_bytes = _sum_browse_size(browse_raw)
    except Exception as e:
        logger.debug("Could not size subfolder %s on push: %s", body.subfolder_path, e)
        folder_label, total_bytes = folder_id, 0

    store.add_subfolder_push(
        inst_id, folder_id, folder_label, body.subfolder_path,
        body.target_instance_id, target_path, total_bytes,
    )
    await _refresh_all()
    return {"ok": True, "steps": steps}


@app.delete("/api/folders/{inst_id}/{folder_id}/push-subfolder", status_code=204)
async def unpush_subfolder(inst_id: str, folder_id: str, subfolder_path: str, target_instance_id: str):
    """Stops selectively syncing this subfolder on the target and forgets the mapping."""
    target_inst = _get_instance(target_instance_id)
    try:
        current_ignores = await st.get_ignores(target_inst["url"], target_inst["api_key"], folder_id)
        remaining = [
            p for p in current_ignores
            if p not in (f"!/{subfolder_path}", f"!/{subfolder_path}/**")
        ]
        await st.set_ignores(target_inst["url"], target_inst["api_key"], folder_id, remaining)
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, e.response.text)
    except Exception as e:
        raise HTTPException(500, str(e))
    store.remove_subfolder_push(inst_id, folder_id, subfolder_path, target_instance_id)
    await _refresh_all()


# ── Static frontend ─────────────────────────────────────────────────────────────
WEB_DIR = Path(__file__).parent / "web"

# html=True makes StaticFiles serve index.html for / and unknown paths
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="static")


# ── Helpers ────────────────────────────────────────────────────────────────────
def _redact(inst: dict) -> dict:
    return {k: v for k, v in inst.items() if k != "api_key"}


def _get_instance(inst_id: str) -> dict:
    for inst in store.load_instances():
        if inst["id"] == inst_id:
            return inst
    raise HTTPException(404, f"Instance '{inst_id}' not found")
