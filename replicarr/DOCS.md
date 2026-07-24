# Replicarr Documentation

## Getting started

After installing and starting the add-on, open **Replicarr** from the Home
Assistant sidebar. On first run the dashboard is empty.

### Adding a Syncthing instance

1. Click **Add Instance** in the dashboard.
2. Enter a name, the base URL of the Syncthing REST API
   (e.g. `http://a0d7b954-syncthing:8384`), and the API key
   (found under Syncthing → Settings → GUI → API Key).
3. Click **Test** to verify connectivity, then **Save**.

Instances added here are stored in `/data/instances.json` and labelled
**UI** in the instance list. They persist across restarts.

### Pre-configuring instances via add-on options

You can seed instances through the add-on Configuration tab. These appear in
the dashboard with a **"from add-on config"** badge and cannot be edited or
deleted from the UI (they return on restart).

```yaml
instances:
  - name: Local Syncthing
    url: http://a0d7b954-syncthing:8384
    api_key: your-api-key-here
  - name: Remote NAS
    url: https://nas.example.com:8384
    api_key: another-api-key
```

### Pushing a folder to another instance

Use the **Push** flow (Phase 4) to share a folder from one Syncthing
instance to another. Replicarr handles the 5-step device registration and
folder-sharing flow — you just supply the target path where the data should
land. If a step fails partway through, Replicarr automatically rolls back
the steps that already succeeded and reports the outcome; if a rollback
step itself fails, you may need to finish cleaning up manually in
Syncthing's own UI.

### Removing folders and devices

From the folder detail panel or the folder table, **Remove** removes a
folder from Syncthing's configuration (files already on disk are left
alone). From an instance's Devices tab, **Unshare** removes that device
from every folder it's shared on and disconnects it.

## Accessing Replicarr outside Home Assistant

Replicarr is reachable via Home Assistant Ingress by default, which relies
on your existing HA login — no extra setup needed. It also publishes port
`8099/tcp` to the host so it can be reached directly (e.g.
`http://<home-assistant-ip>:8099`) without going through HA at all. You can
change the host-side port under the add-on's **Configuration → Network**
tab, or set it to nothing to disable direct access entirely.

Because direct access bypasses HA's login, it requires its own credentials:
set **Direct access username** and **Direct access password** in the
add-on's Configuration tab. Until both are set, direct requests are
refused outright (`403`); once set, your browser will prompt for them
(HTTP Basic Auth) the first time you visit the port directly. Ingress
access is never affected either way — it's already authenticated by HA.

## Security notes

- API keys are never sent to the browser. All Syncthing REST calls happen
  server-side inside the add-on container.
- `/data/instances.json` is written with mode `0600`; if it's ever found
  corrupted, Replicarr backs it up alongside itself (`instances.json.bak.*`)
  rather than silently discarding it.
- Requests reaching Replicarr outside of Ingress are rejected unless Basic
  Auth is configured, as described above — see **Accessing Replicarr
  outside Home Assistant**.
