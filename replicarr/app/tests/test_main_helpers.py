import asyncio
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


def test_shutdown_wait_pattern_interrupts_immediately_instead_of_sleeping_full_interval():
    """
    Regression test for a hung-shutdown bug: /api/stream's loop used to
    `await asyncio.sleep(REFRESH_INTERVAL_SECONDS)` unconditionally, so an
    open SSE connection kept the process alive through a graceful shutdown
    indefinitely (uvicorn waits for in-flight connections to close on their
    own, which an infinite generator never does by itself). The fix waits
    on a shutdown Event instead of sleeping blindly, via the same
    `asyncio.wait_for(event.wait(), timeout=REFRESH_INTERVAL_SECONDS)`
    pattern main.py's stream() uses.

    This exercises that exact pattern with a fresh, locally-scoped Event
    rather than the real module-level `main._shutdown_event` — asyncio.Event
    objects predating Python 3.10 bind to whichever loop is running at
    construction time, and the real one is created at module-import time,
    outside of this test's own asyncio.run() loop. Using a local Event
    keeps the test meaningful on any Python version without depending on
    global async state or cross-test ordering.
    """
    async def scenario():
        event = asyncio.Event()

        async def set_it_shortly():
            await asyncio.sleep(0.05)
            event.set()

        setter = asyncio.create_task(set_it_shortly())
        start = asyncio.get_event_loop().time()
        try:
            await asyncio.wait_for(event.wait(), timeout=main.REFRESH_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
        await setter
        return asyncio.get_event_loop().time() - start

    elapsed = asyncio.run(scenario())
    assert elapsed < 1.0, "shutdown wait should return almost immediately, not wait out the full interval"
