"""
Replicarr — FastAPI backend.

All Syncthing API keys stay server-side.
The browser talks only to /api/* and the static web/ directory.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import store
import syncthing as st

# ── Logging ────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("replicarr")

# ── Direct-access login sessions ───────────────────────────────────────────────
# Requests that arrive via Home Assistant Ingress are already authenticated by
# HA (identified by the X-Ingress-Path header, which only the Supervisor's
# proxy sets). Requests hitting the add-on's directly-published port carry no
# such header and no HA session, so they're gated behind a form login instead.
BASIC_AUTH_USERNAME = os.environ.get("BASIC_AUTH_USERNAME", "")
BASIC_AUTH_PASSWORD = os.environ.get("BASIC_AUTH_PASSWORD", "")
SESSION_COOKIE = "replicarr_session"
SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 15 * 60
WEB_DIR = Path(__file__).parent / "web"

_direct_sessions: dict[str, float] = {}
_login_failures: dict[str, list[float]] = {}
_login_lockouts: dict[str, float] = {}

# ── Shared status/transfers cache ───────────────────────────────────────────────
# A single background refresh cycle owns all outbound Syncthing REST calls.
# /api/status, /api/transfers, and /api/subfolder-transfers all read from
# these caches instead of independently re-fetching from every instance on
# every request — each browser tab polling separately would otherwise
# multiply the load on every configured Syncthing instance on top of this
# same sampler's own cycle.
#
# An SSE endpoint (/api/stream) briefly replaced polling here, pushing these
# caches to the frontend instead of it polling on a timer. It was removed
# after repeatedly causing the add-on to hang on shutdown/restart: a
# long-lived streaming connection is something uvicorn's graceful shutdown
# can end up waiting on indefinitely, and two different fixes attempted for
# that (waiting on a shutdown event; bounding the stream's own lifetime)
# both failed when actually tested live, even though each checked out in
# local testing first. Do not reintroduce a long-lived HTTP connection here
# without a way to verify the fix against a real restart, not just a
# simulated one — see CHANGELOG 0.3.0-0.3.3.
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
        "targetPath": push.get("target_path", ""),
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
    with suppress(asyncio.CancelledError):
        await _sampler_task


# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(title="Replicarr", lifespan=lifespan)


def _direct_access_disabled() -> JSONResponse:
    return JSONResponse(
        {
            "detail": "Direct access is disabled. Set 'basic_auth_username' and "
                      "'basic_auth_password' in the add-on configuration to allow "
                      "access outside Home Assistant Ingress."
        },
        status_code=403,
    )


def _request_is_https(request: Request) -> bool:
    forwarded = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    return request.url.scheme == "https" or forwarded == "https"


def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _active_session(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    now = time.time()
    expires_at = _direct_sessions.get(token)
    if expires_at is None:
        return None
    if expires_at <= now:
        _direct_sessions.pop(token, None)
        return None
    return token


def _same_origin_request(request: Request) -> bool:
    fetch_site = request.headers.get("Sec-Fetch-Site", "").lower()
    if fetch_site == "cross-site":
        return False
    origin = request.headers.get("Origin")
    if not origin:
        return True
    expected = f"{request.url.scheme}://{request.url.netloc}"
    forwarded_proto = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
    forwarded_host = request.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
    if forwarded_proto or forwarded_host:
        expected = f"{forwarded_proto or request.url.scheme}://{forwarded_host or request.url.netloc}"
    return secrets.compare_digest(origin.rstrip("/"), expected.rstrip("/"))


def _login_is_locked(client: str, now: float) -> bool:
    locked_until = _login_lockouts.get(client, 0)
    if locked_until > now:
        return True
    _login_lockouts.pop(client, None)
    recent = [stamp for stamp in _login_failures.get(client, []) if now - stamp < LOGIN_WINDOW_SECONDS]
    if recent:
        _login_failures[client] = recent
    else:
        _login_failures.pop(client, None)
    return False


def _record_login_failure(client: str, now: float) -> None:
    recent = [stamp for stamp in _login_failures.get(client, []) if now - stamp < LOGIN_WINDOW_SECONDS]
    recent.append(now)
    _login_failures[client] = recent
    if len(recent) >= LOGIN_MAX_FAILURES:
        _login_lockouts[client] = now + LOGIN_LOCKOUT_SECONDS
        _login_failures.pop(client, None)


@app.middleware("http")
async def ingress_or_direct_session(request: Request, call_next):
    ingress_path = request.headers.get("X-Ingress-Path", "")
    if ingress_path:
        # Trusted: only the Supervisor's Ingress proxy sets this header, and
        # HA has already authenticated the user's session to get here.
        request.scope["root_path"] = ingress_path
        return await call_next(request)

    if request.url.path == "/health":
        return await call_next(request)

    if not BASIC_AUTH_USERNAME or not BASIC_AUTH_PASSWORD:
        return _direct_access_disabled()

    if request.url.path == "/login":
        return await call_next(request)

    if not _active_session(request):
        if request.url.path.startswith("/api/") or request.method != "GET":
            return JSONResponse({"detail": "Authentication required."}, status_code=401)
        return RedirectResponse(url="./login", status_code=303)

    if request.method not in {"GET", "HEAD", "OPTIONS"} and not _same_origin_request(request):
        return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)

    return await call_next(request)


# ── Health ─────────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Direct-access login ────────────────────────────────────────────────────────
@app.get("/login")
async def login_page(request: Request):
    if _active_session(request):
        return RedirectResponse(url="./", status_code=303)
    return FileResponse(
        WEB_DIR / "login.html",
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@app.post("/login")
async def login(request: Request):
    if not _request_is_https(request):
        return RedirectResponse(url="./login?error=https", status_code=303)

    client = _client_key(request)
    now = time.time()
    if _login_is_locked(client, now):
        return RedirectResponse(url="./login?error=locked", status_code=303)

    try:
        content_length = int(request.headers.get("Content-Length", "0") or 0)
    except ValueError:
        content_length = 8193
    if content_length > 8192:
        _record_login_failure(client, now)
        return RedirectResponse(url="./login?error=invalid", status_code=303)

    try:
        form = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
        username = form.get("username", [""])[0]
        password = form.get("password", [""])[0]
    except (UnicodeDecodeError, ValueError):
        username = password = ""

    user_ok = secrets.compare_digest(username, BASIC_AUTH_USERNAME)
    password_ok = secrets.compare_digest(password, BASIC_AUTH_PASSWORD)
    if not (user_ok and password_ok):
        _record_login_failure(client, now)
        error = "locked" if _login_is_locked(client, now) else "invalid"
        return RedirectResponse(url=f"./login?error={error}", status_code=303)

    _login_failures.pop(client, None)
    _login_lockouts.pop(client, None)
    for expired_token, expires_at in list(_direct_sessions.items()):
        if expires_at <= now:
            _direct_sessions.pop(expired_token, None)
    token = secrets.token_urlsafe(32)
    _direct_sessions[token] = now + SESSION_TTL_SECONDS
    response = RedirectResponse(url="./", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    return response


@app.post("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        _direct_sessions.pop(token, None)
    response = JSONResponse({"ok": True})
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="strict")
    return response


@app.get("/api/auth/session")
async def auth_session(request: Request):
    return {"direct": not bool(request.headers.get("X-Ingress-Path", ""))}


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


class DiscoveredInstanceTestRequest(BaseModel):
    address: str
    api_key: str
    expected_device_id: str


class FolderOrderUpdate(BaseModel):
    folder_ids: list[str]


def _discovered_api_candidates(address: str) -> tuple[list[str], bool]:
    """Turn a live Syncthing transport address into safe GUI/API candidates."""
    value = address.strip()
    if not value:
        return [], False
    try:
        parsed = urlsplit(value if "://" in value else f"tcp://{value}")
        if parsed.scheme not in ("tcp", "quic") or not parsed.hostname:
            return [], False
        host = parsed.hostname
        ip = ipaddress.ip_address(host.split("%", 1)[0])
    except (ValueError, TypeError):
        return [], False

    url_host = f"[{host}]" if ":" in host else host
    public = ip.is_global
    candidates = [f"https://{url_host}:8384"]
    if not public:
        candidates.append(f"http://{url_host}:8384")
    return candidates, public


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


@app.post("/api/instances/_discover_test")
async def discovered_instance_test(body: DiscoveredInstanceTestRequest):
    """Find and verify a discovered peer's API without persisting credentials."""
    candidates, public = _discovered_api_candidates(body.address)
    if not candidates:
        return {
            "reachable": False,
            "ok": False,
            "error": "The live sync address cannot be used to locate this device's API.",
        }

    failures: list[str] = []
    for url in candidates:
        try:
            status = await asyncio.wait_for(
                st.get_system_status(url, body.api_key),
                timeout=4.0,
            )
            actual_id = status.get("myID", "")
            if actual_id != body.expected_device_id:
                return {
                    "reachable": True,
                    "ok": False,
                    "error": "The detected address belongs to a different Syncthing device.",
                    "expectedDeviceID": body.expected_device_id,
                    "actualDeviceID": actual_id,
                }
            return {
                "reachable": True,
                "ok": True,
                "url": url,
                "myID": actual_id,
                "version": status.get("version"),
            }
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403):
                return {
                    "reachable": True,
                    "ok": False,
                    "error": "The API key was rejected by the discovered device.",
                }
            failures.append(f"{url}: HTTP {e.response.status_code}")
        except Exception as e:
            failures.append(f"{url}: {e}")

    if public:
        error = (
            "The device has a public IP address and its HTTPS API could not be reached. "
            "Replicarr did not try plain HTTP because that would expose the API key. "
            "Use a VPN address or enable HTTPS for the Syncthing GUI."
        )
    else:
        error = (
            "Could not reach the Syncthing API on port 8384. On the remote device, "
            "make sure the GUI Listen Address is reachable from Replicarr and that "
            "the firewall allows the connection."
        )
    logger.debug("Discovered device probe failed: %s", "; ".join(failures))
    return {"reachable": False, "ok": False, "error": error}


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


