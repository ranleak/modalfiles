# ModalFiles

A self-hosted, password-protected WebDAV file share backed by a Modal Volume.
Windows File Explorer can map it as a network drive via the Windows WebClient
service. The server is a FastAPI ASGI application running WsgiDAV on an Ubuntu
24.04 Modal image. No Modal API key is needed by the Windows client; the person
deploying the service needs their own Modal account and CLI credentials.

## Deploy

1. Install Python 3.10+ and [uv](https://docs.astral.sh/uv/) (for example with
   `curl -LsSf https://astral.sh/uv/install.sh | sh` or `pipx install uv`),
   then run `uv sync` in the repository. This creates `.venv` and installs the
   locked dependencies from `uv.lock` (including the `dev` group for tests).
2. Run `uv run modal setup` and sign into your own Modal account/workspace.
3. Create a Modal Secret named `modalfiles-auth` containing **both**
   `MODALFILES_USER` and `MODALFILES_PASSWORD`. Use a unique username and a
   long, random password. Create it in the Modal dashboard, or securely provide
   a temporary dotenv file with `uv run modal secret create modalfiles-auth --from-dotenv /path/to/private.env`.
   Remove the local secret file afterwards. Do not commit it or put passwords
   in shell history. The secret must be in the same Modal environment as the app.
4. Run `uv run modal deploy --strategy recreate modal_app.py`. Copy the HTTPS URL
   printed for the `web` endpoint (not the Modal dashboard URL). Recreate is
   important: rolling deployments can temporarily run two writers.

The named `modalfiles-data` Volume is created if needed and persists across
container restarts and redeployments. Modal usage and storage charges apply.
Back up the volume independently; this is not a versioned backup system.

## Windows GUI

The Windows GUI is a local desktop dashboard for ModalFiles. It provides a
better file-system viewing experience, live server metrics, total data
transferred, streaming server logs, and cloud ZIP progress. It can also map or
disconnect the WebDAV drive on Windows and trigger cloud 7-Zip compression for
folders that do not already have a corresponding ZIP.

### Build on Linux

Install the Windows build tools on Linux:

```bash
sudo apt install gcc-mingw-w64-x86-64 nsis
```

Build the launcher and NSIS installer:

```bash
./scripts/build-windows.sh
```

This produces:

* `dist/ModalFilesGUI.exe` - the small Windows launcher.
* `dist/ModalFiles-Setup-<version>.exe` - the NSIS installer for the GUI.
* `dist/ModalFiles-Setup.exe` - the legacy CLI drive-mapping installer.

### Install on Windows 11

1. Copy `dist/ModalFiles-Setup-<version>.exe` to the Windows machine and run it.
2. The installer is per-user (no admin required) and installs to
   `%LOCALAPPDATA%\ModalFiles`.
3. Launch **ModalFiles** from the Start Menu or Desktop shortcut.
4. In **Settings**, enter the Modal HTTPS base URL, WebDAV username, and
   password, then click **Test connection**.
5. Optionally click **Map / remap drive** to create the persistent WebDAV drive.

Requirements on the Windows machine:

* Windows 11 (optimized for Windows 11; WebView/Edge app mode is used for the UI).
* Python 3.10+ installed and available as `py -3`, `pythonw.exe`, or on PATH.
* Windows **WebClient** service running for mapped WebDAV drives.

The GUI runs a local Python server on `127.0.0.1` and opens in Microsoft Edge
app mode when available. The mapped WebDAV drive remains on the system; the GUI
manages and visualizes it but does not replace it.

### Legacy CLI drive mapper

The original console drive-mapping installer is still built as
`dist/ModalFiles-Setup.exe`. It asks for the base HTTPS endpoint URL, a drive
letter, and the WebDAV username; Windows `net use` prompts for the password. To
disconnect, run `net use Z: /delete` with the actual drive letter.

