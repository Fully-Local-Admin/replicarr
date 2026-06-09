# Replicarr

A Home Assistant add-on that provides a unified web dashboard for managing
multiple [Syncthing](https://syncthing.net/) instances from one place.

## Features

- View all your Syncthing instances, folders, and devices in one dashboard
- Live sync status with progress, speed, and ETA
- Add Syncthing instances from the UI or pre-configure them in add-on options
- Push (share) a folder from one Syncthing instance to another with a guided flow
- Pause and resume folder sync and device connections
- Runs behind Home Assistant Ingress — no exposed ports

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

Each instance under `instances` needs `name`, `url`, and `api_key`.
Additional instances can be added at runtime through the dashboard.
