"""
Persistence layer for Replicarr instance config.

Instances are stored in /data/instances.json with mode 0600.
On startup, config-defined instances (from add-on options) are merged in
using the rules defined in the prompt spec:
  - source=="config" instances are overwritten from config (config wins)
  - source=="ui" instances are never touched by config merge
  - config instances appear locked in the UI
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_PATH = Path(os.environ.get("DATA_PATH", "/data"))
INSTANCES_FILE = DATA_PATH / "instances.json"
SUBFOLDER_PUSHES_FILE = DATA_PATH / "subfolder_pushes.json"
FOLDER_ORDERS_FILE = DATA_PATH / "folder_orders.json"

# Guards read-modify-write of the JSON stores. FastAPI's single-threaded
# event loop already serializes these calls in practice (none of them await
# mid-function), but the lock makes that a guarantee rather than an accident
# of the current implementation, and costs nothing at this scale.
_lock = threading.Lock()


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _read_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        backup = path.with_name(f"{path.name}.bak.{int(time.time())}")
        try:
            path.rename(backup)
            logger.error(
                "Could not parse %s — backed up as %s and starting with an empty list",
                path, backup,
            )
        except Exception:
            logger.error(
                "Could not parse %s and could not back it up — starting with an empty list",
                path,
            )
        return []


def _write_json_list(path: Path, items: list[dict[str, Any]]) -> None:
    DATA_PATH.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, indent=2))
    tmp.chmod(0o600)
    tmp.rename(path)


def _load_raw() -> list[dict[str, Any]]:
    return _read_json_list(INSTANCES_FILE)


def _save_raw(instances: list[dict[str, Any]]) -> None:
    _write_json_list(INSTANCES_FILE, instances)


def load_instances() -> list[dict[str, Any]]:
    return _load_raw()


def load_folder_orders() -> dict[str, list[str]]:
    orders: dict[str, list[str]] = {}
    for entry in _read_json_list(FOLDER_ORDERS_FILE):
        instance_id = entry.get("instance_id")
        folder_ids = entry.get("folder_ids")
        if isinstance(instance_id, str) and isinstance(folder_ids, list):
            orders[instance_id] = [fid for fid in folder_ids if isinstance(fid, str)]
    return orders


def save_folder_order(instance_id: str, folder_ids: list[str]) -> list[str]:
    clean_ids = list(dict.fromkeys(fid for fid in folder_ids if isinstance(fid, str) and fid))
    with _lock:
        entries = _read_json_list(FOLDER_ORDERS_FILE)
        entries = [entry for entry in entries if entry.get("instance_id") != instance_id]
        entries.append({"instance_id": instance_id, "folder_ids": clean_ids})
        _write_json_list(FOLDER_ORDERS_FILE, entries)
    return clean_ids


def merge_config_instances(
    config_instances: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Merge config-defined instances into the persistent store.
    Returns the merged list (also persisted).
    """
    with _lock:
        current = {inst["id"]: inst for inst in _load_raw()}

        for cfg in config_instances:
            inst_id = _slug(cfg["name"])
            entry = {
                "id": inst_id,
                "name": cfg["name"],
                "url": cfg["url"].rstrip("/"),
                "api_key": cfg["api_key"],
                "source": "config",
            }
            existing = current.get(inst_id)
            if existing is None or existing.get("source") == "config":
                current[inst_id] = entry
            # source=="ui" entries are never touched

        merged = list(current.values())
        _save_raw(merged)
        return merged


def add_instance(name: str, url: str, api_key: str) -> dict[str, Any]:
    with _lock:
        instances = _load_raw()
        inst_id = _slug(name)
        if any(i["id"] == inst_id for i in instances):
            raise ValueError(f"Instance with id '{inst_id}' already exists")
        entry: dict[str, Any] = {
            "id": inst_id,
            "name": name,
            "url": url.rstrip("/"),
            "api_key": api_key,
            "source": "ui",
        }
        instances.append(entry)
        _save_raw(instances)
        return entry