Windows' **WebClient** service must be installed and running. Windows' WebDAV
client may impose its own file-size, timeout, and Basic-authentication policies;
contact your administrator if a mapping fails. The service only supports HTTPS
URLs, and the connection uses HTTP Basic authentication over TLS. Use a strong
password: the endpoint is public on the internet and anyone with the credentials
can read and delete everything. Rotate the Modal secret and redeploy if exposed.
Do not share the URL/credentials with untrusted users. Windows may cache the
credentials according to its normal credential policies.

## ZIP automation

After a successful WebDAV PUT or MOVE to a `.zip` name, the service tries to
extract it into a sibling folder named after the ZIP (e.g. `photos.zip` →
`photos/`). Extraction runs on the server's allocated CPU, before the mutation
is committed and its response is returned. The ZIP remains in place. Existing
folders are never overwritten. Invalid, encrypted, unsafe, or oversized ZIPs
are kept unextracted; a failed extraction is logged but does not fail the file
upload. The limits are 10,000 entries and 10 GiB total uncompressed data. These
limits are safety checks, not a guarantee against resource exhaustion. Large
archives can take longer than a Windows client or Modal web request timeout.

### Cloud ZIP from GUI/API

The GUI can compress a remote folder into a ZIP in the cloud. The server prefers
7-Zip (`7zz`/`7z`/`7za`) when available and falls back to Python's `zipfile`
module. The ZIP is written next to the source folder (e.g. `photos/` →
`photos.zip`) and committed to the Modal Volume. Progress is exposed through the
authenticated API and streamed in the GUI.

Authenticated API endpoints (same Basic auth as WebDAV):

* `GET /__modalfiles__/api/status` - server status and transfer counters.
* `GET /__modalfiles__/api/metrics` - SSE metrics stream.
* `GET /__modalfiles__/api/logs` - SSE log stream.
* `GET /__modalfiles__/api/files?path=/` - file listing with ZIP indicators.
* `POST /__modalfiles__/api/zip` - start a cloud ZIP job for a folder path.
* `GET /__modalfiles__/api/zip/jobs` - list ZIP jobs and progress.

## Operational limitations

* This is a single-writer design, not a distributed filesystem. One Modal
  container handles requests serially, reloads the Volume before each request,
  and commits successful changes before acknowledging them. Do not mount the
  same Volume in other writers or scale this service beyond one container.
  Long transfers and extraction block other clients. Volume commits, cold starts,
  Modal web request limits, and Windows timeouts affect large files and latency.
* WebDAV locks are in-memory and disappear when the container restarts; do not
  rely on them for durable coordination. Volume writes from elsewhere are not
  coordinated. A failed commit or interrupted operation can leave changes whose
  eventual persistence is uncertain, including due to Volume background commits.
* This is WebDAV, not SMB: Explorer compatibility depends on Windows version,
  local policies and network environment, and some filesystem features are not
  available. The setup executable and Explorer mapping should be verified on
  the target Windows installation.

## Local GUI development

Run the GUI locally (serves on `127.0.0.1:8765` by default):

```bash
uv run python -m modalfiles.gui.server
# or, after installing the package:
modalfiles-gui
```

The local GUI proxies authenticated requests to your deployed ModalFiles server.
Configure the server URL and credentials in the Settings view.

## Local tests

`uv sync` then `uv run pytest -q`.
Tests exercise WebDAV authentication, file operations, volume callbacks, ZIP
safety, and GUI API endpoints locally; they do not deploy to Modal or exercise
Windows Explorer.

## Dependency management

Dependencies are declared in `pyproject.toml` and locked in `uv.lock`; both are
committed. Use `uv add <package>` / `uv remove <package>` to change them, then
commit the updated lockfile. `uv sync` reproduces the environment exactly
(`uv sync --frozen` in automation). The Modal image builds its server
dependencies with `uv pip install` via Modal's `uv_pip_install`. The test-only
dependencies (`pytest`, `httpx`) live in the `dev` dependency group, which `uv
sync` installs by default; use `uv sync --no-dev` to skip them.
