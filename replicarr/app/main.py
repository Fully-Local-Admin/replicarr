"""
Replicarr — FastAPI backend.

All Syncthing API keys stay server-side.
The browser talks only to /api/* and the static web/ directory.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import store
import syncthing as st

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("replicarr")

# ── Speed sampler state ────────────────────────────────────────────────────────
# { instance_id: { "ts": float, "inBytes": int, "outBytes": int } }
_byte_samples: dict[str, dict[str, Any]] = {}
# { instance_id + folder_id: { "ts": float, "needBytes": int } }
_folder_samples: dict[str, dict[str, Any]] = {}
_sampler_task: asyncio.Task | None = None

EWMA_ALPHA = 0.3  # smoothing factor for byte-rate EWMA
# { key: smoothed_rate_bytes_per_sec }
_smoothed_rates: dict[str, float] = {}


def _ewma(key: str, new_rate: float) -> float:
    prev = _smoothed_rates.get(key, new_rate)
    smoothed = EWMA_ALPHA * new_rate + (1 - EWMA_ALPHA) * prev
    _smoothed_rates[key] = smoothed
    return smoothed


async def _sample_loop() -> None:
    while True:
        await asyncio.sleep(2)
        instances = store.load_instances()
        for inst in instances:
            iid = inst["id"]
            url, key = inst["url"], inst["api_key"]
            try:
                conn = await st.get_system_connections(url, key)
                total = conn.get("total", {})
                in_b = total.get("inBytesTotal", 0)
                out_b = total.get("outBytesTotal", 0)
                now = time.monotonic()
                prev = _byte_samples.get(iid)
                if prev:
                    dt = now - prev["ts"]
                    if dt > 0:
                        in_rate = (in_b - prev["inBytes"]) / dt
                        out_rate = (out_b - prev["outBytes"]) / dt
                        _ewma(f"{iid}:in", max(0, in_rate))
                        _ewma(f"{iid}:out", max(0, out_rate))
                _byte_samples[iid] = {"ts": now, "inBytes": in_b, "outBytes": out_b}

                folders = await st.get_config_folders(url, key)
                for fdr in folders:
                    fid = fdr["id"]
                    fkey = f"{iid}:{fid}"
                    try:
                        dbs = await st.get_db_status(url, key, fid)
                        need = dbs.get("needBytes", 0)
                        now2 = time.monotonic()
                        prev_f = _folder_samples.get(fkey)
                        if prev_f:
                            dt2 = now2 - prev_f["ts"]
                            if dt2 > 0:
                                delta = prev_f["needBytes"] - need  # falling = progress
                                _ewma(f"{fkey}:speed", max(0, delta / dt2))
                        _folder_samples[fkey] = {"ts": now2, "needBytes": need}
                    except Exception:
                        pass
            except Exception:
                pass


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
            pass
    store.merge_config_instances(config_instances)
    logger.info("Replicarr started. Instances loaded.")

    _sampler_task = asyncio.create_task(_sample_loop())
    yield
    _sampler_task.cancel()


# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Replicarr", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def ingress_root_path(request: Request, call_next):
    ingress_path = request.headers.get("X-Ingress-Path", "")
    if ingress_path:
        request.scope["root_path"] = ingress_path
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
        return _redact(inst)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.put("/api/instances/{inst_id}")
async def update_instance(inst_id: str, body: InstanceUpdate):
    try:
        inst = store.update_instance(inst_id, body.name, body.url, body.api_key)
        return _redact(inst)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.delete("/api/instances/{inst_id}", status_code=204)
async def delete_instance(inst_id: str):
    try:
        store.delete_instance(inst_id)
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
    """
    Returns per-instance status: folders, devices, sync state, sizes.
    Fan-out is concurrent per instance; an offline instance returns an error block.
    """
    instances = store.load_instances()
    results = await asyncio.gather(*[_fetch_instance_status(i) for i in instances])
    return results


async def _fetch_instance_status(inst: dict) -> dict:
    url, key, iid = inst["url"], inst["api_key"], inst["id"]
    base = {"id": iid, "name": inst["name"], "source": inst["source"]}
    try:
        system_status, folders, devices, connections = await asyncio.gather(
            st.get_system_status(url, key),
            st.get_config_folders(url, key),
            st.get_config_devices(url, key),
            st.get_system_connections(url, key),
        )
        my_id = system_status.get("myID", "")
        conn_map = connections.get("connections", {})

        folder_data = await asyncio.gather(*[
            _fetch_folder_status(url, key, f, my_id, conn_map)
            for f in folders
        ])

        return {
            **base,
            "online": True,
            "myID": my_id,
            "version": system_status.get("version"),
            "folders": folder_data,
            "devices": [_device_info(d, conn_map) for d in devices],
        }
    except httpx.HTTPStatusError as e:
        return {**base, "online": False, "error": f"HTTP {e.response.status_code}"}
    except Exception as e:
        return {**base, "online": False, "error": str(e)}


async def _fetch_folder_status(
    url: str, key: str, folder: dict, my_id: str, conn_map: dict
) -> dict:
    fid = folder["id"]
    try:
        dbs = await st.get_db_status(url, key, fid)
        state = dbs.get("state", "unknown")
        global_bytes = dbs.get("globalBytes", 0)
        need_bytes = dbs.get("needBytes", 0)
        in_sync = dbs.get("inSyncBytes", 0)
        pct = round((in_sync / global_bytes * 100) if global_bytes else 100, 1)
        return {
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
        }
    except Exception as e:
        return {
            "id": fid,
            "label": folder.get("label", fid),
            "paused": folder.get("paused", False),
            "error": str(e),
        }


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
    instances = store.load_instances()
    inst_results = await asyncio.gather(*[_fetch_instance_transfers(i) for i in instances])

    overall_need = 0
    overall_total = 0
    overall_in_speed = 0.0
    overall_out_speed = 0.0
    for ir in inst_results:
        overall_in_speed  += ir.get("_in_speed", 0.0)
        overall_out_speed += ir.get("_out_speed", 0.0)
        for f in ir.get("folders", []):
            overall_need  += f.get("needBytes", 0)
            overall_total += f.get("totalBytes", 0)

    # Strip internal speed fields before returning
    result = [{k: v for k, v in ir.items() if not k.startswith("_")} for ir in inst_results]

    overall_pct = round(
        ((overall_total - overall_need) / overall_total * 100) if overall_total else 100, 1
    )
    overall_eta = (
        int(overall_need / overall_in_speed) if overall_in_speed > 0 and overall_need > 0 else None
    )
    return {
        "instances": result,
        "overall": {
            "totalBytes": overall_total,
            "needBytes": overall_need,
            "percent": overall_pct,
            "inSpeedBytesPerSec": round(overall_in_speed, 1),
            "outSpeedBytesPerSec": round(overall_out_speed, 1),
            "etaSeconds": overall_eta,
        },
    }


async def _fetch_instance_transfers(inst: dict) -> dict:
    iid = inst["id"]
    url, key = inst["url"], inst["api_key"]
    in_speed  = _smoothed_rates.get(f"{iid}:in", 0.0)
    out_speed = _smoothed_rates.get(f"{iid}:out", 0.0)
    try:
        folders = await st.get_config_folders(url, key)
        folder_statuses = await asyncio.gather(*[
            st.get_db_status(url, key, fdr["id"]) for fdr in folders
        ], return_exceptions=True)

        folder_metrics = []
        for fdr, dbs in zip(folders, folder_statuses):
            fid = fdr["id"]
            fkey = f"{iid}:{fid}"
            if isinstance(dbs, Exception):
                folder_metrics.append({"id": fid, "error": str(dbs)})
                continue
            global_b = dbs.get("globalBytes", 0)
            need_b   = dbs.get("needBytes", 0)
            in_sync  = dbs.get("inSyncBytes", 0)
            pct      = round((in_sync / global_b * 100) if global_b else 100, 1)
            speed    = _smoothed_rates.get(f"{fkey}:speed", 0.0)
            eta      = int(need_b / speed) if speed > 0 and need_b > 0 else None
            folder_metrics.append({
                "id": fid,
                "label": fdr.get("label", fid),
                "paused": fdr.get("paused", False),
                "state": dbs.get("state", "unknown"),
                "percent": pct,
                "totalBytes": global_b,
                "needBytes": need_b,
                "speedBytesPerSec": round(speed, 1),
                "speedApproximate": True,
                "etaSeconds": eta,
            })
        return {"instanceId": iid, "folders": folder_metrics, "_in_speed": in_speed, "_out_speed": out_speed}
    except Exception:
        return {"instanceId": iid, "folders": [], "offline": True, "_in_speed": 0.0, "_out_speed": 0.0}


# ── Pause / resume folder ─────────────────────────────────────────────────────
@app.post("/api/folders/{inst_id}/{folder_id}/pause")
async def pause_folder(inst_id: str, folder_id: str):
    inst = _get_instance(inst_id)
    try:
        await st.pause_folder(inst["url"], inst["api_key"], folder_id)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/folders/{inst_id}/{folder_id}/resume")
async def resume_folder(inst_id: str, folder_id: str):
    inst = _get_instance(inst_id)
    try:
        await st.resume_folder(inst["url"], inst["api_key"], folder_id)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Pause / resume device ─────────────────────────────────────────────────────
@app.post("/api/devices/{inst_id}/{device_id}/pause")
async def pause_device(inst_id: str, device_id: str):
    inst = _get_instance(inst_id)
    try:
        await st.pause_device(inst["url"], inst["api_key"], device_id)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/devices/{inst_id}/{device_id}/resume")
async def resume_device(inst_id: str, device_id: str):
    inst = _get_instance(inst_id)
    try:
        await st.resume_device(inst["url"], inst["api_key"], device_id)
        return {"ok": True}
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
        return {"ok": True, "restartRequired": rr.get("requiresRestart", False)}
    except httpx.HTTPStatusError as e:
        raise HTTPException(e.response.status_code, e.response.text)
    except Exception as e:
        raise HTTPException(500, str(e))


# ── Push (share folder to another instance) ───────────────────────────────────
@app.post("/api/folders/{inst_id}/{folder_id}/push")
async def push_folder(inst_id: str, folder_id: str, body: PushRequest):
    """
    5-step folder-sharing flow from source (inst_id) to target.
    Returns a step-by-step result list.
    """
    source_inst = _get_instance(inst_id)
    target_inst = _get_instance(body.target_instance_id)
    steps: list[dict] = []

    try:
        # Step 1: Get device IDs
        src_status = await st.get_system_status(source_inst["url"], source_inst["api_key"])
        tgt_status = await st.get_system_status(target_inst["url"], target_inst["api_key"])
        src_id = src_status["myID"]
        tgt_id = tgt_status["myID"]
        steps.append({"step": 1, "description": "Got device IDs", "ok": True,
                       "sourceDeviceID": src_id, "targetDeviceID": tgt_id})
    except Exception as e:
        steps.append({"step": 1, "description": "Get device IDs", "ok": False, "error": str(e)})
        return {"ok": False, "steps": steps}

    try:
        # Step 2a: Add target as device on source
        tgt_dev_cfg = {"deviceID": tgt_id, "name": target_inst["name"],
                       "addresses": ["dynamic"], "compression": "metadata",
                       "introducer": False, "skipIntroductionRemovals": False,
                       "introducedBy": "", "paused": False, "allowedNetworks": [],
                       "autoAcceptFolders": False, "maxSendKbps": 0, "maxRecvKbps": 0,
                       "ignoredFolders": [], "maxRequestKiB": 0, "untrustedIntroducer": False}
        await st.put_config_device(source_inst["url"], source_inst["api_key"], tgt_id, tgt_dev_cfg)
        steps.append({"step": 2, "description": "Registered target device on source", "ok": True})
    except Exception as e:
        steps.append({"step": 2, "description": "Register target on source", "ok": False, "error": str(e)})
        return {"ok": False, "steps": steps}

    try:
        # Step 2b: Add source as device on target
        src_dev_cfg = {"deviceID": src_id, "name": source_inst["name"],
                       "addresses": ["dynamic"], "compression": "metadata",
                       "introducer": False, "skipIntroductionRemovals": False,
                       "introducedBy": "", "paused": False, "allowedNetworks": [],
                       "autoAcceptFolders": False, "maxSendKbps": 0, "maxRecvKbps": 0,
                       "ignoredFolders": [], "maxRequestKiB": 0, "untrustedIntroducer": False}
        await st.put_config_device(target_inst["url"], target_inst["api_key"], src_id, src_dev_cfg)
        steps.append({"step": 2, "description": "Registered source device on target", "ok": True})
    except Exception as e:
        steps.append({"step": 2, "description": "Register source on target", "ok": False, "error": str(e)})
        return {"ok": False, "steps": steps}

    try:
        # Step 3: Add target to folder's device list on source (read-modify-write)
        folder_cfg = await st.get_config_folder(source_inst["url"], source_inst["api_key"], folder_id)
        existing_ids = [d["deviceID"] for d in folder_cfg.get("devices", [])]
        if tgt_id not in existing_ids:
            folder_cfg.setdefault("devices", []).append(
                {"deviceID": tgt_id, "introducedBy": "", "encryptionPassword": ""}
            )
            await st.put_config_folder(source_inst["url"], source_inst["api_key"], folder_id, folder_cfg)
        steps.append({"step": 3, "description": "Shared folder with target device on source", "ok": True})
    except Exception as e:
        steps.append({"step": 3, "description": "Share folder on source", "ok": False, "error": str(e)})
        return {"ok": False, "steps": steps}

    try:
        # Step 4: Recreate folder on target with same ID
        defaults = await st.get_default_folder(target_inst["url"], target_inst["api_key"])
        src_folder = await st.get_config_folder(source_inst["url"], source_inst["api_key"], folder_id)
        new_folder = {
            **defaults,
            "id": folder_id,
            "label": src_folder.get("label", folder_id),
            "path": body.target_path,
            "devices": [
                {"deviceID": src_id, "introducedBy": "", "encryptionPassword": ""}
            ],
            "type": src_folder.get("type", "sendreceive"),
            "paused": False,
        }
        await st.put_config_folder(target_inst["url"], target_inst["api_key"], folder_id, new_folder)
        steps.append({"step": 4, "description": "Created folder on target", "ok": True})
    except Exception as e:
        steps.append({"step": 4, "description": "Create folder on target", "ok": False, "error": str(e)})
        return {"ok": False, "steps": steps}

    try:
        # Step 5: Check restart required on both
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

    return {"ok": True, "steps": steps}


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
