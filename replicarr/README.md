# Replicarr

A Home Assistant add-on that provides a unified web dashboard for managing
multiple [Syncthing](https://syncthing.net/) instances from one place.

## Features

- View all your Syncthing instances, folders, and devices in one dashboard
- Live sync status with progress, speed, and ETA, refreshed every few seconds
- Add Syncthing instances from the UI or pre-configure them in add-on options
- Push individual subfolders of a main folder to another instance via Syncthing's selective sync, with automatic rollback if a step fails and a Transfers view of every push in progress or completed
- Pause, resume, and remove folders; pause, resume, and unshare devices
- A problems banner surfaces offline instances, folder errors, and disconnected devices at a glance
- Collapsible sidebar and a Settings panel for theme, default tab, and sidebar preferences
- Runs behind Home Assistant Ingress, with an optional directly-exposed port (protected by its own Basic Auth) for access from outside Home Assistant

## Installation

1. Add this repository to your Home Assistant Add-on store.
2. Install **Replicarr**.
3. Configure any instances you want pre-seeded under **Configuration**.
4. Start the add-on and open the UI from the sidebar.

## Configuration

| Option | Description |
|--------|-------------|
| `log_level` | Backend log verbosity (`info` recommended) |
| `instances` | Optional list of Syncthing instances to pre-configure |
| `basic_auth_username` / `basic_auth_password` | Credentials required to reach Replicarr via its directly-exposed port (not Ingress). Leave both blank to keep direct access disabled. |

Each instance under `instances` needs `name`, `url`, and `api_key`.
Additional instances can be added at runtime through the dashboard.