def update_instance(inst_id: str, name: str, url: str, api_key: str) -> dict[str, Any]:
    with _lock:
        instances = _load_raw()
        for i, inst in enumerate(instances):
            if inst["id"] == inst_id:
                if inst["source"] == "config":
                    raise PermissionError("Cannot edit a config-managed instance")
                instances[i] = {
                    "id": inst_id,
                    "name": name,
                    "url": url.rstrip("/"),
                    "api_key": api_key,
                    "source": "ui",
                }
                _save_raw(instances)
                return instances[i]
        raise KeyError(f"Instance '{inst_id}' not found")


def delete_instance(inst_id: str) -> None:
    with _lock:
        instances = _load_raw()
        for inst in instances:
            if inst["id"] == inst_id:
                if inst["source"] == "config":
                    raise PermissionError("Cannot delete a config-managed instance")
                instances.remove(inst)
                _save_raw(instances)
                orders = _read_json_list(FOLDER_ORDERS_FILE)
                remaining_orders = [
                    entry for entry in orders if entry.get("instance_id") != inst_id
                ]
                if len(remaining_orders) != len(orders):
                    _write_json_list(FOLDER_ORDERS_FILE, remaining_orders)
                return
        raise KeyError(f"Instance '{inst_id}' not found")


# ── Subfolder pushes ─────────────────────────────────────────────────────────
# Syncthing has no notion of "this subfolder came from a push" — a subfolder
# push is really the parent folder shared normally, with the target's copy
# selectively syncing just that subfolder via ignore patterns. Replicarr has
# to remember the mapping itself so the Transfers view can show a from/to
# pair, and so a second subfolder pushed to an already-shared target knows to
# widen the existing share instead of re-registering devices from scratch.

def load_subfolder_pushes() -> list[dict[str, Any]]:
    return _read_json_list(SUBFOLDER_PUSHES_FILE)


def find_subfolder_push(
    source_instance_id: str, folder_id: str, target_instance_id: str
) -> dict[str, Any] | None:
    """Finds any existing subfolder push that already shares this main folder with this target."""
    for p in load_subfolder_pushes():
        if (
            p["source_instance_id"] == source_instance_id
            and p["folder_id"] == folder_id
            and p["target_instance_id"] == target_instance_id
        ):
            return p
    return None


def add_subfolder_push(
    source_instance_id: str,
    folder_id: str,
    folder_label: str,
    subfolder_path: str,
    target_instance_id: str,
    target_path: str,
    total_bytes: int,
) -> dict[str, Any]:
    with _lock:
        pushes = _read_json_list(SUBFOLDER_PUSHES_FILE)
        for p in pushes:
            if (
                p["source_instance_id"] == source_instance_id
                and p["folder_id"] == folder_id
                and p["subfolder_path"] == subfolder_path
                and p["target_instance_id"] == target_instance_id
            ):
                p["target_path"] = target_path
                p["total_bytes"] = total_bytes
                _write_json_list(SUBFOLDER_PUSHES_FILE, pushes)
                return p
        entry: dict[str, Any] = {
            "source_instance_id": source_instance_id,
            "folder_id": folder_id,
            "folder_label": folder_label,
            "subfolder_path": subfolder_path,
            "target_instance_id": target_instance_id,
            "target_path": target_path,
            "total_bytes": total_bytes,
            "created_at": time.time(),
        }
        pushes.append(entry)
        _write_json_list(SUBFOLDER_PUSHES_FILE, pushes)
        return entry


def remove_subfolder_push(
    source_instance_id: str, folder_id: str, subfolder_path: str, target_instance_id: str
) -> None:
    with _lock:
        pushes = _read_json_list(SUBFOLDER_PUSHES_FILE)
        remaining = [
            p for p in pushes
            if not (
                p["source_instance_id"] == source_instance_id
                and p["folder_id"] == folder_id
                and p["subfolder_path"] == subfolder_path
                and p["target_instance_id"] == target_instance_id
            )
        ]
        _write_json_list(SUBFOLDER_PUSHES_FILE, remaining)


def remove_subfolder_pushes_for_instance(instance_id: str) -> None:
    """Called when an instance is deleted, so pushes don't point at a ghost instance."""
    with _lock:
        pushes = _read_json_list(SUBFOLDER_PUSHES_FILE)
        remaining = [
            p for p in pushes
            if p["source_instance_id"] != instance_id and p["target_instance_id"] != instance_id
        ]
        if len(remaining) != len(pushes):
            _write_json_list(SUBFOLDER_PUSHES_FILE, remaining)
