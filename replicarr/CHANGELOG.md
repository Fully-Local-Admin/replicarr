# Changelog

## [0.3.5] - 2026-07-24

### Fixed
- Reverted `apparmor: true` back to `false`. The 0.3.4 fix targets the
  actual shutdown mechanism (a bounded s6 stop deadline plus properly
  reaping the sampler task); AppArmor was never confirmed to be a
  contributing cause of the restart hangs, and re-enabling it carries its
  own separate risk of the `/run/s6/basedir` permission-denied boot loop
  seen in 0.2.0. Turned off out of caution while 0.3.4's fix is verified
  on its own.

## [0.3.4] - 2026-07-24

### Fixed
- Added a 7-second s6 service stop deadline so an HTTP request stuck in
  Uvicorn's "Waiting for connections to close" phase cannot outlive Home
  Assistant's container shutdown window. Uvicorn still gets its existing
  5-second graceful-shutdown period first; if it fails to exit, s6 now
  terminates the service while the supervisor is still in control, avoiding
  the stale supervision state that caused the next start to loop on
  `s6-svscan: fatal: another instance ... already running`.
- The background Syncthing sampler is now both cancelled and awaited during a
  normal FastAPI lifespan shutdown, ensuring its in-flight task is fully
  reaped before the application exits.

## [0.3.3] - 2026-07-24

### Removed
- Removed the `/api/stream` SSE endpoint entirely. It caused the exact same
  shutdown hang / `s6-svscan: fatal: another instance ... already running`
  failure on **three** separate live tests, across two different attempted
  fixes (0.3.1's shutdown-event wait, 0.3.2's self-bounded 8s lifetime) that
  each checked out fine in local/simulated testing but failed against the
  real add-on. Rather than attempt a fourth theory about uvicorn's shutdown
  internals without a way to verify it live, the long-lived connection is
  gone. The frontend polls `/api/status`, `/api/transfers`, and
  `/api/subfolder-transfers` on a 3-second timer instead — the same
  approach used before 0.3.0, which never had this failure mode, since a
  plain request/response cycle completes in milliseconds and leaves
  nothing for a graceful shutdown to wait on.

## [0.3.2] - 2026-07-24

### Fixed
- The 0.3.1 fix for the SSE hung-shutdown bug didn't actually work —
  confirmed live, same symptom recurred (~10s hang, then
  `s6-svscan: fatal: another instance ... already running` on the next
  start). Root cause: uvicorn only delivers the ASGI lifespan "shutdown"
  event *after* all in-flight connections close, but 0.3.1's fix set a
  shutdown flag from inside that same lifespan handler for the stream loop
  to watch for — a deadlock, since neither side can move first.
  `/api/stream` now bounds its own connection lifetime (8 seconds) instead
  of waiting to be told to stop. EventSource reconnects automatically when
  a stream ends, and once shutdown begins uvicorn stops accepting new
  connections, so the cycled-out connection is simply never replaced —
  this doesn't depend on uvicorn's shutdown-ordering internals at all.
  Verified live (not just unit-tested) that a stream connection now ends
  on its own within 8 seconds rather than hanging indefinitely.

## [0.3.1] - 2026-07-24

### Fixed
- Fixed a hung-shutdown bug introduced by 0.3.0's `/api/stream` SSE
  endpoint: an open browser tab keeps that connection alive forever, and
  uvicorn's graceful shutdown waited for it to close on its own, which it
  never did on its own. Confirmed live: the log showed "Waiting for
  connections to close" followed by continued activity, and the next start
  failed with `s6-svscan: fatal: another instance ... already running` —
  consistent with the previous process being force-killed mid-shutdown and
  leaving s6's supervision state behind.
  The stream loop now waits on a shutdown event (with the same interval as
  before) instead of sleeping unconditionally, so it exits within
  milliseconds of a shutdown starting instead of hanging indefinitely.
  Also added `--timeout-graceful-shutdown 5` to the uvicorn invocation as a
  safety net against any other long-lived connection having the same
  problem in the future.

## [0.3.0] - 2026-07-24

