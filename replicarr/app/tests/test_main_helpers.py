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


def test_stream_max_lifetime_is_comfortably_under_a_typical_kill_grace_period():
    """
    Regression guard for a hung-shutdown bug: an open /api/stream connection
    never completes on its own, and uvicorn only delivers the ASGI lifespan
    "shutdown" event *after* in-flight connections close — so a signal set
    from that lifespan handler can never reach the stream loop; neither side
    moves first. Confirmed live twice: the process hung until Docker's
    default ~10s SIGKILL grace period force-killed it, corrupting s6's
    supervision state for the next boot (see CHANGELOG 0.3.1 and 0.3.2).

    The fix bounds the stream's own lifetime instead of waiting to be told
    to stop — EventSource reconnects automatically, and once shutdown begins
    uvicorn refuses new connections, so nothing replaces the one that ends.
    This just guards the constant itself: it must stay well under a typical
    container stop grace period, or the whole point of self-bounding is lost.
    """
    assert 0 < main.STREAM_MAX_LIFETIME_SECONDS <= 8
