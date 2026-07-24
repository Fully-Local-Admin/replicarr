import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

import store


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Point store at a throwaway instances.json for every test."""
    monkeypatch.setattr(store, "DATA_PATH", tmp_path)
    monkeypatch.setattr(store, "INSTANCES_FILE", tmp_path / "instances.json")
    yield


def test_slug_lowercases_and_strips_punctuation():
    assert store._slug("My Syncthing!") == "my-syncthing"
    assert store._slug("  leading/trailing--dashes  ") == "leading-trailing-dashes"


def test_add_instance_persists_and_round_trips():
    inst = store.add_instance("Home NAS", "http://nas:8384/", "secret")
    assert inst["id"] == "home-nas"
    assert inst["url"] == "http://nas:8384"  # trailing slash stripped
    assert inst["source"] == "ui"
    assert store.load_instances() == [inst]


def test_add_instance_rejects_duplicate_slug():
    store.add_instance("Home NAS", "http://a", "k1")
    with pytest.raises(ValueError):
        store.add_instance("home nas", "http://b", "k2")


def test_update_instance_replaces_fields():
    store.add_instance("Home NAS", "http://a", "k1")
    updated = store.update_instance("home-nas", "Home NAS", "http://b/", "k2")
    assert updated == {
        "id": "home-nas", "name": "Home NAS", "url": "http://b", "api_key": "k2", "source": "ui",
    }


def test_update_instance_missing_raises_keyerror():
    with pytest.raises(KeyError):
        store.update_instance("nope", "x", "http://a", "k")


def test_update_instance_config_managed_raises_permissionerror():
    store.merge_config_instances([{"name": "NAS", "url": "http://a", "api_key": "k"}])
    with pytest.raises(PermissionError):
        store.update_instance("nas", "NAS", "http://b", "k2")


def test_delete_instance_removes_ui_instance():
    store.add_instance("Home NAS", "http://a", "k1")
    store.delete_instance("home-nas")
    assert store.load_instances() == []


def test_delete_instance_config_managed_raises_permissionerror():
    store.merge_config_instances([{"name": "NAS", "url": "http://a", "api_key": "k"}])
    with pytest.raises(PermissionError):
        store.delete_instance("nas")


def test_delete_instance_missing_raises_keyerror():
    with pytest.raises(KeyError):
        store.delete_instance("nope")


def test_merge_config_instances_adds_new_entries():
    merged = store.merge_config_instances([{"name": "NAS", "url": "http://a/", "api_key": "k"}])
    assert merged == [{"id": "nas", "name": "NAS", "url": "http://a", "api_key": "k", "source": "config"}]


def test_merge_config_instances_overwrites_existing_config_entry():
    store.merge_config_instances([{"name": "NAS", "url": "http://old", "api_key": "k1"}])
    merged = store.merge_config_instances([{"name": "NAS", "url": "http://new", "api_key": "k2"}])
    assert merged == [{"id": "nas", "name": "NAS", "url": "http://new", "api_key": "k2", "source": "config"}]


def test_merge_config_instances_never_touches_ui_entries():
    store.add_instance("NAS", "http://ui-set", "ui-key")
    merged = store.merge_config_instances([{"name": "NAS", "url": "http://config-set", "api_key": "config-key"}])
    assert merged == [{"id": "nas", "name": "NAS", "url": "http://ui-set", "api_key": "ui-key", "source": "ui"}]


def test_load_instances_backs_up_corrupt_file_instead_of_losing_it():
    store.INSTANCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    store.INSTANCES_FILE.write_text("{not valid json")

    result = store.load_instances()

    assert result == []
    backups = list(store.DATA_PATH.glob("instances.json.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "{not valid json"
