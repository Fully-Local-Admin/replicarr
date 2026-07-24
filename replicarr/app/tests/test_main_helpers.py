import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main


def test_normalize_browse_entries_leaf_names():
    raw = [
        {"name": "Paradise", "type": "FILE_INFO_TYPE_DIRECTORY"},
        {"name": "notes.txt", "type": "FILE_INFO_TYPE_FILE"},
    ]
    entries = main._normalize_browse_entries(raw, "TV Shows")
    assert entries == [
        {"name": "Paradise", "path": "TV Shows/Paradise", "type": "dir"},
        {"name": "notes.txt", "path": "TV Shows/notes.txt", "type": "file"},
    ]


def test_normalize_browse_entries_full_paths_already():
    raw = [{"name": "TV Shows/Paradise", "type": "FILE_INFO_TYPE_DIRECTORY"}]
    entries = main._normalize_browse_entries(raw, "TV Shows")
    assert entries == [{"name": "Paradise", "path": "TV Shows/Paradise", "type": "dir"}]


def test_normalize_browse_entries_no_prefix():
    raw = [{"name": "TV Shows", "type": "FILE_INFO_TYPE_DIRECTORY"}]
    entries = main._normalize_browse_entries(raw, "")
    assert entries == [{"name": "TV Shows", "path": "TV Shows", "type": "dir"}]


def test_sum_browse_size_flat_files():
    raw = [{"name": "a", "size": 100}, {"name": "b", "size": 250}]
    assert main._sum_browse_size(raw) == 350


def test_sum_browse_size_nested_children():
    raw = [
        {"name": "a", "size": 100},
        {"name": "subdir", "children": [
            {"name": "c", "size": 50},
            {"name": "d", "size": 25},
        ]},
    ]
    assert main._sum_browse_size(raw) == 175


def test_selective_sync_ignores_first_push():
    result = main._selective_sync_ignores([], "TV Shows/Paradise")
    assert result == ["!/TV Shows/Paradise", "!/TV Shows/Paradise/**", "/*"]


def test_selective_sync_ignores_second_push_preserves_first():
    after_first = main._selective_sync_ignores([], "TV Shows/Paradise")
    after_second = main._selective_sync_ignores(after_first, "TV Shows/Breaking Bad")
    assert after_second == [
        "!/TV Shows/Paradise", "!/TV Shows/Paradise/**",
        "!/TV Shows/Breaking Bad", "!/TV Shows/Breaking Bad/**",
        "/*",
    ]


def test_selective_sync_ignores_idempotent():
    once = main._selective_sync_ignores([], "TV Shows/Paradise")
    twice = main._selective_sync_ignores(once, "TV Shows/Paradise")
    assert once == twice


def test_no_long_lived_streaming_endpoint_exists():
    """
    Regression guard: /api/stream (an SSE feed) repeatedly caused the add-on
    to hang on shutdown/restart, confirmed live three times across three
    different attempted fixes (see CHANGELOG 0.3.0-0.3.3) — a long-lived
    connection is something uvicorn's graceful shutdown can end up waiting
    on indefinitely, in ways that were not reproducible in local testing.
    It was removed in favor of plain polling. This just guards against it
    (or something like it) being reintroduced without a way to actually
    verify a fix against a real restart, not just a simulated one.
    """
    stream_routes = [r for r in main.app.routes if getattr(r, "path", "") == "/api/stream"]
    assert not stream_routes, "a long-lived /api/stream endpoint was reintroduced"
