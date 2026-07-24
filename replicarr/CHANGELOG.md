# Changelog

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