# ── Replicarr UI preferences ──────────────────────────────────────────────────
@app.get("/api/folder-orders")
async def get_folder_orders():
    return store.load_folder_orders()


@app.put("/api/folder-orders/{inst_id}")
async def update_folder_order(inst_id: str, body: FolderOrderUpdate):
    if not any(inst["id"] == inst_id for inst in store.load_instances()):
        raise HTTPException(404, f"Instance '{inst_id}' not found")
    return {"folderIds": store.save_folder_order(inst_id, body.folder_ids)}


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
    if isinstance(raw, dict) and "name" not in raw and "Name" not in raw:
        for name, value in raw.items():
            full_path = f"{prefix}/{name}".strip("/") if prefix else name
            entries.append({
                "name": name,
                "path": full_path,
                "type": "dir" if isinstance(value, dict) else "file",
            })
        return entries

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


def _flatten_browse_directories(raw: Any, prefix: str = "") -> list[dict[str, str]]:
    """Flatten recursive v1 tree and v2 list-style browse responses to directories."""
    entries: list[dict[str, str]] = []

    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("Name") or ""
            if not name:
                continue
            full_path = name if prefix and name.startswith(f"{prefix}/") else (
                f"{prefix}/{name}".strip("/") if prefix else name
            )
            item_type = str(item.get("type") or item.get("Type") or "").upper()
            children = item.get("children", item.get("Children"))
            if "DIR" in item_type or children is not None:
                entries.append({"name": full_path.rsplit("/", 1)[-1], "path": full_path})
                if children is not None:
                    entries.extend(_flatten_browse_directories(children, full_path))
        return entries

    if isinstance(raw, dict):
        if "name" in raw or "Name" in raw:
            return _flatten_browse_directories([raw], prefix)

        # Syncthing v1 represents a tree as {name: subtree}; file values are arrays.
        for name, value in raw.items():
            if not isinstance(value, dict):
                continue
            full_path = f"{prefix}/{name}".strip("/") if prefix else name
            entries.append({"name": name, "path": full_path})
            entries.extend(_flatten_browse_directories(value, full_path))
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


