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

### Pushing a subfolder to another instance

Replicarr pushes **subfolders**, never a whole main folder as one unit.
Open a folder's detail panel (click it on the Overview tab) and browse into
its **Subfolders** section, then click **Push →** on the specific subfolder
you want on another instance.

Under the hood, Syncthing folders always sync as a whole, so Replicarr
can't just register the subfolder as its own independent folder (Syncthing
won't allow a folder nested inside another folder's path anyway). Instead:

- The **first** subfolder you push from a given main folder to a given
  target shares that whole main folder with the target (device
  registration, folder creation — the same steps as before), but configures
  the target to selectively sync **only** the subfolder you pushed, not the
  rest of the folder.
- **Every subsequent** subfolder pushed from that same main folder to the
  same target just widens what's selectively synced there — no new device
  registration, no path to re-enter.

If a step fails partway through the first-time share, Replicarr
automatically rolls back what already succeeded and reports the outcome;
if a rollback step itself fails, you may need to finish cleaning up
manually in Syncthing's own UI. The Transfers tab shows every subfolder
push — in progress and completed — with its source, target, size, and
approximate speed.

### Removing folders and devices

From the folder detail panel or the folder table, **Remove** removes a
folder from Syncthing's configuration (files already on disk are left
alone). From an instance's Devices tab, **Unshare** removes that device
from every folder it's shared on and disconnects it.

## Accessing Replicarr outside Home Assistant

Replicarr is reachable via Home Assistant Ingress by default, which relies
on your existing HA login — no extra setup needed. It also publishes port
`8099/tcp` to the host so it can be reached directly without going through
HA at all. You can change the host-side port under the add-on's
**Configuration → Network** tab, or set it to nothing to disable direct
access entirely.

Because direct access bypasses HA's login, it requires its own credentials:
set **Direct access username** and **Direct access password** in the
add-on's Configuration tab. Until both are set, direct requests are
refused outright (`403`). Once set, opening Replicarr through an HTTPS URL
shows a standard sign-in form that iOS and password managers such as
1Password can autofill. The add-on's port speaks plain HTTP itself, so
secure direct access requires an HTTPS reverse proxy or a secure publishing
service such as Tailscale Serve in front of port `8099`; Replicarr refuses
to create a direct session over plain HTTP.

Direct sessions use an `HttpOnly`, `Secure`, `SameSite=Strict` cookie,
expire after 7 days, and are cleared whenever the add-on restarts. Five
failed sign-in attempts within five minutes lock further attempts from that
client for 15 minutes. Use the sign-out button in Replicarr's top bar to end
a session immediately. Ingress access is never affected — it remains
authenticated by Home Assistant.

## Security notes

- API keys are never sent to the browser. All Syncthing REST calls happen
  server-side inside the add-on container.
- `/data/instances.json` is written with mode `0600`; if it's ever found
  corrupted, Replicarr backs it up alongside itself (`instances.json.bak.*`)
  rather than silently discarding it.
- Requests reaching Replicarr outside of Ingress are rejected unless direct
  credentials are configured and a valid HTTPS login session is present, as
  described above.
