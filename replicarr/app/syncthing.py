"""
Async httpx wrapper for the Syncthing REST API.
All API keys stay server-side — never returned to the browser.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(10.0)


def _client(url: str, api_key: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=url,
        headers={"X-API-Key": api_key},
        timeout=TIMEOUT,
        verify=False,  # self-signed certs common on home instances
    )


async def get_system_status(url: str, api_key: str) -> dict[str, Any]:
    async with _client(url, api_key) as c:
        r = await c.get("/rest/system/status")
        r.raise_for_status()
        return r.json()


async def get_config_folders(url: str, api_key: str) -> list[dict[str, Any]]:
    async with _client(url, api_key) as c:
        r = await c.get("/rest/config/folders")
        r.raise_for_status()
        return r.json()


async def get_config_devices(url: str, api_key: str) -> list[dict[str, Any]]:
    async with _client(url, api_key) as c:
        r = await c.get("/rest/config/devices")
        r.raise_for_status()
        return r.json()


async def get_db_status(url: str, api_key: str, folder_id: str) -> dict[str, Any]:
    async with _client(url, api_key) as c:
        r = await c.get("/rest/db/status", params={"folder": folder_id})
        r.raise_for_status()
        return r.json()


async def get_db_completion(
    url: str, api_key: str, folder_id: str, device_id: str
) -> dict[str, Any]:
    async with _client(url, api_key) as c:
        r = await c.get(
            "/rest/db/completion",
            params={"folder": folder_id, "device": device_id},
        )
        r.raise_for_status()
        return r.json()


async def get_system_connections(url: str, api_key: str) -> dict[str, Any]:
    async with _client(url, api_key) as c:
        r = await c.get("/rest/system/connections")
        r.raise_for_status()
        return r.json()


async def get_restart_required(url: str, api_key: str) -> dict[str, Any]:
    async with _client(url, api_key) as c:
        r = await c.get("/rest/config/restart-required")
        r.raise_for_status()
        return r.json()


async def get_default_folder(url: str, api_key: str) -> dict[str, Any]:
    async with _client(url, api_key) as c:
        r = await c.get("/rest/config/defaults/folder")
        r.raise_for_status()
        return r.json()


async def put_config_folder(
    url: str, api_key: str, folder_id: str, folder_cfg: dict[str, Any]
) -> dict[str, Any]:
    async with _client(url, api_key) as c:
        r = await c.put(f"/rest/config/folders/{folder_id}", json=folder_cfg)
        r.raise_for_status()
        return r.json() if r.content else {}


async def get_config_folder(
    url: str, api_key: str, folder_id: str
) -> dict[str, Any]:
    async with _client(url, api_key) as c:
        r = await c.get(f"/rest/config/folders/{folder_id}")
        r.raise_for_status()
        return r.json()


async def get_config_device(
    url: str, api_key: str, device_id: str
) -> dict[str, Any] | None:
    async with _client(url, api_key) as c:
        r = await c.get(f"/rest/config/devices/{device_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()


async def put_config_device(
    url: str, api_key: str, device_id: str, device_cfg: dict[str, Any]
) -> dict[str, Any]:
    async with _client(url, api_key) as c:
        r = await c.put(f"/rest/config/devices/{device_id}", json=device_cfg)
        r.raise_for_status()
        return r.json() if r.content else {}


async def pause_folder(url: str, api_key: str, folder_id: str) -> None:
    """Read-modify-write to set paused=true — never send a partial PATCH."""
    folder_cfg = await get_config_folder(url, api_key, folder_id)
    folder_cfg["paused"] = True
    await put_config_folder(url, api_key, folder_id, folder_cfg)


async def resume_folder(url: str, api_key: str, folder_id: str) -> None:
    folder_cfg = await get_config_folder(url, api_key, folder_id)
    folder_cfg["paused"] = False
    await put_config_folder(url, api_key, folder_id, folder_cfg)


async def pause_device(url: str, api_key: str, device_id: str) -> None:
    async with _client(url, api_key) as c:
        r = await c.post("/rest/system/pause", params={"device": device_id})
        r.raise_for_status()


async def resume_device(url: str, api_key: str, device_id: str) -> None:
    async with _client(url, api_key) as c:
        r = await c.post("/rest/system/resume", params={"device": device_id})
        r.raise_for_status()


async def post_db_scan(url: str, api_key: str, folder_id: str) -> None:
    async with _client(url, api_key) as c:
        r = await c.post("/rest/db/scan", params={"folder": folder_id})
        r.raise_for_status()
