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
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_PATH = Path(os.environ.get("DATA_PATH", "/data"))
INSTANCES_FILE = DATA_PATH / "instances.json"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _load_raw() -> list[dict[str, Any]]:
    if not INSTANCES_FILE.exists():
        return []
    try:
        return json.loads(INSTANCES_FILE.read_text())
    except Exception:
        logger.warning("Could not read %s — starting with empty list", INSTANCES_FILE)
        return []


def _save_raw(instances: list[dict[str, Any]]) -> None:
    DATA_PATH.mkdir(parents=True, exist_ok=True)
    tmp = INSTANCES_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(instances, indent=2))
    tmp.chmod(0o600)
    tmp.rename(INSTANCES_FILE)


def load_instances() -> list[dict[str, Any]]:
    return _load_raw()


def save_instances(instances: list[dict[str, Any]]) -> None:
    _save_raw(instances)


def merge_config_instances(
    config_instances: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Merge config-defined instances into the persistent store.
    Returns the merged list (also persisted).
    """
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
    instances = _load_raw()
    for inst in instances:
        if inst["id"] == inst_id:
            if inst["source"] == "config":
                raise PermissionError("Cannot delete a config-managed instance")
            instances.remove(inst)
            _save_raw(instances)
            return
    raise KeyError(f"Instance '{inst_id}' not found")