### Added
- Push individual subfolders of an already-synced main folder to another
  instance, instead of only pushing the whole folder. This works via
  Syncthing's selective sync (the main folder is shared once, then each
  pushed subfolder widens what's selectively synced on the target) rather
  than creating a second folder nested inside the first, which Syncthing
  doesn't allow. The main folder itself can no longer be pushed directly —
  only its subfolders, browsable from the folder detail panel. The target
  folder is created paused on the first push for a given (folder, target)
  pair and only resumed after selective-sync ignore patterns are safely in
  place, so it can't briefly sync everything in the window before those
  patterns are applied.
  **Not independently verified against a live Syncthing instance**: the
  `/rest/db/browse` response shape and the exact ignore-pattern ordering
  Syncthing expects for selective sync are implemented from documentation/
  memory, not tested live — if subfolder browsing or selective sync looks
  wrong in practice, `main.py`'s `_normalize_browse_entries` and
  `_selective_sync_ignores` are the first places to check.
- Transfers tab now lists individual subfolder pushes (in progress and
  completed), each showing source → target instance, size, live progress,
  and approximate speed.
- Sidebar can be collapsed via a new toggle next to the dark-mode button.
- Settings modal (sidebar → Settings) with real, working options: theme
  (light/dark/follow system), default tab on load, and default sidebar
  state — all saved in the browser.

### Removed
- The three sidebar filter buttons (Online/Offline/Syncing). The problems
  banner's "instances offline" link now jumps to the Instances tab instead
  of filtering the Overview grid.
- Direct "Push whole folder" UI (table row and folder detail panel) — see
  "Added" above for why, and the new subfolder-based flow.

## [0.2.2] - 2026-07-24

### Fixed
- Re-enabled `apparmor: true` with the actual missing rule identified from
  the 0.2.0 crash: `/run/s6/basedir/** rix,`. s6-overlay re-execs
  `/run/s6/basedir/bin/init` on every restart; the profile only granted
  `rw` there, not execute, which is what caused the "Permission denied"
  boot loop. **Please verify this survives an actual add-on *restart***
  (not just a fresh install) — that's the exact path that broke last
  time. If it fails again, set `apparmor: false` in `config.yaml` and
  bump the version to recover, same as before.

## [0.2.1] - 2026-07-24

### Fixed
- Reverted `apparmor: true` from 0.2.0 — re-enabling it broke add-on
  *restarts*: s6-overlay's stage0 re-execs into `/run/s6/basedir/bin/init`
  on restart, which needs execute permission that the `/run/** rw` rule
  didn't grant, so the container looped on "Permission denied" until
  disabled again. Back to `apparmor: false`, matching the previously
  working state. See the comment in `apparmor.txt` for what a future
  attempt needs to get right, and test through a restart, not just a
  first boot.

## [0.2.0] - 2026-07-24

### Added
- Optional direct (non-Ingress) access via a published port, gated behind
  HTTP Basic Auth (`basic_auth_username`/`basic_auth_password`); Ingress
  access is unaffected and never requires it.
- Remove a folder or unshare a device directly from the dashboard.
- Real Server-Sent Events feed (`/api/stream`) — the frontend no longer
  polls on a timer; this is the SSE work the 0.1.0 entry below described
  but that hadn't actually landed in the code until now.
- Automatic best-effort rollback when the Push flow fails partway through,
  with the rollback outcome reported alongside the step list.
- A problems banner on the Overview tab surfacing offline instances, folder
  errors, and disconnected devices.
- Unit tests for the instance store's merge/CRUD logic (`app/tests/`).

### Changed
- `/api/status` and `/api/transfers` are now served from a single shared
  cache refreshed every 2s, instead of each browser tab independently
  re-fetching every configured Syncthing instance on every request.
- Re-enabled and tightened the AppArmor profile (previously disabled after
  earlier boot failures).

### Fixed
- Removed the wildcard CORS policy (`allow_origins: "*"`), which had no
  legitimate use case and widened the attack surface once a direct port
  was exposed.
- Fixed an XSS gap where folder/device IDs from a remote Syncthing peer
  could break out of inline `onclick` handlers; untrusted values are now
  passed via `data-*` attributes instead of interpolated into inline JS.
- Corrupt `instances.json` is now backed up instead of being silently
  discarded on the next read.

## [0.1.0] - 2026-06-09

### Added
- Phase 0: App skeleton with Ingress support, health endpoint, and static frontend
- Phase 1: Instance management (add/edit/delete, config-lock, connectivity test)
- Phase 2: Read view — folders, devices, sync status, sizes across all instances
- Phase 2.5: Transfer metrics — progress bars, speed, ETA, Pause/Resume controls
- Phase 3: Add folders from the dashboard
- Phase 4: Push flow — guided 5-step folder sharing between instances
- Phase 5: SSE live updates, multi-arch Docker images via GitHub Actions
