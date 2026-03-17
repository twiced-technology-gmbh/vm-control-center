# VM Control Center API

This document describes the API served by the VM Control Center web app.

## Base URL

- `http://127.0.0.1:8000`

## Authentication

All HTTP API routes under `/api/*` require an API token via header:

- `X-Local-Token: <token>`

The token is generated on first run and stored at:

- `~/.vm-control-center/token`

The WebSocket endpoint requires the token as a query parameter:

- `ws://127.0.0.1:8000/ws/tasks/<task_id>?token=<token>`

## Conventions

- All responses are JSON unless otherwise noted.
- Errors are returned as JSON with a `detail` field (FastAPI default).

## Data models (high level)

### VM

Returned by `/api/vms` and related endpoints.

Fields (current):

- `name`: string
- `status`: `running | stopped | unknown`
- `ip_address`: string or null
- `source`: string or null
- `os`: string or null
- `cpu`: number or null
- `memory`: string or null
- `disk_size`: number or string or null
- `display`: string or null

### VM config

Returned by `/api/vms/{vm_name}/config`.

Fields:

- `name`: string
- `cpu`: number or null
- `memory`: string or null (normalized; e.g. `8G`)
- `disk_size`: string or null (normalized; e.g. `50G`)
- `raw`: object (raw JSON from `tart get --format json`)

### Task

Returned by start/stop/delete/pull/clone/create endpoints and `/api/tasks/{task_id}`.

Fields:

- `id`: string
- `action`: string
- `status`: `pending | running | completed | failed`
- `command`: array of strings or null
- `exit_code`: number or null
- `result`: object or null
- `error`: string or null
- `stderr`: string or null
- `created_at`: unix timestamp (float)
- `updated_at`: unix timestamp (float)
- `logs`: array of strings

### VM Image

Returned by `/api/vms/available-images`.

Fields:

- `name`: string (e.g., `macos-ventura-base`)
- `url`: string (OCI URL, e.g., `ghcr.io/cirruslabs/macos-ventura-base:latest`)
- `description`: string or null
- `tags`: array of strings (available tags for the image)
- `updated_at`: string or null (ISO 8601 timestamp)

### VM Images Summary

Returned by `/api/vms/categorized`.

Fields:

- `base_images`: array of VM objects (OCI-pulled images)
- `working_vms`: array of VM objects (locally created/cloned VMs)

## Endpoints

### Health

#### `GET /api/health`

Returns server status and version.

### Settings

#### `GET /api/settings/github-token`

Get GitHub token configuration status (without revealing the actual token).

Response:

```json
{
  "configured": true,
  "masked_token": "ghp_...xyz"
}
```

Example:

```bash
curl -H "X-Local-Token: $TOKEN" http://127.0.0.1:8000/api/settings/github-token
```

#### `POST /api/settings/github-token`

Set or clear the GitHub API token.

Request body:

```json
{
  "token": "ghp_your_github_token_here"
}
```

To clear the token, send an empty string:

```json
{
  "token": ""
}
```

Example:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Local-Token: $TOKEN" \
  -d '{"token":"ghp_your_token_here"}' \
  http://127.0.0.1:8000/api/settings/github-token
```

### Tart

#### `GET /api/tart/version`

Returns the installed Tart version.

Example:

```bash
curl -H "X-Local-Token: $TOKEN" http://127.0.0.1:8000/api/tart/version
```

### VMs

#### `GET /api/vms/available-images`

Get list of available Cirrus Labs macOS images from GitHub API.

**Requires a GitHub personal access token to be configured** (see Settings endpoints).

Returns an array of `VMImageModel` objects with available macOS images that can be pulled.

Example:

```bash
curl -H "X-Local-Token: $TOKEN" http://127.0.0.1:8000/api/vms/available-images
```

#### `GET /api/vms/categorized`

Get VMs categorized as base images (OCI-pulled) vs working VMs (locally created/cloned).

Returns a `VMImagesSummary` object.

Example:

```bash
curl -H "X-Local-Token: $TOKEN" http://127.0.0.1:8000/api/vms/categorized
```

#### `GET /api/vms`

Returns the current cached inventory of VMs.

Example:

```bash
curl -H "X-Local-Token: $TOKEN" http://127.0.0.1:8000/api/vms
```

#### `POST /api/vms/refresh`

Triggers a full refresh from Tart (calls `tart list` and `tart ip`) and returns the refreshed VM list.

Example:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Local-Token: $TOKEN" \
  -d '{}' \
  http://127.0.0.1:8000/api/vms/refresh
```