@app.get("/api/search/subfolders")
async def search_subfolders(q: str):
    """Search directory paths across online managed folders on explicit request."""
    query = q.strip().casefold()
    if len(query) < 2:
        raise HTTPException(400, "Enter at least 2 characters")

    instances = {i["id"]: i for i in store.load_instances()}
    # A selective-sync target knows the source folder's entire global tree,
    # including paths its ignore rules deliberately keep off disk.  Treating
    # db/browse as a local filesystem listing therefore creates ghost search
    # results on the target.  Replicarr's push records are the authoritative
    # list of paths that may actually exist there.
    selective_target_paths: dict[tuple[str, str], list[str]] = {}
    for push in store.load_subfolder_pushes():
        key = (push["target_instance_id"], push["folder_id"])
        selective_target_paths.setdefault(key, []).append(
            push["subfolder_path"].strip("/")
        )

    jobs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for status in _status_cache:
        inst = instances.get(status["id"])
        if not inst or not status.get("online"):
            continue
        jobs.extend((inst, folder) for folder in status.get("folders", []))

    async def search_folder(inst: dict[str, Any], folder: dict[str, Any]) -> list[dict[str, str]]:
        raw = await st.get_db_browse(
            inst["url"], inst["api_key"], folder["id"], prefix="", levels=None
        )
        allowed_paths = selective_target_paths.get((inst["id"], folder["id"]))
        return [
            {
                "instanceId": inst["id"],
                "instanceName": inst["name"],
                "folderId": folder["id"],
                "folderLabel": folder.get("label", folder["id"]),
                "name": entry["name"],
                "path": entry["path"],
            }
            for entry in _flatten_browse_directories(raw)
            if query in entry["path"].casefold()
            and (
                allowed_paths is None
                or any(
                    entry["path"] == allowed
                    or entry["path"].startswith(f"{allowed}/")
                    for allowed in allowed_paths
                )
            )
        ]

    searched = await asyncio.gather(
        *(search_folder(inst, folder) for inst, folder in jobs),
        return_exceptions=True,
    )
    matches: list[dict[str, str]] = []
    failed_folders = 0
    for result in searched:
        if isinstance(result, BaseException):
            failed_folders += 1
        else:
            matches.extend(result)
    matches.sort(key=lambda m: (m["name"].casefold(), m["path"].casefold()))
    limit = 100
    return {
        "results": matches[:limit],
        "truncated": len(matches) > limit,
        "searchedFolders": len(jobs),
        "failedFolders": failed_folders,
    }


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
