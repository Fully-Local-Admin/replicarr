import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("DATA_PATH", tempfile.mkdtemp())

import main  # noqa: E402

HDR = {"X-Ingress-Path": "/x"}


class FakeInstance:
    def __init__(self, my_id):
        self.my_id = my_id
        self.devices: dict[str, dict] = {}
        self.folders: dict[str, dict] = {}
        self.ignores: dict[str, list[str]] = {}
        self.need: dict[str, dict] = {}


@pytest.fixture
def fakes(monkeypatch, tmp_path):
    monkeypatch.setattr(main.store, "DATA_PATH", tmp_path)
    monkeypatch.setattr(main.store, "INSTANCES_FILE", tmp_path / "instances.json")
    monkeypatch.setattr(main.store, "SUBFOLDER_PUSHES_FILE", tmp_path / "subfolder_pushes.json")

    registry = {
        "http://source": FakeInstance("SRC-ID"),
        "http://target": FakeInstance("TGT-ID"),
        "_events": [],  # ordered log of (event, ...) tuples for sequencing assertions
    }
    registry["http://source"].folders["tvshows"] = {
        "id": "tvshows", "label": "TV Shows", "devices": [], "type": "sendreceive",
    }

    async def fake_get_system_status(url, key):
        return {"myID": registry[url].my_id}

    async def fake_put_config_device(url, key, device_id, cfg):
        registry[url].devices[device_id] = cfg
        return {}

    async def fake_delete_config_device(url, key, device_id):
        registry[url].devices.pop(device_id, None)

    async def fake_get_config_folder(url, key, folder_id):
        return dict(registry[url].folders[folder_id])

    async def fake_put_config_folder(url, key, folder_id, cfg):
        registry[url].folders[folder_id] = cfg
        if url == "http://target":
            registry["_events"].append(("folder_paused_state", cfg.get("paused")))
        return {}

    async def fake_get_default_folder(url, key):
        return {"type": "sendreceive"}

    async def fake_get_ignores(url, key, folder_id):
        return list(registry[url].ignores.get(folder_id, []))

    async def fake_set_ignores(url, key, folder_id, patterns):
        registry[url].ignores[folder_id] = list(patterns)
        registry["_events"].append(("set_ignores", list(patterns)))

    async def fake_get_db_browse(url, key, folder_id, prefix="", levels=1):
        if prefix == "":
            return [{"name": "Paradise", "type": "FILE_INFO_TYPE_DIRECTORY"},
                     {"name": "Breaking Bad", "type": "FILE_INFO_TYPE_DIRECTORY"}]
        return [{"name": "s01e01.mkv", "size": 1000}]

    async def fake_get_db_need(url, key, folder_id):
        return registry[url].need.get(folder_id, {"progress": [], "queued": [], "rest": []})

    monkeypatch.setattr(main.st, "get_system_status", fake_get_system_status)
    monkeypatch.setattr(main.st, "put_config_device", fake_put_config_device)
    monkeypatch.setattr(main.st, "delete_config_device", fake_delete_config_device)
    monkeypatch.setattr(main.st, "get_config_folder", fake_get_config_folder)
    monkeypatch.setattr(main.st, "put_config_folder", fake_put_config_folder)
    monkeypatch.setattr(main.st, "get_default_folder", fake_get_default_folder)
    monkeypatch.setattr(main.st, "get_ignores", fake_get_ignores)
    monkeypatch.setattr(main.st, "set_ignores", fake_set_ignores)
    monkeypatch.setattr(main.st, "get_db_browse", fake_get_db_browse)
    monkeypatch.setattr(main.st, "get_db_need", fake_get_db_need)
    return registry


@pytest.fixture
def client(fakes):
    with TestClient(main.app) as c:
        c.post("/api/instances", json={"name": "Source", "url": "http://source", "api_key": "k"}, headers=HDR)
        c.post("/api/instances", json={"name": "Target", "url": "http://target", "api_key": "k"}, headers=HDR)
        yield c


def test_browse_lists_only_directories(client):
    r = client.get("/api/folders/source/tvshows/browse", headers=HDR)
    assert r.status_code == 200
    entries = r.json()["entries"]
    assert {e["path"] for e in entries} == {"Paradise", "Breaking Bad"}
    assert all(e["type"] == "dir" for e in entries)


def test_push_subfolder_requires_target_path_on_first_push(client):
    r = client.post(
        "/api/folders/source/tvshows/push-subfolder",
        json={"subfolder_path": "TV Shows/Paradise", "target_instance_id": "target"},
        headers=HDR,
    )
    assert r.status_code == 400


def test_first_push_creates_folder_paused_until_selective_sync_is_set(client, fakes):
    """
    Regression test: the target folder must never be briefly unpaused (i.e.
    fully syncing) before its selective-sync ignore patterns are in place,
    or Syncthing could start pulling the whole folder in that window.
    """
    r = client.post(
        "/api/folders/source/tvshows/push-subfolder",
        json={"subfolder_path": "TV Shows/Paradise", "target_instance_id": "target", "target_path": "/data/tv"},
        headers=HDR,
    )
    assert r.status_code == 200, r.text

    events = fakes["_events"]
    kinds = [e[0] for e in events]
    assert kinds == ["folder_paused_state", "set_ignores", "folder_paused_state"], events
    assert events[0] == ("folder_paused_state", True)   # created paused
    assert events[2] == ("folder_paused_state", False)  # resumed only after ignores were set
    assert fakes["http://target"].folders["tvshows"]["paused"] is False