#### `GET /api/vms/{vm_name}`

Returns a single VM (from cached inventory) by name.

#### `GET /api/vms/{vm_name}/config`

Returns VM configuration details using `tart get <name> --format json`.

- Cached server-side in memory.
- Use `?force_refresh=true` to bypass cache.

Example:

```bash
curl -H "X-Local-Token: $TOKEN" \
  "http://127.0.0.1:8000/api/vms/my-vm/config?force_refresh=true"
```

### VM actions (tasks)

These endpoints return a `Task` immediately. You can:

- Poll via `GET /api/tasks/{task_id}`
- Subscribe via WebSocket `GET /ws/tasks/{task_id}?token=...`

#### `POST /api/vms/{vm_name}/start`

Starts a VM.

Current behavior:

- Runs `tart run --vnc --no-graphics <vm_name>` detached
- Polls `tart ip <vm_name>` and stores `ip_address` in the task result when available

Task `result` includes:

- `message`
- `ip_address` (nullable)
- `vnc_url` (nullable; `vnc://<ip>`)

Example:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Local-Token: $TOKEN" \
  -d '{}' \
  http://127.0.0.1:8000/api/vms/my-vm/start
```

#### `POST /api/vms/{vm_name}/stop`

Stops a VM.

Current behavior:

- Runs `tart stop --timeout 30 <vm_name>` with a fallback to `tart stop <vm_name>`

Example:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Local-Token: $TOKEN" \
  -d '{}' \
  http://127.0.0.1:8000/api/vms/my-vm/stop
```

#### `POST /api/vms/{vm_name}/delete`

Deletes a VM.

Example:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Local-Token: $TOKEN" \
  -d '{}' \
  http://127.0.0.1:8000/api/vms/my-vm/delete
```

#### `POST /api/vms/pull`

Pulls a VM from an OCI registry.

Request body:

```json
{ "oci_url": "ghcr.io/cirruslabs/macos-ventura-base:latest" }
```

Example:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Local-Token: $TOKEN" \
  -d '{"oci_url":"ghcr.io/cirruslabs/macos-ventura-base:latest"}' \
  http://127.0.0.1:8000/api/vms/pull
```

#### `POST /api/vms/{vm_name}/clone`

Clone a VM and optionally start it with VNC.

Request body:

```json
{
  "new_name": "my-cloned-vm",
  "start_after_clone": false
}
```

Example:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Local-Token: $TOKEN" \
  -d '{"new_name":"my-clone","start_after_clone":true}' \
  http://127.0.0.1:8000/api/vms/base-image/clone
```

#### `POST /api/vms/create`

Create a new VM by cloning a base image and configuring CPU/memory/disk.

Request body:

```json
{
  "name": "my-new-vm",
  "source_vm": "ghcr.io/cirruslabs/macos-ventura-base:latest",
  "cpu": 4,
  "memory": 8,
  "disk_size": 50
}
```

Example:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  -H "X-Local-Token: $TOKEN" \
  -d '{"name":"dev-vm","source_vm":"ghcr.io/cirruslabs/macos-ventura-base:latest","cpu":4,"memory":8,"disk_size":50}' \
  http://127.0.0.1:8000/api/vms/create
```

### Tasks

#### `GET /api/tasks/active`

Returns a list of all active (non-completed, non-failed) tasks.

Example:

```bash
curl -H "X-Local-Token: $TOKEN" http://127.0.0.1:8000/api/tasks/active
```

#### `GET /api/tasks/{task_id}`

Returns the latest state of a task.

Example:

```bash
curl -H "X-Local-Token: $TOKEN" http://127.0.0.1:8000/api/tasks/<task_id>
```

### WebSocket

#### `GET /ws/tasks/{task_id}?token=<token>`

Streams task updates (JSON-serialized `Task` model). The server sends updates whenever task state/logs change.

Example:

- URL: `ws://127.0.0.1:8000/ws/tasks/<task_id>?token=<token>`
