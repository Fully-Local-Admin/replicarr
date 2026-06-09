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
land.

## Security notes

- API keys are never sent to the browser. All Syncthing REST calls happen
  server-side inside the add-on container.
- `/data/instances.json` is written with mode `0600`.
- The add-on uses Ingress only — no ports are exposed on the host.
