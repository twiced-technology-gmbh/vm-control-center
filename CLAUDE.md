# VM Control Center

FastAPI + Jinja2 + SSE app for managing Orchard VMs and Tart images.
Stack: Python (FastAPI, aiohttp, pydantic-settings), vanilla JS, Font Awesome icons.

## UI Patterns

### Overflow Dropdown Button

All groups of related action buttons use a single blue `⋮` trigger that opens a floating dropdown menu. **Never render 3+ inline action buttons side by side** — collapse them into this pattern.

**When to use a dropdown vs direct buttons:**
- 3+ actions → dropdown
- 1–2 actions with no grouping needed → direct `icon-btn` buttons

**HTML structure:**
```html
<div class="vm-overflow">
  <button class="icon-btn icon-btn-blue vm-overflow-btn" data-tooltip="Actions">
    <i class="fas fa-ellipsis-vertical" style="font-size: 11px;"></i>
  </button>
  <div class="vm-overflow-menu">
    <div class="vm-overflow-section-label">Control</div>
    <button class="vm-overflow-item" data-action="start" disabled>
      <i class="fas fa-play" style="color: #4ade80;"></i> Start
    </button>
    <button class="vm-overflow-item" data-action="stop">
      <i class="fas fa-stop" style="color: #fb923c;"></i> Stop
    </button>
    <div class="vm-overflow-divider"></div>
    <div class="vm-overflow-section-label">Persist</div>
    <button class="vm-overflow-item" data-action="snapshot">
      <i class="fas fa-camera" style="color: #fbbf24;"></i> Create Snapshot
    </button>
  </div>
</div>
```

**Section labels** group related items. Use short labels: `Control`, `Persist`, `Manage`, `Create`, `Bulk`, `Monitor`, `Hosts`.

**Icon color conventions:**
| Color | Hex | Use |
|-------|-----|-----|
| green | `#4ade80` | create, start |
| orange | `#fb923c` | stop, refresh |
| red | `#f87171` | destroy, remove |
| blue | `#60a5fa` | edit, restart |
| amber | `#fbbf24` | snapshot, git |
| teal | `#2dd4bf` | recreate |
| indigo | `#a5b4fc` | manage/list |
| cyan | `#22d3ee` | config, service |
| purple | `#c084fc` | auth, settings |

**Delegating to hidden buttons** (when JS handlers are wired by element ID):
```html
<!-- retain ID for JS, hide visually -->
<button id="refresh-btn" style="display:none"><i class="fas fa-sync-alt"></i></button>

<!-- dropdown item delegates -->
<button class="vm-overflow-item" onclick="document.getElementById('refresh-btn').click()">
  <i class="fas fa-sync-alt" style="color: #fb923c;"></i> Refresh
</button>
```

**JS toggle handler** (one block handles all `.vm-overflow-btn` instances — already in index.html):
- Closes all menus on outside click or scroll
- Flips menu upward if not enough space below the button
- Uses `position: fixed` so it escapes `overflow: hidden` table containers

### Prompt Modal

Destructive or complex operations (create VM, add/remove/rename hosts, delete VM) show a CLI prompt modal instead of executing directly. The modal auto-copies the prompt to clipboard on open.

```js
showPrompt('Action Title', 'multi-line CLI instructions...');
```

`showPrompt` handles: displaying the modal, auto-copying to clipboard, showing a "Copied" badge.

### Host Colors

Host names are colored using a deterministic hash function `_hostColor(name)` — no hardcoded name→color mappings. Any new host automatically gets a consistent color.

## Config

All settings are prefixed `VMCC_` and can be set in `.env` or as env vars. Key settings:

| Variable | Default | Purpose |
|----------|---------|---------|
| `VMCC_SSH_KEY` | — | SSH key for Caddy host access |
| `VMCC_SSH_USER` | `admin` | SSH user on Caddy host |
| `VMCC_CADDY_HOST` | auto-detected from Orchard URL | Host to SSH into for Caddyfile edits |
| `VMCC_CADDY_BINARY` | `caddy` | Path to caddy binary on Caddy host |
| `VMCC_REMOTE_TART_PATH` | `/opt/homebrew/bin/tart` | Tart binary on remote workers |