def test_first_push_subfolder_shares_whole_folder_selectively(client, fakes):
    r = client.post(
        "/api/folders/source/tvshows/push-subfolder",
        json={
            "subfolder_path": "TV Shows/Paradise",
            "target_instance_id": "target",
            "target_path": "/data/tv",
        },
        headers=HDR,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True

    # The whole folder is now shared (device + folder registered on target)...
    assert "tvshows" in fakes["http://target"].folders
    assert fakes["http://target"].folders["tvshows"]["path"] == "/data/tv"
    # ...but only the pushed subfolder is selectively synced.
    assert fakes["http://target"].ignores["tvshows"] == [
        "!/TV Shows/Paradise", "!/TV Shows/Paradise/**", "/*",
    ]

    pushes = main.store.load_subfolder_pushes()
    assert len(pushes) == 1
    assert pushes[0]["subfolder_path"] == "TV Shows/Paradise"
    assert pushes[0]["target_path"] == "/data/tv"


def test_second_push_subfolder_reuses_share_and_widens_selective_sync(client, fakes):
    client.post(
        "/api/folders/source/tvshows/push-subfolder",
        json={"subfolder_path": "TV Shows/Paradise", "target_instance_id": "target", "target_path": "/data/tv"},
        headers=HDR,
    )
    device_count_after_first = len(fakes["http://target"].devices)
    fakes["_events"].clear()

    r = client.post(
        "/api/folders/source/tvshows/push-subfolder",
        json={"subfolder_path": "TV Shows/Breaking Bad", "target_instance_id": "target"},
        headers=HDR,
    )
    assert r.status_code == 200, r.text

    # Reusing an existing share must not touch pause state at all — only
    # the first push for a (folder, target) pair pauses/resumes the folder.
    assert fakes["_events"] == [("set_ignores", fakes["http://target"].ignores["tvshows"])]

    # No re-registration — same device count as after the first push.
    assert len(fakes["http://target"].devices) == device_count_after_first
    assert fakes["http://target"].ignores["tvshows"] == [
        "!/TV Shows/Paradise", "!/TV Shows/Paradise/**",
        "!/TV Shows/Breaking Bad", "!/TV Shows/Breaking Bad/**",
        "/*",
    ]
    assert len(main.store.load_subfolder_pushes()) == 2


def test_subfolder_transfer_progress_is_scoped_to_the_pushed_subfolder(client, fakes):
    client.post(
        "/api/folders/source/tvshows/push-subfolder",
        json={"subfolder_path": "TV Shows/Paradise", "target_instance_id": "target", "target_path": "/data/tv"},
        headers=HDR,
    )
    # Total size for "TV Shows/Paradise" was snapshotted as 1000 bytes at push time
    # (from the fake browse response). Simulate 400 bytes still needed on target,
    # plus an unrelated file elsewhere in the folder that must NOT be counted.
    fakes["http://target"].need["tvshows"] = {
        "progress": [{"name": "TV Shows/Paradise/s01e01.mkv", "size": 400}],
        "queued": [],
        "rest": [{"name": "TV Shows/Breaking Bad/s01e01.mkv", "size": 9999}],
    }
    asyncio.run(main._refresh_all())

    r = client.get("/api/subfolder-transfers", headers=HDR)
    assert r.status_code == 200
    transfers = r.json()
    assert len(transfers) == 1
    t = transfers[0]
    assert t["sourceInstanceName"] == "Source"
    assert t["targetInstanceName"] == "Target"
    assert t["subfolderPath"] == "TV Shows/Paradise"
    assert t["targetPath"] == "/data/tv"
    assert t["totalBytes"] == 1000
    assert t["needBytes"] == 400
    assert t["percent"] == 60.0
    assert t["state"] == "syncing"


def test_subfolder_transfer_reports_complete_when_nothing_needed(client, fakes):
    client.post(
        "/api/folders/source/tvshows/push-subfolder",
        json={"subfolder_path": "TV Shows/Paradise", "target_instance_id": "target", "target_path": "/data/tv"},
        headers=HDR,
    )
    fakes["http://target"].need["tvshows"] = {"progress": [], "queued": [], "rest": []}
    asyncio.run(main._refresh_all())

    r = client.get("/api/subfolder-transfers", headers=HDR)
    t = r.json()[0]
    assert t["needBytes"] == 0
    assert t["percent"] == 100
    assert t["state"] == "complete"


def test_unpush_subfolder_removes_pattern_and_mapping(client, fakes):
    client.post(
        "/api/folders/source/tvshows/push-subfolder",
        json={"subfolder_path": "TV Shows/Paradise", "target_instance_id": "target", "target_path": "/data/tv"},
        headers=HDR,
    )

    r = client.request(
        "DELETE",
        "/api/folders/source/tvshows/push-subfolder",
        params={"subfolder_path": "TV Shows/Paradise", "target_instance_id": "target"},
        headers=HDR,
    )
    assert r.status_code == 204, r.text
    assert fakes["http://target"].ignores["tvshows"] == ["/*"]
    assert main.store.load_subfolder_pushes() == []
