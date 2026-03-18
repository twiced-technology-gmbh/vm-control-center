"""Main FastAPI application for VM Control Center."""
import asyncio
import base64
import hmac
import json
import logging
import os
import re
import socket
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote, urlparse

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    status,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from pydantic import BaseModel

from . import __version__
from .config import settings
from .models import (
    CreateVMRequest,
    TartImageModel,
    TaskModel,
    TaskStatus,
    VMModel,
    VMImageModel,
    VMStatus,
)
from .tasks import task_manager


_VM_PATH_SETUP = (
    'export PATH="$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:$PATH" && '
    '[ -f "$HOME/.profile" ] && . "$HOME/.profile" 2>/dev/null; '
)

# Track background tasks for proper cleanup
background_tasks: set = set()


def create_background_task(coro):
    """Create a background task and track it for cleanup."""
    task = asyncio.create_task(coro)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task


class GitHubTokenRequest(BaseModel):
    token: Optional[str] = None


class RecreateVMRequest(BaseModel):
    snapshot: Optional[str] = None


class GitProfileRequest(BaseModel):
    label: str
    host: str
    name: Optional[str] = None
    email: Optional[str] = None
    ssh_key: Optional[str] = None


class ApplyProfileRequest(BaseModel):
    profile_id: str



class DeleteSnapshotsRequest(BaseModel):
    names: List[str]


class SyncPath(BaseModel):
    vm: str
    local: Optional[str] = None


class ServiceConfigRequest(BaseModel):
    systemd_unit: str
    app_dir: str
    dev_command: Optional[str] = None
    dev_proc_pattern: Optional[str] = None
    caddy_domain: Optional[str] = None
    prod_upstream: Optional[str] = None
    dev_host: Optional[str] = None
    sync_paths: Optional[List[SyncPath]] = None


class ServiceModeRequest(BaseModel):
    mode: str
    dev_host: Optional[str] = None
    sync_locals: Optional[dict[str, str]] = None


# Set up logging
logging.basicConfig(level=logging.DEBUG if settings.DEBUG else logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan handler."""
    logger.info("VM Control Center is starting up...")
    logger.info(f"Orchard controller: {settings.ORCHARD_URL or 'not configured'}")
    logger.info(f"Token file: {settings.TOKEN_FILE}")

    # Initial VM list refresh from Orchard
    try:
        await task_manager.refresh_inventory_best_effort()
        logger.info("Initial VM list refreshed from Orchard")
    except Exception as e:
        logger.error(f"Failed to refresh initial VM list: {e}")

    # Background inventory monitoring
    task_manager.start_inventory_monitoring(interval_seconds=10.0)

    # Background task cleanup
    task_manager.start_task_cleanup(interval_seconds=300.0, ttl_seconds=3600.0)

    yield

    # Shutdown
    await task_manager.stop_inventory_monitoring()
    await task_manager.stop_task_cleanup()
    await task_manager.close()

    if background_tasks:
        logger.info(f"Cancelling {len(background_tasks)} background tasks...")
        for task in background_tasks:
            task.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)


# Initialize FastAPI app
app = FastAPI(
    title="VM Control Center",
    description="Web interface for managing Orchard VMs",
    version=__version__,
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


@app.get("/appicon.png", include_in_schema=False)
async def app_icon():
    icon_path = Path(__file__).resolve().parent.parent / "appicon.png"
    return FileResponse(icon_path)

# Set up CORS — allow local + any *.twiced.de origin (all behind Tailscale/Caddy)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?|https://.*\.twiced\.de",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Set up templates
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# Set up static files
app.mount(
    "/static",
    StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")),
    name="static",
)


# Dependency to verify API token
async def verify_token(x_local_token: Optional[str] = Header(default=None)):
    if not x_local_token or not hmac.compare_digest(x_local_token, settings.SECRET_KEY):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing X-Local-Token header",
        )
    return x_local_token


# --- Health & Info ---

@app.get("/api/health")
async def health_check():
    return {"status": "ok", "version": __version__}


async def _fetch_orchard_hosts() -> list:
    """Fetch Orchard workers and enrich with Tailscale IPs."""
    workers = await task_manager.get_workers()
    if not workers:
        return []

    ts_raw = await _get_tailscale_name_to_ip()
    ts_name_to_ip = {k.lower(): v for k, v in ts_raw.items()}

    hosts = []
    for w in workers:
        resources = w.get("resources", {})
        name = w.get("name")
        hosts.append({
            "name": name,
            "ip": ts_name_to_ip.get(name.lower()) if name else None,
            "last_seen": w.get("last_seen"),
            "cores": resources.get("org.cirruslabs.logical-cores"),
            "memory_mb": resources.get("org.cirruslabs.memory-mib"),
            "vm_slots": resources.get("org.cirruslabs.tart-vms"),
            "created_at": w.get("createdAt"),
        })
    return hosts


@app.get("/api/hosts", dependencies=[Depends(verify_token)])
async def get_orchard_hosts():
    """Get all Orchard workers with their resources and status."""
    try:
        return await _fetch_orchard_hosts()
    except Exception as e:
        logger.warning(f"Failed to fetch Orchard workers: {e}")
        return []


@app.get("/api/events")
async def events_stream(request: Request, token: str = ""):
    """SSE endpoint — pushes VM + host state on every inventory refresh."""
    if not token or not hmac.compare_digest(token, settings.SECRET_KEY):
        raise HTTPException(status_code=401, detail="Invalid token")

    async def generate():
        q = task_manager.subscribe_inventory()
        try:
            # Send initial state immediately
            yield await _build_sse_event()
            while True:
                # Wait for next inventory change (or check disconnect every 30s)
                try:
                    await asyncio.wait_for(q.get(), timeout=30.0)
                    yield await _build_sse_event()
                except asyncio.TimeoutError:
                    # Send keepalive comment to prevent connection timeout
                    yield ": keepalive\n\n"
                if await request.is_disconnected():
                    break
        finally:
            task_manager.unsubscribe_inventory(q)

    return StreamingResponse(generate(), media_type="text/event-stream")


async def _build_sse_event() -> str:
    """Build SSE data payload with VMs, hosts, and images."""
    vms = await task_manager.get_inventory()
    vms_data = [vm.model_dump() for vm in vms]

    images = await task_manager.get_images()
    images_data = [img.model_dump() for img in images]

    try:
        hosts_data = await _fetch_orchard_hosts()
    except Exception:
        hosts_data = []

    payload = json.dumps({"vms": vms_data, "hosts": hosts_data, "images": images_data})
    return f"data: {payload}\n\n"


_tailscale_cache: Dict[str, str] = {}
_tailscale_cache_time: float = 0
_TAILSCALE_CACHE_TTL: float = 15.0  # seconds


async def _get_tailscale_name_to_ip() -> Dict[str, str]:
    """Get Tailscale hostname → IPv4 mapping for self + all peers (cached)."""
    global _tailscale_cache, _tailscale_cache_time

    now = time.monotonic()
    if _tailscale_cache and (now - _tailscale_cache_time) < _TAILSCALE_CACHE_TTL:
        return _tailscale_cache

    result: Dict[str, str] = {}
    try:
        proc = await asyncio.create_subprocess_exec(
            "tailscale", "status", "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            ts = json.loads(stdout)
            # Self
            self_name = ts.get("Self", {}).get("HostName", "")
            for tip in ts.get("TailscaleIPs", []):
                if "." in tip:
                    result[self_name] = tip
                    break
            # Peers
            for peer in ts.get("Peer", {}).values():
                hostname = peer.get("HostName", "")
                for tip in peer.get("TailscaleIPs", []):
                    if "." in tip:
                        result[hostname] = tip
                        break
    except FileNotFoundError:
        pass

    _tailscale_cache = result
    _tailscale_cache_time = now
    return result


@app.get("/api/tailscale/peers", dependencies=[Depends(verify_token)])
async def get_tailscale_peers():
    """Get Tailscale hostname → IP mapping for all peers."""
    return await _get_tailscale_name_to_ip()


# --- Services (Caddy + Tailscale discovery) ---

@app.get("/api/services", dependencies=[Depends(verify_token)])
async def get_services():
    """Get service URLs for VMs by querying Caddy admin API and matching via Tailscale."""
    import aiohttp

    if not settings.CADDY_ADMIN_URL:
        return {}

    try:
        # 1. Fetch Caddy routes: IP → [domains]
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{settings.CADDY_ADMIN_URL}/config/apps/http/servers",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return {}
                servers = await resp.json()

        ip_to_domains: Dict[str, list] = {}
        for server in servers.values():
            # Only process standard HTTPS servers (port 443)
            listen_addrs = server.get("listen", [])
            listen_port = ""
            for addr in listen_addrs:
                if ":" in addr:
                    listen_port = addr.rsplit(":", 1)[-1]
                    break
            if listen_port != "443":
                continue

            for route in server.get("routes", []):
                hosts = []
                for m in route.get("match", []):
                    hosts.extend(m.get("host", []))
                if not hosts:
                    continue
                for handler in route.get("handle", []):
                    for sub in handler.get("routes", []):
                        for h in sub.get("handle", []):
                            if h.get("handler") == "reverse_proxy":
                                for upstream in h.get("upstreams", []):
                                    dial = upstream.get("dial", "")
                                    if dial:
                                        ip = dial.split(":")[0]
                                        ip_to_domains.setdefault(ip, []).append(hosts)

        # 2. Get Tailscale peers: IP → hostname (reverse of name→IP)
        name_to_ip = await _get_tailscale_name_to_ip()
        ip_to_tailscale_name: Dict[str, str] = {ip: name for name, ip in name_to_ip.items()}

        # 3. Get VM names from Orchard inventory
        vms = await task_manager.get_inventory()
        vm_names = {vm.name for vm in vms}

        # 4. Match: Caddy IP → Tailscale hostname → VM name
        def find_vm_name(tailscale_hostname: str) -> Optional[str]:
            if tailscale_hostname in vm_names:
                return tailscale_hostname
            stripped = tailscale_hostname.removesuffix("-vm")
            if stripped in vm_names:
                return stripped
            return None

        services: Dict[str, list] = {}
        for ip, domain_lists in ip_to_domains.items():
            ts_name = ip_to_tailscale_name.get(ip)
            if not ts_name:
                continue
            vm_name = find_vm_name(ts_name)
            key = vm_name or ts_name
            for hosts in domain_lists:
                primary = hosts[0]
                services.setdefault(key, []).append({
                    "url": f"https://{primary}",
                    "label": primary.removesuffix(".twiced.de"),
                })

        return services

    except Exception as e:
        logger.warning(f"Failed to fetch services from Caddy: {e}")
        return {}


# --- GitHub settings ---

@app.get("/api/settings/github-token", dependencies=[Depends(verify_token)])
async def get_github_token_status():
    return {
        "configured": settings.GITHUB_TOKEN is not None and len(settings.GITHUB_TOKEN) > 0,
        "masked_token": f"{settings.GITHUB_TOKEN[:4]}...{settings.GITHUB_TOKEN[-4:]}" if settings.GITHUB_TOKEN and len(settings.GITHUB_TOKEN) > 8 else None
    }


@app.post("/api/settings/github-token", dependencies=[Depends(verify_token)])
async def set_github_token(payload: GitHubTokenRequest):
    try:
        if payload.token and payload.token.strip():
            token = payload.token.strip()
            settings.GITHUB_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
            settings.GITHUB_TOKEN_FILE.write_text(token)
            settings.GITHUB_TOKEN_FILE.chmod(0o600)
            settings.GITHUB_TOKEN = token
            return {"status": "success", "message": "GitHub token configured"}
        else:
            if settings.GITHUB_TOKEN_FILE.exists():
                settings.GITHUB_TOKEN_FILE.unlink()
            settings.GITHUB_TOKEN = None
            return {"status": "success", "message": "GitHub token cleared"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save GitHub token: {e}")


# --- Available images (GitHub API) ---

@app.get("/api/vms/available-images", response_model=List[VMImageModel])
async def get_available_images():
    """Get available Cirrus Labs macOS images from GitHub API."""
    import aiohttp

    if not settings.GITHUB_TOKEN:
        return []

    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.github.com/orgs/cirruslabs/packages"
            params = {"package_type": "container", "per_page": 100}
            headers = {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Authorization": f"Bearer {settings.GITHUB_TOKEN}",
            }

            async with session.get(url, params=params, headers=headers) as response:
                if response.status != 200:
                    return []
                packages = await response.json()

            macos_images = []
            for package in packages:
                package_name = package.get("name", "")
                if not package_name.startswith("macos-"):
                    continue

                versions_url = f"https://api.github.com/orgs/cirruslabs/packages/container/{package_name}/versions"
                async with session.get(versions_url, headers=headers, params={"per_page": 10}) as versions_response:
                    if versions_response.status != 200:
                        continue
                    versions = await versions_response.json()
                    all_tags = []
                    latest_updated = None
                    for version in versions:
                        tags = version.get("metadata", {}).get("container", {}).get("tags", [])
                        all_tags.extend(tags)
                        updated = version.get("updated_at")
                        if updated and (not latest_updated or updated > latest_updated):
                            latest_updated = updated

                    default_tag = "latest" if "latest" in all_tags else (all_tags[0] if all_tags else "latest")
                    macos_images.append(VMImageModel(
                        name=package_name,
                        url=f"ghcr.io/cirruslabs/{package_name}:{default_tag}",
                        description=package.get("description"),
                        tags=all_tags,
                        updated_at=latest_updated,
                    ))

            macos_images.sort(key=lambda x: x.name)
            return macos_images

    except Exception as e:
        logger.exception("Failed to fetch available images from GitHub API")
        return []


# --- Images (local Tart) ---

@app.get("/api/images", response_model=List[TartImageModel], dependencies=[Depends(verify_token)])
async def list_images():
    """List local Tart images (source images + OCI cache)."""
    return await task_manager.get_images()


@app.delete("/api/images/{name}", dependencies=[Depends(verify_token)])
async def delete_image(name: str):
    """Delete a local Tart image by name."""
    proc = await asyncio.create_subprocess_exec(
        settings.TART_PATH, "delete", name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise HTTPException(status_code=400, detail=stderr.decode().strip())
    await task_manager._refresh_images()
    return {"ok": True}


class RenameImageRequest(BaseModel):
    new_name: str


@app.post("/api/images/{name}/rename", dependencies=[Depends(verify_token)])
async def rename_image(name: str, body: RenameImageRequest):
    """Rename a local Tart image by moving its directory."""
    tart_dir = Path.home() / ".tart" / "vms"
    src = tart_dir / name
    dst = tart_dir / body.new_name
    if not src.exists():
        raise HTTPException(status_code=404, detail=f"Image '{name}' not found")
    if dst.exists():
        raise HTTPException(status_code=409, detail=f"Image '{body.new_name}' already exists")
    src.rename(dst)
    await task_manager._refresh_images()
    return {"ok": True}


# --- VM endpoints (Orchard) ---

@app.get("/api/vms", response_model=List[VMModel], dependencies=[Depends(verify_token)])
async def list_vms():
    """List all VMs from Orchard + stopped VMs from saved configs."""
    vms = await task_manager.get_inventory()
    live_names = {vm.name for vm in vms}
    # Add stopped VMs from saved configs that aren't in Orchard
    for config in _list_saved_vm_configs():
        name = config.get("name")
        if name and name not in live_names:
            image = config.get("image", name)
            vms.append(VMModel(
                name=name,
                status=VMStatus.STOPPED,
                os=task_manager._detect_os(image),
                worker=config.get("worker") or config.get("assignedWorker"),
                image=image or None,
                cpu=config.get("cpu"),
                memory=config.get("memory"),
                restart_policy=config.get("restartPolicy") or config.get("restart_policy"),
            ))
    vms.sort(key=lambda v: v.name)
    return vms


@app.post("/api/vms/refresh", response_model=List[VMModel], dependencies=[Depends(verify_token)])
async def refresh_vms():
    """Force refresh VM list from Orchard + stopped VMs."""
    await task_manager.refresh_inventory()
    return await list_vms()


@app.get("/api/vms/{vm_name}", response_model=VMModel, dependencies=[Depends(verify_token)])
async def get_vm(vm_name: str):
    """Get details for a specific VM."""
    all_vms = await list_vms()
    for vm in all_vms:
        if vm.name == vm_name:
            return vm
    raise HTTPException(status_code=404, detail=f"VM '{vm_name}' not found")


@app.post("/api/vms", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def create_vm(payload: CreateVMRequest):
    """Create a new VM via Orchard."""
    task = await task_manager.create_task("create_vm")
    create_background_task(_create_vm(task.id, payload))
    return task


async def _create_vm(task_id: str, payload: CreateVMRequest):
    try:
        await task_manager.update_task(task_id, status=TaskStatus.RUNNING)
        await task_manager.create_vm(
            name=payload.name,
            image=payload.image,
            cpu=payload.cpu,
            memory=payload.memory,
            disk_size=payload.disk_size,
            startup_script=payload.startup_script,
            task_id=task_id,
        )
        await task_manager.update_task(
            task_id,
            status=TaskStatus.COMPLETED,
            result={"message": f"VM '{payload.name}' created"},
        )
    except Exception as e:
        logger.exception(f"Failed to create VM '{payload.name}'")
        await task_manager.update_task(task_id, status=TaskStatus.FAILED, error=str(e))


@app.get("/api/snapshots", dependencies=[Depends(verify_token)])
async def list_all_snapshots():
    """List all snapshots across all workers (local tart images that aren't orchard clones or OCI)."""
    results = []
    seen_workers = set()

    # Collect workers from inventory
    vms = await task_manager.get_inventory()
    vm_names = {vm.name for vm in vms}
    for vm in vms:
        if vm.worker:
            seen_workers.add(vm.worker)
    if not seen_workers:
        seen_workers.add(socket.gethostname())

    for worker in seen_workers:
        rc, stdout, _ = await _run_tart_on_worker(["list", "--format", "json"], worker)
        if rc != 0:
            continue
        raw = json.loads(stdout)
        for item in raw:
            name = item.get("Name", "")
            source = item.get("Source", "local")
            if source != "local":
                continue
            if name.startswith("orchard-"):
                continue
            if "@sha256:" in name:
                continue
            # Determine which VM this snapshot belongs to and its type
            vm_owner = None
            snap_type = "unknown"
            for vm_name in sorted(vm_names, key=len, reverse=True):
                if name == vm_name:
                    vm_owner = vm_name
                    snap_type = "base"
                    break
                if name.startswith(vm_name + "-"):
                    vm_owner = vm_name
                    suffix = name[len(vm_name) + 1:]
                    if re.match(r"\d{4}-\d{2}-\d{2}$", suffix):
                        snap_type = "daily"
                    elif suffix.startswith("snap-"):
                        snap_type = "manual"
                    elif suffix == "hourly" or suffix.startswith("hourly"):
                        snap_type = "hourly"
                    else:
                        snap_type = "other"
                    break
            if not vm_owner:
                # Template or orphan image — skip
                if name.startswith("templates-"):
                    continue
                snap_type = "other"
            results.append({
                "name": name,
                "vm": vm_owner,
                "type": snap_type,
                "worker": worker,
                "disk_size": item.get("Disk"),
                "size": item.get("Size"),
                "last_accessed": item.get("Accessed"),
            })
    return results


@app.post("/api/snapshots/delete", dependencies=[Depends(verify_token)])
async def delete_global_snapshots(body: DeleteSnapshotsRequest):
    """Delete snapshots by name. Finds the correct worker automatically."""
    # Build name→worker map
    all_snaps = await list_all_snapshots()
    snap_worker = {s["name"]: s["worker"] for s in all_snaps}

    deleted = []
    failed = []
    for name in body.names:
        worker = snap_worker.get(name)
        if not worker:
            failed.append({"name": name, "error": "snapshot not found"})
            continue
        rc, _, stderr = await _run_tart_on_worker(["delete", name], worker)
        if rc == 0:
            deleted.append(name)
        else:
            failed.append({"name": name, "error": stderr.strip()})

    await task_manager.refresh_inventory_best_effort()
    return {"deleted": deleted, "failed": failed}


@app.post("/api/vms/bulk/start", dependencies=[Depends(verify_token)])
async def bulk_start_vms():
    """Start all stopped VMs."""
    vms = await task_manager.get_inventory()
    stopped = [vm for vm in vms if vm.status != VMStatus.RUNNING]
    tasks = []
    for vm in stopped:
        task = await task_manager.create_task("start_vm")
        create_background_task(_start_vm(task.id, vm.name))
        tasks.append({"vm": vm.name, "task_id": task.id})
    return {"started": len(tasks), "tasks": tasks}


@app.post("/api/vms/bulk/restart", dependencies=[Depends(verify_token)])
async def bulk_restart_vms():
    """Restart all running VMs."""
    vms = await task_manager.get_inventory()
    running = [vm for vm in vms if vm.status == VMStatus.RUNNING]
    tasks = []
    for vm in running:
        task = await task_manager.create_task("restart_vm")
        create_background_task(_restart_vm(task.id, vm.name))
        tasks.append({"vm": vm.name, "task_id": task.id})
    return {"restarted": len(tasks), "tasks": tasks}


@app.post("/api/vms/bulk/snapshot", dependencies=[Depends(verify_token)])
async def bulk_snapshot_vms():
    """Snapshot all running VMs."""
    vms = await task_manager.get_inventory()
    running = [vm for vm in vms if vm.status == VMStatus.RUNNING]
    tasks = []
    for vm in running:
        task = await task_manager.create_task("snapshot_vm")
        create_background_task(_snapshot_vm(task.id, vm.name))
        tasks.append({"vm": vm.name, "task_id": task.id})
    return {"snapshotted": len(tasks), "tasks": tasks}


@app.post("/api/vms/{vm_name}/start", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def start_vm(vm_name: str):
    """Start a VM via Orchard (recreate from same config if in terminal state)."""
    task = await task_manager.create_task("start_vm")
    create_background_task(_start_vm(task.id, vm_name))
    return task


@app.post("/api/vms/{vm_name}/stop", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def stop_vm(vm_name: str):
    """Stop a VM via Orchard (deletes the Orchard VM; Tart image is preserved)."""
    task = await task_manager.create_task("stop_vm")
    create_background_task(_stop_vm(task.id, vm_name))
    return task


@app.post("/api/vms/{vm_name}/restart", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def restart_vm(vm_name: str):
    """Restart a VM via Orchard (delete + recreate)."""
    task = await task_manager.create_task("restart_vm")
    create_background_task(_restart_vm(task.id, vm_name))
    return task


@app.post("/api/vms/{vm_name}/snapshot", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def snapshot_vm(vm_name: str):
    """Snapshot a running VM: clone Orchard tart clone back to source image."""
    task = await task_manager.create_task("snapshot_vm")
    create_background_task(_snapshot_vm(task.id, vm_name))
    return task


@app.get("/api/vms/{vm_name}/snapshots", dependencies=[Depends(verify_token)])
async def list_snapshots(vm_name: str):
    """List available snapshots for a VM (source image + hourly/daily snapshots)."""
    try:
        _, worker = await task_manager.resolve_tart_name(vm_name)
    except RuntimeError:
        # VM not running — check saved config for worker, default to local
        worker = socket.gethostname()

    rc, stdout, _ = await _run_tart_on_worker(["list", "--format", "json"], worker)
    if rc != 0:
        return []

    raw = json.loads(stdout)

    # Get birth times for accurate timestamps (tart's Accessed field is unreliable)
    birth_times = await _get_snapshot_birth_times(worker, vm_name)

    snapshots = []
    prefixes = (vm_name + "-", )
    for item in raw:
        name = item.get("Name", "")
        if name == vm_name or (name.startswith(prefixes[0]) and not name.startswith("orchard-")):
            snapshots.append({
                "name": name,
                "disk_size": item.get("Disk"),
                "size": item.get("Size"),
                "last_accessed": birth_times.get(name) or item.get("Accessed"),
                "state": item.get("State", "stopped"),
            })
    return snapshots


async def _get_snapshot_birth_times(worker: str, vm_name: str) -> dict:
    """Get filesystem birth times for snapshot images on a worker."""
    if _is_local_worker(worker):
        tart_dir = Path.home() / ".tart" / "vms"
        dirs = [str(p) for p in tart_dir.iterdir() if p.is_dir() and (p.name == vm_name or p.name.startswith(f"{vm_name}-"))]
        if not dirs:
            return {}
        cmd = ["stat", "-f", "%N\t%SB", "-t", "%Y-%m-%dT%H:%M:%SZ"] + dirs
    else:
        cmd_str = f"stat -f '%N\\t%SB' -t '%Y-%m-%dT%H:%M:%SZ' ~/.tart/vms/{vm_name}/ ~/.tart/vms/{vm_name}-*/ 2>/dev/null"
        cmd = _build_tart_ssh_cmd(worker, cmd_str)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout_data, _ = await proc.communicate()
        result = {}
        for line in stdout_data.decode().strip().split("\n"):
            if "\t" not in line:
                continue
            path, birth = line.split("\t", 1)
            name = path.rstrip("/").rsplit("/", 1)[-1]
            result[name] = birth
        return result
    except Exception:
        return {}


@app.post("/api/vms/{vm_name}/recreate", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def recreate_vm(vm_name: str, body: RecreateVMRequest = RecreateVMRequest()):
    """Recreate a VM from a snapshot. If no snapshot specified, uses the source image."""
    task = await task_manager.create_task("recreate_vm")
    create_background_task(_recreate_vm(task.id, vm_name, snapshot=body.snapshot))
    return task


@app.post("/api/vms/{vm_name}/snapshots/delete", dependencies=[Depends(verify_token)])
async def delete_snapshots(vm_name: str, body: DeleteSnapshotsRequest):
    """Delete one or more snapshots for a VM."""
    try:
        _, worker = await task_manager.resolve_tart_name(vm_name)
    except RuntimeError:
        worker = socket.gethostname()

    deleted = []
    failed = []
    for name in body.names:
        # Safety: only allow deleting snapshots that belong to this VM
        if name != vm_name and not name.startswith(f"{vm_name}-"):
            failed.append({"name": name, "error": "not a snapshot of this VM"})
            continue
        rc, _, stderr = await _run_tart_on_worker(["delete", name], worker)
        if rc == 0:
            deleted.append(name)
        else:
            failed.append({"name": name, "error": stderr.strip()})

    await task_manager.refresh_inventory_best_effort()
    return {"deleted": deleted, "failed": failed}


_VM_CONFIGS_DIR = Path.home() / ".vm-control-center" / "vm-configs"
_GIT_PROFILES_FILE = Path.home() / ".vm-control-center" / "git-profiles.json"


def _load_git_profiles() -> list[dict]:
    try:
        return json.loads(_GIT_PROFILES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_git_profiles(profiles: list[dict]) -> None:
    _GIT_PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    _GIT_PROFILES_FILE.write_text(json.dumps(profiles, indent=2))
    _GIT_PROFILES_FILE.chmod(0o600)


def _save_vm_config(vm_name: str, config: dict) -> None:
    """Persist VM config to disk so Start can recreate after Stop."""
    _VM_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    (_VM_CONFIGS_DIR / f"{vm_name}.json").write_text(json.dumps(config, indent=2))


def _load_vm_config(vm_name: str) -> dict | None:
    """Load a previously saved VM config from disk."""
    try:
        return json.loads((_VM_CONFIGS_DIR / f"{vm_name}.json").read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _delete_saved_vm_config(vm_name: str) -> None:
    """Remove a saved VM config from disk."""
    path = _VM_CONFIGS_DIR / f"{vm_name}.json"
    path.unlink(missing_ok=True)


def _list_saved_vm_configs() -> list[dict]:
    """List all saved VM configs."""
    if not _VM_CONFIGS_DIR.exists():
        return []
    configs = []
    for path in _VM_CONFIGS_DIR.glob("*.json"):
        try:
            configs.append(json.loads(path.read_text()))
        except Exception:
            pass
    return configs


async def _get_vm_config(vm_name: str) -> dict:
    """Fetch current VM config from Orchard for recreating it."""
    return await task_manager.get_vm(vm_name)


def _build_create_body(config: dict) -> dict:
    """Build a create-VM request body from an existing Orchard VM config."""
    body = {
        "name": config["name"],
        "image": config.get("image", config["name"]),
        "cpu": config.get("cpu", 4),
        "memory": config.get("memory", 8192),
        "headless": config.get("headless", True),
    }
    if config.get("diskSize"):
        body["disk_size"] = config["diskSize"]
    if config.get("restartPolicy"):
        body["restartPolicy"] = config["restartPolicy"]
    if config.get("labels"):
        body["labels"] = config["labels"]
    if config.get("resources"):
        body["resources"] = config["resources"]
    return body


async def _stop_vm(task_id: str, vm_name: str):
    """Stop = delete the Orchard VM (Tart source image is preserved on the worker)."""
    try:
        await task_manager.update_task(task_id, status=TaskStatus.RUNNING, log=f"Stopping VM '{vm_name}'...")

        # Save config before deleting so Start can recreate with same settings
        try:
            config = await _get_vm_config(vm_name)
            _save_vm_config(vm_name, config)
            worker = config.get("worker", "unknown")
            await task_manager.update_task(task_id, log=f"VM is on worker '{worker}', config saved, removing from Orchard...")
        except Exception:
            pass

        await task_manager.delete_vm(vm_name, task_id=task_id)
        await task_manager.update_task(
            task_id, status=TaskStatus.COMPLETED,
            result={"message": f"VM '{vm_name}' stopped"},
        )
    except Exception as e:
        logger.exception(f"Failed to stop VM '{vm_name}'")
        await task_manager.update_task(task_id, status=TaskStatus.FAILED, error=str(e))


async def _start_vm(task_id: str, vm_name: str):
    """Start = recreate the Orchard VM from its config (or source image name)."""
    try:
        await task_manager.update_task(task_id, status=TaskStatus.RUNNING, log=f"Starting VM '{vm_name}'...")

        # Check if VM already exists and is running
        try:
            config = await _get_vm_config(vm_name)
            status = config.get("status", "").lower()
            if status == "running":
                await task_manager.update_task(
                    task_id, status=TaskStatus.COMPLETED,
                    result={"message": f"VM '{vm_name}' is already running"},
                    log="VM is already running, nothing to do",
                )
                return

            # VM exists but in terminal state — delete and recreate
            await task_manager.update_task(task_id, log=f"VM in '{status}' state, deleting to recreate...")
            create_body = _build_create_body(config)
            await task_manager.delete_vm(vm_name, task_id=task_id)
        except RuntimeError:
            # VM doesn't exist — try saved config first, then fall back to defaults
            saved = _load_vm_config(vm_name)
            if saved:
                await task_manager.update_task(task_id, log=f"VM not found in Orchard, recreating from saved config...")
                create_body = _build_create_body(saved)
            else:
                await task_manager.update_task(task_id, log=f"VM not found in Orchard, creating from image '{vm_name}' with defaults...")
                create_body = {
                    "name": vm_name,
                    "image": vm_name,
                    "cpu": 4,
                    "memory": 8192,
                    "headless": True,
                }

        await task_manager.update_task(task_id, log=f"Creating VM '{vm_name}'...")
        status_code, resp = await task_manager.orchard_create_vm(create_body)
        if status_code not in (200, 201):
            msg = resp.get("message", resp) if isinstance(resp, dict) else str(resp)
            raise RuntimeError(f"Failed to create VM: {msg}")

        _delete_saved_vm_config(vm_name)
        await task_manager.update_task(
            task_id, status=TaskStatus.COMPLETED,
            result={"message": f"VM '{vm_name}' started"},
            log=f"VM '{vm_name}' is now running",
        )
        await task_manager.refresh_inventory_best_effort()
    except Exception as e:
        logger.exception(f"Failed to start VM '{vm_name}'")
        await task_manager.update_task(task_id, status=TaskStatus.FAILED, error=str(e))


async def _restart_vm(task_id: str, vm_name: str):
    """Restart = delete + recreate the Orchard VM."""
    try:
        await task_manager.update_task(task_id, status=TaskStatus.RUNNING, log=f"Restarting VM '{vm_name}'...")

        # Fetch and save config before deleting
        config = await _get_vm_config(vm_name)
        _save_vm_config(vm_name, config)
        create_body = _build_create_body(config)

        # Delete
        await task_manager.update_task(task_id, log=f"Stopping VM '{vm_name}'...")
        await task_manager.delete_vm(vm_name, task_id=task_id)

        # Recreate
        await task_manager.update_task(task_id, log=f"Starting VM '{vm_name}'...")
        status_code, resp = await task_manager.orchard_create_vm(create_body)
        if status_code not in (200, 201):
            msg = resp.get("message", resp) if isinstance(resp, dict) else str(resp)
            raise RuntimeError(f"Failed to recreate VM: {msg}")

        await task_manager.update_task(
            task_id, status=TaskStatus.COMPLETED,
            result={"message": f"VM '{vm_name}' restarted"},
            log=f"VM '{vm_name}' is now running",
        )
        await task_manager.refresh_inventory_best_effort()
    except Exception as e:
        logger.exception(f"Failed to restart VM '{vm_name}'")
        await task_manager.update_task(task_id, status=TaskStatus.FAILED, error=str(e))


def _build_tart_ssh_cmd(worker: str, tart_cmd: str) -> list:
    """Build SSH command to run a tart command on a remote worker."""
    ssh = ["ssh"]
    if settings.SSH_KEY:
        ssh += ["-i", settings.SSH_KEY]
    ssh += ["-o", "StrictHostKeyChecking=accept-new",
            f"{settings.SSH_USER}@{worker}", tart_cmd]
    return ssh


def _is_local_worker(worker: str) -> bool:
    return worker and worker.lower() == socket.gethostname().lower()


async def _run_tart_on_worker(args: list, worker: str, timeout: float = 30.0) -> tuple:
    """Run a tart command on the correct worker (local or SSH). Returns (returncode, stdout, stderr)."""
    import shlex
    if _is_local_worker(worker):
        cmd = [settings.TART_PATH] + args
    else:
        tart_cmd = f"{settings.REMOTE_TART_PATH} {' '.join(shlex.quote(a) for a in args)}"
        cmd = _build_tart_ssh_cmd(worker, tart_cmd)

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 1, "", f"Command timed out after {timeout}s"
    return proc.returncode, stdout.decode(), stderr.decode()


async def _snapshot_vm(task_id: str, vm_name: str):
    """Snapshot: clone the running Orchard tart clone to a timestamped image."""
    from datetime import datetime
    try:
        await task_manager.update_task(task_id, status=TaskStatus.RUNNING, log=f"Creating snapshot of VM '{vm_name}'...")

        tart_name, worker = await task_manager.resolve_tart_name(vm_name)
        await task_manager.update_task(task_id, log=f"Resolved tart clone: {tart_name} on {worker}")

        # Create uniquely named snapshot
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        snapshot_name = f"{vm_name}-snap-{timestamp}"

        await task_manager.update_task(task_id, log=f"Cloning {tart_name} → {snapshot_name}...")
        rc, _, stderr = await _run_tart_on_worker(["clone", tart_name, snapshot_name], worker, timeout=300.0)
        if rc != 0:
            raise RuntimeError(f"tart clone failed: {stderr.strip()}")

        await task_manager.update_task(
            task_id, status=TaskStatus.COMPLETED,
            result={"message": f"Snapshot '{snapshot_name}' created"},
            log=f"Snapshot complete: '{snapshot_name}' on {worker}",
        )
        await task_manager.refresh_inventory_best_effort()
    except Exception as e:
        logger.exception(f"Failed to snapshot VM '{vm_name}'")
        await task_manager.update_task(task_id, status=TaskStatus.FAILED, error=str(e))


async def _recreate_vm(task_id: str, vm_name: str, snapshot: Optional[str] = None):
    """Recreate: delete Orchard VM and create fresh from a snapshot image."""
    image_name = snapshot or vm_name
    try:
        await task_manager.update_task(task_id, status=TaskStatus.RUNNING, log=f"Recreating VM '{vm_name}' from image '{image_name}'...")

        config = await _get_vm_config(vm_name)
        _save_vm_config(vm_name, config)
        create_body = _build_create_body(config)
        create_body["image"] = image_name

        await task_manager.update_task(task_id, log=f"Deleting Orchard VM '{vm_name}'...")
        await task_manager.delete_vm(vm_name, task_id=task_id)

        await task_manager.update_task(task_id, log=f"Creating VM '{vm_name}' from image '{image_name}'...")
        status_code, resp = await task_manager.orchard_create_vm(create_body)
        if status_code not in (200, 201):
            msg = resp.get("message", resp) if isinstance(resp, dict) else str(resp)
            raise RuntimeError(f"Failed to recreate VM: {msg}")

        await task_manager.update_task(
            task_id, status=TaskStatus.COMPLETED,
            result={"message": f"VM '{vm_name}' recreated from '{image_name}'"},
            log=f"VM '{vm_name}' is now running from '{image_name}'",
        )
        await task_manager.refresh_inventory_best_effort()
    except Exception as e:
        logger.exception(f"Failed to recreate VM '{vm_name}'")
        await task_manager.update_task(task_id, status=TaskStatus.FAILED, error=str(e))


@app.delete("/api/vms/{vm_name}", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def destroy_vm(vm_name: str):
    """Destroy a VM via Orchard (stops and removes it)."""
    task = await task_manager.create_task("destroy_vm")
    create_background_task(_destroy_vm(task.id, vm_name))
    return task


async def _destroy_vm(task_id: str, vm_name: str):
    try:
        await task_manager.update_task(task_id, status=TaskStatus.RUNNING)
        await task_manager.delete_vm(vm_name, task_id=task_id)
        _delete_saved_vm_config(vm_name)
        await task_manager.update_task(
            task_id,
            status=TaskStatus.COMPLETED,
            result={"message": f"VM '{vm_name}' destroyed"},
        )
    except Exception as e:
        logger.exception(f"Failed to destroy VM '{vm_name}'")
        await task_manager.update_task(task_id, status=TaskStatus.FAILED, error=str(e))


# --- Git Profiles ---

@app.get("/api/git-profiles", dependencies=[Depends(verify_token)])
async def list_git_profiles():
    """List all git profiles with secrets masked."""
    profiles = _load_git_profiles()
    masked = []
    for p in profiles:
        mp = {**p}
        if mp.get("ssh_key"):
            mp["ssh_key"] = "***"
        mp.pop("token", None)
        masked.append(mp)
    return masked


@app.get("/api/git-profiles/{profile_id}", dependencies=[Depends(verify_token)])
async def get_git_profile(profile_id: str):
    """Get a single git profile with secrets unmasked (for edit form)."""
    profiles = _load_git_profiles()
    for p in profiles:
        if p["id"] == profile_id:
            return p
    raise HTTPException(status_code=404, detail="Profile not found")


@app.post("/api/git-profiles", dependencies=[Depends(verify_token)])
async def create_git_profile(req: GitProfileRequest):
    """Create a new git profile."""
    profiles = _load_git_profiles()
    profile = {
        "id": str(uuid.uuid4()),
        "label": req.label,
        "host": req.host,
        "name": req.name or "",
        "email": req.email or "",
        "ssh_key": req.ssh_key or "",
    }
    profiles.append(profile)
    _save_git_profiles(profiles)
    return profile


@app.put("/api/git-profiles/{profile_id}", dependencies=[Depends(verify_token)])
async def update_git_profile(profile_id: str, req: GitProfileRequest):
    """Update an existing git profile. Empty ssh_key = keep existing."""
    profiles = _load_git_profiles()
    for i, p in enumerate(profiles):
        if p["id"] == profile_id:
            p["label"] = req.label
            p["host"] = req.host
            p["name"] = req.name or ""
            p["email"] = req.email or ""
            if req.ssh_key:
                p["ssh_key"] = req.ssh_key
            p.pop("match", None)
            profiles[i] = p
            _save_git_profiles(profiles)
            return p
    raise HTTPException(status_code=404, detail="Profile not found")


@app.delete("/api/git-profiles/{profile_id}", dependencies=[Depends(verify_token)])
async def delete_git_profile(profile_id: str):
    """Delete a git profile."""
    profiles = _load_git_profiles()
    profiles = [p for p in profiles if p["id"] != profile_id]
    _save_git_profiles(profiles)
    return {"message": "Profile deleted"}


@app.post("/api/vms/{vm_name}/git-profiles/apply", dependencies=[Depends(verify_token)])
async def apply_git_profile(vm_name: str, req: ApplyProfileRequest):
    """Apply a single git profile to a VM via tart exec (replaces any previous profile)."""
    all_profiles = _load_git_profiles()
    profile = next((p for p in all_profiles if p["id"] == req.profile_id), None)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    tart_name, worker = await task_manager.resolve_tart_name(vm_name)
    path_setup = _VM_PATH_SETUP

    commands = []
    pid = profile["id"]
    host = profile["host"]
    host_alias = f"{host}-{pid[:8]}"
    key_file = f"~/.ssh/git-profile-{pid[:8]}"

    # SSH key
    if profile.get("ssh_key"):
        b64_key = base64.b64encode(profile["ssh_key"].encode()).decode()
        commands.append("mkdir -p ~/.ssh && chmod 700 ~/.ssh")
        commands.append(f"echo '{b64_key}' | base64 -d > {key_file} && chmod 600 {key_file}")
        commands.append(f"ssh-keyscan -T 5 {host} >> ~/.ssh/known_hosts 2>/dev/null")

        marker = f"# git-profile {pid}"
        ssh_block = (
            f"{marker}\\n"
            f"Host {host_alias}\\n"
            f"  HostName {host}\\n"
            f"  User git\\n"
            f"  IdentityFile {key_file}\\n"
            f"  IdentitiesOnly yes\\n"
            f"{marker} end"
        )
        commands.append(f"sed -i '/^{marker}$/,/^{marker} end$/d' ~/.ssh/config 2>/dev/null; true")
        commands.append(f"echo -e '{ssh_block}' >> ~/.ssh/config && chmod 600 ~/.ssh/config")

    # Git identity
    if profile.get("name"):
        commands.append(f'git config --global user.name "{profile["name"]}"')
    if profile.get("email"):
        commands.append(f'git config --global user.email "{profile["email"]}"')

    cmd_str = path_setup + " && ".join(commands)
    rc, stdout, stderr = await _run_tart_on_worker(["exec", tart_name, "bash", "-c", cmd_str], worker)
    output = (stdout + stderr).strip()
    success = rc == 0

    # Track applied profile (replaces previous)
    if success:
        vm_cfg = _load_vm_config(vm_name) or {}
        vm_cfg["applied_git_profile"] = req.profile_id
        _save_vm_config(vm_name, vm_cfg)

    return {"success": success, "profile": profile["label"], "output": output}


@app.get("/api/vms/{vm_name}/git-profiles/applied", dependencies=[Depends(verify_token)])
async def get_applied_git_profile(vm_name: str):
    """Get the profile ID applied to this VM."""
    vm_cfg = _load_vm_config(vm_name) or {}
    return {"applied": vm_cfg.get("applied_git_profile")}



@app.get("/api/vms/{vm_name}/cli-auth-status", dependencies=[Depends(verify_token)])
async def get_cli_auth_status(vm_name: str):
    """Check GitHub/GitLab CLI auth status on a VM."""
    tart_name, worker = await task_manager.resolve_tart_name(vm_name)
    path_setup = _VM_PATH_SETUP

    cmd_str = path_setup + (
        'echo "::GH::"; (gh auth status 2>&1 || echo "NOT_AUTHED"); '
        'echo "::GL::"; (glab auth status 2>&1 || echo "NOT_AUTHED"); '
        'echo "::GIT::"; git config --global user.name 2>/dev/null; echo "::GITEMAIL::"; git config --global user.email 2>/dev/null'
    )

    rc, stdout, stderr = await _run_tart_on_worker(["exec", tart_name, "bash", "-c", cmd_str], worker)
    output = stdout + stderr

    def _extract(marker, next_marker):
        start = output.find(marker)
        if start == -1:
            return ""
        start += len(marker)
        end = output.find(next_marker, start) if next_marker else len(output)
        return output[start:end].strip() if end != -1 else output[start:].strip()

    gh_output = _extract("::GH::", "::GL::")
    gl_output = _extract("::GL::", "::GIT::")

    gh_logged_in = "Logged in" in gh_output
    gl_logged_in = "Logged in" in gl_output

    # Extract account info if logged in (strip file paths like /home/admin/...)
    gh_account = ""
    if gh_logged_in:
        for line in gh_output.split("\n"):
            if "account" in line.lower():
                gh_account = re.sub(r"\s*\([/~][^)]*\)", "", line.strip().lstrip("✓").lstrip("-").strip())
                break

    gl_account = ""
    if gl_logged_in:
        for line in gl_output.split("\n"):
            if "logged in" in line.lower() or "account" in line.lower():
                gl_account = re.sub(r"\s*\([/~][^)]*\)", "", line.strip().lstrip("✓").lstrip("-").strip())
                break

    # Extract current git identity from VM
    git_name = _extract("::GIT::", "::GITEMAIL::").strip()
    git_email = _extract("::GITEMAIL::", None).strip()

    return {
        "github": {"authenticated": gh_logged_in, "account": gh_account},
        "gitlab": {"authenticated": gl_logged_in, "account": gl_account},
        "git_identity": {"name": git_name, "email": git_email},
    }


# --- Service Mode ---


@app.get("/api/vms/{vm_name}/service-config", dependencies=[Depends(verify_token)])
async def get_service_config(vm_name: str):
    """Return the service config for a VM (or null if not configured)."""
    vm_cfg = _load_vm_config(vm_name) or {}
    return {"service": vm_cfg.get("service")}


@app.put("/api/vms/{vm_name}/service-config", dependencies=[Depends(verify_token)])
async def set_service_config(vm_name: str, req: ServiceConfigRequest):
    """Save service config to the VM's config JSON."""
    vm_cfg = _load_vm_config(vm_name) or {}
    svc = {
        "systemd_unit": req.systemd_unit,
        "app_dir": req.app_dir,
        "dev_command": req.dev_command or "make dev",
        "dev_proc_pattern": req.dev_proc_pattern or "make dev",
    }
    if req.caddy_domain:
        svc["caddy_domain"] = req.caddy_domain
    if req.prod_upstream:
        svc["prod_upstream"] = req.prod_upstream
    if req.dev_host:
        svc["dev_host"] = req.dev_host
    if req.sync_paths:
        svc["sync_paths"] = [sp.model_dump() for sp in req.sync_paths]
    vm_cfg["service"] = svc
    _save_vm_config(vm_name, vm_cfg)
    return {"service": vm_cfg["service"]}


@app.delete("/api/vms/{vm_name}/service-config", dependencies=[Depends(verify_token)])
async def delete_service_config(vm_name: str):
    """Remove service config from the VM's config JSON."""
    vm_cfg = _load_vm_config(vm_name) or {}
    vm_cfg.pop("service", None)
    _save_vm_config(vm_name, vm_cfg)
    return {"deleted": True}


def _pgrep_safe_pattern(pattern: str) -> str:
    """Wrap first char in brackets so pgrep/pkill doesn't match itself."""
    if not pattern:
        return pattern
    return f"[{pattern[0]}]{pattern[1:]}"


async def _get_caddy_upstream(domain: str) -> str | None:
    """Read the current Caddy upstream for a domain from the Caddyfile on the Caddy host."""
    try:
        cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5"]
        if settings.SSH_KEY:
            cmd += ["-i", settings.SSH_KEY]
        cmd += [f"{settings.SSH_USER}@{settings.CADDY_HOST}",
                f"grep -A5 '{domain}' ~/Applications/caddy/Caddyfile | grep reverse_proxy | head -1"]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        # e.g. "	reverse_proxy 100.112.238.48:3200"
        line = stdout.decode().strip()
        if "reverse_proxy" in line:
            return line.split("reverse_proxy")[-1].strip()
    except Exception:
        pass
    return None


async def _resolve_dev_upstream(service: dict) -> str | None:
    """Resolve dev_host + prod port into a dev upstream like '100.x.x.x:3200'."""
    dev_host = service.get("dev_host")
    prod_upstream = service.get("prod_upstream", "")
    if not dev_host:
        return None
    ts = await _get_tailscale_name_to_ip()
    ts_lower = {k.lower(): v for k, v in ts.items()}
    ip = ts_lower.get(dev_host.lower())
    if not ip:
        return None
    port = prod_upstream.split(":")[-1] if ":" in prod_upstream else "80"
    return f"{ip}:{port}"


@app.get("/api/vms/{vm_name}/service-mode", dependencies=[Depends(verify_token)])
async def get_service_mode(vm_name: str):
    """Detect current mode by checking which Caddy upstream is active."""
    vm_cfg = _load_vm_config(vm_name) or {}
    service = vm_cfg.get("service")
    if not service:
        return {"mode": None}

    caddy_domain = service.get("caddy_domain")
    if not caddy_domain:
        return {"mode": "prod"}

    dev_upstream = await _resolve_dev_upstream(service)
    if not dev_upstream:
        return {"mode": "prod"}

    current = await _get_caddy_upstream(caddy_domain)
    mode = "dev" if current and current == dev_upstream else "prod"
    result = {"mode": mode}
    if mode == "dev":
        prod_upstream = service.get("prod_upstream", "")
        port = prod_upstream.split(":")[-1] if ":" in prod_upstream else None
        if port:
            result["dev_url"] = f"http://localhost:{port}"
        result["caddy_url"] = f"https://{caddy_domain}"
    return result


@app.post("/api/vms/{vm_name}/service-mode", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def set_service_mode(vm_name: str, req: ServiceModeRequest, request: Request):
    """Switch a VM's service between dev and prod mode (async task)."""
    if req.mode not in ("dev", "prod"):
        raise HTTPException(status_code=400, detail="mode must be 'dev' or 'prod'")
    vm_cfg = _load_vm_config(vm_name) or {}
    service = vm_cfg.get("service")
    if not service:
        raise HTTPException(status_code=404, detail="No service config for this VM")

    # Request overrides take precedence over config
    effective_service = {**service}
    if req.dev_host:
        effective_service["dev_host"] = req.dev_host
    elif not effective_service.get("dev_host"):
        # Auto-detect from caller's IP → Tailscale hostname
        caller_ip = request.client.host if request.client else None
        if caller_ip:
            ts = await _get_tailscale_name_to_ip()
            ip_to_name = {v: k for k, v in ts.items()}
            effective_service["dev_host"] = ip_to_name.get(caller_ip, caller_ip)

    # Merge sync_locals into sync_paths
    if req.sync_locals:
        for sp in effective_service.get("sync_paths", []):
            if isinstance(sp, dict) and sp["vm"] in req.sync_locals:
                sp["local"] = req.sync_locals[sp["vm"]]

    task = await task_manager.create_task("switch_service_mode")
    create_background_task(_switch_service_mode(task.id, vm_name, req.mode, effective_service))
    return task


async def _update_caddy_upstream(domain: str, new_upstream: str, task_id: str) -> None:
    """Update a Caddy reverse_proxy upstream on the Caddy host and reload."""
    import shlex
    await task_manager.update_task(task_id, log=f"Updating Caddy: {domain} → {new_upstream}")
    cf = "~/Applications/caddy/Caddyfile"
    # Find the domain line, then the reverse_proxy within the next 10 lines, replace by line number
    find_cmd = (
        f'LINE=$(grep -n "{domain}" {cf} | head -1 | cut -d: -f1) && '
        f'RPLINE=$(sed -n "${{LINE}},$((LINE+10))p" {cf} | grep -n "reverse_proxy" | head -1 | cut -d: -f1) && '
        f'ACTUAL=$((LINE + RPLINE - 1)) && '
        f'sed -i "" "${{ACTUAL}}s|reverse_proxy .*|reverse_proxy {new_upstream}|" {cf}'
    )
    reload_cmd = f"{settings.CADDY_BINARY} reload --config {cf}"
    remote_cmd = f"{find_cmd} && {reload_cmd}"

    cmd = ["ssh", "-o", "StrictHostKeyChecking=accept-new", "-o", "ConnectTimeout=5"]
    if settings.SSH_KEY:
        cmd += ["-i", settings.SSH_KEY]
    cmd += [f"{settings.SSH_USER}@{settings.CADDY_HOST}", remote_cmd]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    if proc.returncode != 0:
        raise RuntimeError(f"Caddy update failed: {(stdout + stderr).decode().strip()}")


async def _sync_db_via_api(service: dict, direction: str, local_path: str, task_id: str) -> bool:
    """Sync a DB via cockpit's /api/db/backup and /api/db/restore endpoints.
    Returns True if the API-based sync was used."""
    prod_upstream = service.get("prod_upstream", "")
    dev_upstream = await _resolve_dev_upstream(service)
    # Determine the running cockpit URL (whichever side is currently active)
    if direction == "from_vm":
        url = f"http://{prod_upstream}"
    else:
        url = f"http://{dev_upstream}" if dev_upstream else None
    if not url:
        return False
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            if direction == "from_vm":
                async with session.get(f"{url}/api/db/backup") as resp:
                    if resp.status != 200:
                        return False
                    data = await resp.read()
                    if len(data) < 100:
                        return False
                    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
                    Path(local_path).write_bytes(data)
                    return True
            else:
                local = Path(local_path)
                if not local.exists() or local.stat().st_size < 100:
                    return False
                # Create a clean backup locally first (merge WAL)
                import sqlite3 as _sqlite3
                backup_path = Path(f"/tmp/_sync_backup_{os.getpid()}.db")
                try:
                    src = _sqlite3.connect(str(local))
                    dst = _sqlite3.connect(str(backup_path))
                    src.backup(dst)
                    dst.close()
                    src.close()
                    data = backup_path.read_bytes()
                finally:
                    backup_path.unlink(missing_ok=True)
                async with session.post(f"{url}/api/db/restore", data=data) as resp:
                    return resp.status == 200
    except Exception as e:
        logger.warning("API-based DB sync failed (%s): %s", direction, e)
        return False


async def _sync_file_from_vm(tart_name: str, worker: str, vm_path: str, local_path: str,
                              service: dict | None = None) -> None:
    """Copy a DB from VM to local. Prefers /api/db/backup, falls back to tart exec."""
    if service and vm_path.endswith(".db"):
        if await _sync_db_via_api(service, "from_vm", local_path, ""):
            return
    # Fallback: raw file copy via tart exec
    import shlex
    cat_cmd = f"cat {shlex.quote(vm_path)}"
    if _is_local_worker(worker):
        cmd = [settings.TART_PATH, "exec", tart_name, "bash", "-c", cat_cmd]
    else:
        tart_cmd = f"{settings.REMOTE_TART_PATH} exec {shlex.quote(tart_name)} bash -c {shlex.quote(cat_cmd)}"
        cmd = _build_tart_ssh_cmd(worker, tart_cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout_raw, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to read {vm_path} from VM")
    if len(stdout_raw) == 0:
        logger.warning("Skipping sync from VM: %s is empty", vm_path)
        return
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    Path(local_path).write_bytes(stdout_raw)


async def _sync_file_to_vm(tart_name: str, worker: str, local_path: str, vm_path: str,
                             service: dict | None = None) -> None:
    """Copy a DB from local to VM. Prefers /api/db/restore, falls back to tart exec."""
    local = Path(local_path)
    if not local.exists() or local.stat().st_size == 0:
        logger.warning("Skipping sync to VM: local file %s is missing or empty", local_path)
        return
    if service and local_path.endswith(".db"):
        if await _sync_db_via_api(service, "to_vm", local_path, ""):
            return
    # Fallback: raw file copy via tart exec
    import shlex
    data = local.read_bytes()
    if len(data) == 0:
        return
    if _is_local_worker(worker):
        cmd = [settings.TART_PATH, "exec", tart_name, "bash", "-c", f"cat > {vm_path}"]
    else:
        tart_cmd = f"{settings.REMOTE_TART_PATH} exec {shlex.quote(tart_name)} bash -c {shlex.quote(f'cat > {vm_path}')}"
        cmd = _build_tart_ssh_cmd(worker, tart_cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_raw = await asyncio.wait_for(proc.communicate(input=data), timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(f"Failed to write {vm_path} to VM: {stderr_raw.decode().strip()}")


async def _vm_systemctl(tart_name: str, worker: str, action: str, unit: str) -> None:
    """Run systemctl start/stop on a VM."""
    cmd_str = f"sudo systemctl {action} {unit}"
    await _run_tart_on_worker(["exec", tart_name, "bash", "-c", cmd_str], worker, timeout=15)


async def _switch_service_mode(task_id: str, vm_name: str, mode: str, service: dict):
    caddy_domain = service.get("caddy_domain")
    prod_upstream = service.get("prod_upstream")
    unit = service.get("systemd_unit")
    sync_paths = service.get("sync_paths", [])
    try:
        await task_manager.update_task(task_id, status=TaskStatus.RUNNING, log=f"Switching {vm_name} to {mode} mode...")
        tart_name, worker = await task_manager.resolve_tart_name(vm_name)

        # Filter sync_paths to those that have a local path resolved
        resolved_syncs = [sp for sp in sync_paths if isinstance(sp, dict) and sp.get("local")]

        if mode == "dev":
            # 1. Sync DB from VM BEFORE stopping (API backup works while running)
            for sp in resolved_syncs:
                await task_manager.update_task(task_id, log=f"Syncing {sp['vm']} → {sp['local']}")
                await _sync_file_from_vm(tart_name, worker, sp["vm"], sp["local"], service=service)

            # 2. Stop VM service
            if unit:
                await task_manager.update_task(task_id, log=f"Stopping {unit} on VM...")
                await _vm_systemctl(tart_name, worker, "stop", unit)

            # 3. Update Caddy to point to dev host
            if caddy_domain and prod_upstream:
                dev_upstream = await _resolve_dev_upstream(service)
                if not dev_upstream:
                    raise RuntimeError(f"Cannot resolve dev host '{service.get('dev_host')}'")
                await _update_caddy_upstream(caddy_domain, dev_upstream, task_id)

        else:  # prod
            # 1. Sync DB to VM via restore API (cockpit on VM will be started after)
            # First start the service so restore API is available
            if unit:
                await task_manager.update_task(task_id, log=f"Starting {unit} on VM...")
                await _vm_systemctl(tart_name, worker, "start", unit)
                await asyncio.sleep(2)  # wait for service to be ready

            for sp in resolved_syncs:
                await task_manager.update_task(task_id, log=f"Syncing {sp['local']} → {sp['vm']}")
                await _sync_file_to_vm(tart_name, worker, sp["local"], sp["vm"], service=service)

            # 2. Update Caddy to point to VM
            if caddy_domain and prod_upstream:
                await _update_caddy_upstream(caddy_domain, prod_upstream, task_id)

        await task_manager.update_task(
            task_id, status=TaskStatus.COMPLETED,
            log=f"Switched to {mode} mode."
        )
    except Exception as e:
        await task_manager.update_task(task_id, status=TaskStatus.FAILED, error=str(e))


# --- Login Status Check (all VMs) ---

_login_cache: Dict[str, Dict] = {}
_login_cache_file = Path.home() / ".vm-control-center" / "logins-cache.json"


def _load_login_cache():
    global _login_cache
    if _login_cache_file.exists():
        try:
            _login_cache = json.loads(_login_cache_file.read_text())
        except Exception:
            _login_cache = {}


def _save_login_cache():
    try:
        _login_cache_file.parent.mkdir(parents=True, exist_ok=True)
        _login_cache_file.write_text(json.dumps(_login_cache))
    except Exception:
        logger.warning("Failed to save login cache", exc_info=True)


_load_login_cache()

_LOGIN_CHECK_CMD = (
    _VM_PATH_SETUP +
    'echo "::GH::"; gh auth status 2>&1 || echo "NOT_AUTHED"; '
    'echo "::GL::"; glab auth status 2>&1 || echo "NOT_AUTHED"; '
    'echo "::CLAUDE::"; claude auth status 2>&1 || echo "NOT_AUTHED"; '
    'echo "::GEMINI::"; '
    'if test -f ~/.gemini/google_accounts.json; then echo "LOGIN_OK"; '
    'elif test -f ~/.gemini/settings.json; then echo "LOGIN_OK"; '
    'else echo "LOGIN_NO"; fi; '
    'echo "::CODEX::"; '
    'if test -f ~/.codex/auth.json; then echo "LOGIN_OK"; else echo "LOGIN_NO"; fi; '
    'echo "::COPILOT::"; '
    'if test -f ~/.copilot/config.json; then '
    'grep -q last_logged_in_user ~/.copilot/config.json 2>/dev/null && echo "LOGIN_OK" || echo "LOGIN_NO"; '
    'else echo "LOGIN_NO"; fi'
)


def _parse_login_output(output: str) -> Dict[str, bool]:
    markers = ["::GH::", "::GL::", "::CLAUDE::", "::GEMINI::", "::CODEX::", "::COPILOT::"]
    keys = ["github", "gitlab", "claude", "gemini", "codex", "copilot"]

    def _section(marker, next_marker=None):
        start = output.find(marker)
        if start == -1:
            return ""
        start += len(marker)
        if next_marker:
            end = output.find(next_marker, start)
            if end == -1:
                end = len(output)
        else:
            end = len(output)
        return output[start:end].strip()

    sections = {}
    for i, marker in enumerate(markers):
        next_m = markers[i + 1] if i + 1 < len(markers) else None
        sections[keys[i]] = _section(marker, next_m)

    return {
        "github": "Logged in" in sections["github"],
        "gitlab": "Logged in" in sections["gitlab"],
        "claude": (
            sections["claude"] != ""
            and "NOT_AUTHED" not in sections["claude"]
            and "not logged in" not in sections["claude"].lower()
            and "error" not in sections["claude"].lower()
        ),
        "gemini": "LOGIN_OK" in sections["gemini"],
        "codex": "LOGIN_OK" in sections["codex"],
        "copilot": "LOGIN_OK" in sections["copilot"],
    }


async def _check_vm_logins(vm_name: str) -> Dict[str, bool]:
    import shlex
    tart_name, worker = await task_manager.resolve_tart_name(vm_name)
    is_local = _is_local_worker(worker)
    if is_local:
        cmd = [settings.TART_PATH, "exec", tart_name, "bash", "-c", _LOGIN_CHECK_CMD]
    else:
        # For SSH, the command string is interpreted by the remote shell.
        # shlex.quote wraps the compound command so it arrives intact to bash -c.
        tart_cmd = f"{settings.REMOTE_TART_PATH} exec {tart_name} bash -c {shlex.quote(_LOGIN_CHECK_CMD)}"
        cmd = _build_tart_ssh_cmd(worker, tart_cmd)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        proc.kill()
        return {}
    output = stdout.decode() + stderr.decode()
    return _parse_login_output(output)


@app.get("/api/logins", dependencies=[Depends(verify_token)])
async def get_logins():
    """Return cached login status for all VMs."""
    return _login_cache


@app.get("/api/logins/stream")
async def stream_login_checks(request: Request, token: str = "", vm: str = ""):
    """SSE: check logins on running VMs, stream results per VM. Optional vm= to check one."""
    if not hmac.compare_digest(token, settings.SECRET_KEY):
        raise HTTPException(status_code=401, detail="Invalid token")

    async def event_generator():
        vms = await task_manager.get_inventory()
        if vm:
            running_vms = [v for v in vms if v.name == vm and v.status == VMStatus.RUNNING]
        else:
            running_vms = [v for v in vms if v.status == VMStatus.RUNNING]

        async def check_one(vm):
            try:
                result = await _check_vm_logins(vm.name)
                _login_cache[vm.name] = {**result, "checked_at": time.time()}
                return vm.name, result
            except Exception as e:
                logger.warning("Login check failed for %s: %s", vm.name, e)
                return vm.name, None

        tasks = [asyncio.create_task(check_one(vm)) for vm in running_vms]

        for coro in asyncio.as_completed(tasks):
            if await request.is_disconnected():
                break
            vm_name, result = await coro
            if result is not None:
                yield f"data: {json.dumps({'vm': vm_name, 'logins': result})}\n\n"

        _save_login_cache()
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- Tasks ---

@app.get("/api/tasks/active", response_model=List[TaskModel], dependencies=[Depends(verify_token)])
async def get_active_tasks():
    return [
        task for task in task_manager.tasks.values()
        if task.status in (TaskStatus.PENDING, TaskStatus.RUNNING)
    ]


@app.get("/api/tasks/{task_id}", response_model=TaskModel, dependencies=[Depends(verify_token)])
async def get_task(task_id: str):
    task = await task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found")
    return task


@app.websocket("/ws/tasks/{task_id}")
async def websocket_task(websocket: WebSocket, task_id: str):
    token = websocket.query_params.get("token")
    if not token or not hmac.compare_digest(token, settings.SECRET_KEY):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    task = await task_manager.get_task(task_id)
    if not task:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    await websocket.send_json(task.model_dump())

    async def send_keepalive():
        try:
            while True:
                await asyncio.sleep(30)
                try:
                    await websocket.send_text("")
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    keepalive_task = create_background_task(send_keepalive())

    try:
        async for update in task_manager.subscribe_to_task(task_id):
            try:
                await websocket.send_json(update.model_dump())
                if update.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    await websocket.close()
                    break
            except WebSocketDisconnect:
                break
    finally:
        keepalive_task.cancel()
        try:
            await keepalive_task
        except asyncio.CancelledError:
            pass


# --- Agent Login ---

AGENTS = {
    "claude": {
        "name": "Claude Code",
        "binary": "claude",
        "install": "sudo npm install -g @anthropic-ai/claude-code",
        "login": "claude auth login",
        "test": (
            'claude --version && echo "---" && '
            '(claude auth status 2>&1 || echo "Not logged in")'
        ),
    },
    "codex": {
        "name": "Codex",
        "binary": "codex",
        "install": "sudo npm install -g @openai/codex",
        "login": "codex login --device-auth",
        "test": (
            'codex --version && echo "---" && '
            "if test -f ~/.codex/auth.json; then "
            "jq -r '.tokens.id_token' ~/.codex/auth.json "
            "| cut -d. -f2 "
            "| base64 -d 2>/dev/null "
            "| jq -r '\"Account: \" + .email "
            "+ \" (\" + .[\"https://api.openai.com/auth\"].chatgpt_plan_type + \")\"' "
            '2>/dev/null || echo "Logged in (cannot parse token)"; '
            'else echo "Not logged in"; fi'
        ),
    },
    "gemini": {
        "name": "Gemini CLI",
        "binary": "gemini",
        "install": (
            "sudo npm install -g @google/gemini-cli && "
            "mkdir -p ~/.gemini"
        ),
        "login": "GOOGLE_GENAI_USE_GCA=true gemini -p hello",
        "test": (
            'gemini --version 2>&1 && echo "---" && '
            "if test -f ~/.gemini/google_accounts.json; then "
            "jq -r '\"Account: \" + .active' ~/.gemini/google_accounts.json 2>/dev/null; "
            'elif test -f ~/.gemini/settings.json; then '
            'echo "Auth: API key" && '
            "jq -r '.' ~/.gemini/settings.json 2>/dev/null; "
            'else echo "Not authenticated"; fi'
        ),
    },
    "copilot": {
        "name": "GitHub Copilot",
        "binary": "copilot",
        "install": "sudo npm install -g @github/copilot",
        "login": "copilot login",
        "test": (
            'copilot --version && echo "---" && '
            'if test -f ~/.copilot/config.json; then '
            'cat ~/.copilot/config.json 2>/dev/null; '
            'else echo "Not logged in"; fi'
        ),
    },
    "github": {
        "name": "GitHub CLI",
        "binary": "gh",
        "install": (
            "(type -p brew >/dev/null && brew install gh) || "
            "(curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg "
            "| sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg "
            '&& echo "deb [arch=$(dpkg --print-architecture) '
            "signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] "
            'https://cli.github.com/packages stable main" '
            "| sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null "
            "&& sudo apt update && sudo apt install gh -y)"
        ),
        "login": "gh auth login --web -p https -s repo,read:org,workflow",
        "test": (
            'gh --version && echo "---" && '
            '(gh auth status 2>&1 || echo "Not logged in")'
        ),
    },
    "gitlab": {
        "name": "GitLab CLI",
        "binary": "glab",
        "install": (
            "(type -p brew >/dev/null && brew install glab) || "
            "(curl -fsSL https://gitlab.com/gitlab-org/cli/-/releases/permalink/latest/downloads/glab_$(uname -s)_$(uname -m).tar.gz "
            "| sudo tar -xzf - -C /usr/local/bin glab)"
        ),
        "login": "glab auth login",
        "test": (
            'glab --version && echo "---" && '
            '(glab auth status 2>&1 || echo "Not logged in")'
        ),
    },
}


@app.websocket("/ws/agent-login/{vm_name}")
async def websocket_agent_login(websocket: WebSocket, vm_name: str):
    """Bidirectional WebSocket for interactive agent login inside a VM."""
    token = websocket.query_params.get("token")
    if not token or not hmac.compare_digest(token, settings.SECRET_KEY):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()

    # Wait for agent selection
    try:
        msg = await asyncio.wait_for(websocket.receive_json(), timeout=30)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        await websocket.close()
        return

    agent = msg.get("agent")
    action = msg.get("action", "login")
    if agent not in AGENTS:
        await websocket.send_json({"type": "error", "data": f"Unknown agent: {agent}"})
        await websocket.close()
        return
    if action not in ("login", "test"):
        await websocket.send_json({"type": "error", "data": f"Unknown action: {action}"})
        await websocket.close()
        return

    # Resolve VM to Tart clone name and worker
    try:
        tart_name, worker = await task_manager.resolve_tart_name(vm_name)
    except RuntimeError as e:
        await websocket.send_json({"type": "error", "data": str(e)})
        await websocket.close()
        return

    # Build exec command with auto-install
    cfg = AGENTS[agent]
    action_cmd = cfg[action]
    # Source profile, extend PATH, and suppress browser opening in headless VMs.
    # SSH_CONNECTION makes CLIs (Gemini, etc.) think it's a remote session → paste-code flow.
    # CI=true is a fallback signal for other CLIs that check for non-interactive environments.
    path_setup = (
        'export PATH="$HOME/.npm-global/bin:/opt/homebrew/bin:/usr/local/bin:$PATH" && '
        'export SSH_CONNECTION=1 && '
        'export CI=true && '
        '[ -f "$HOME/.profile" ] && . "$HOME/.profile" 2>/dev/null; '
    )
    install_prefix = (
        f'if ! command -v {cfg["binary"]} >/dev/null 2>&1; then '
        f'echo "Installing {cfg["name"]}..." && {cfg["install"]}; fi && '
    )
    cmd_str = path_setup + install_prefix + action_cmd

    is_local = _is_local_worker(worker)

    if is_local:
        cmd = [settings.TART_PATH, "exec", "-i", tart_name, "bash", "-c", cmd_str]
    else:
        escaped_cmd = cmd_str.replace("'", "'\\''")
        inner = f"{settings.REMOTE_TART_PATH} exec -i {tart_name} bash -c '{escaped_cmd}'"
        cmd = _build_tart_ssh_cmd(worker, inner)

    async def _find_agent_port_raw(tn, wk, loc):
        """List all listening ports inside the VM (works on macOS and Linux)."""
        port_cmd = (
            "(lsof -iTCP -sTCP:LISTEN -nP 2>/dev/null | grep -oE ':[0-9]+' | tr -d ':') || "
            "(ss -tlnp 2>/dev/null | grep -oP '(?<=:)\\d+')"
        )
        if loc:
            fp = [settings.TART_PATH, "exec", tn, "bash", "-c", port_cmd]
        else:
            fp = _build_tart_ssh_cmd(wk, f"{settings.REMOTE_TART_PATH} exec {tn} bash -c \"{port_cmd}\"")
        p = await asyncio.create_subprocess_exec(
            *fp, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=10)
        return out.decode().strip()

    await websocket.send_json({
        "type": "output",
        "data": f"$ {cfg['name']} {action}  (on {vm_name} @ {worker})\n",
    })

    # Snapshot existing ports in the VM before launching agent (for port detection)
    baseline_ports: set[str] = set()
    if agent in {"claude"}:
        try:
            baseline_scan = await _find_agent_port_raw(tart_name, worker, is_local)
            baseline_ports = set(baseline_scan.split())
            logger.info("agent-login: baseline ports in VM: %s", baseline_ports)
        except Exception:
            pass

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    # For claude: rewrite auth URL redirect_uri and start a local proxy
    # that automatically forwards browser OAuth callbacks to the VM
    captured_port = None
    output_buffer = ""
    url_rewritten = False
    proxy_server = None

    async def _find_agent_port():
        # Scan VM for listening ports, exclude baseline, return the new one
        try:
            raw = await _find_agent_port_raw(tart_name, worker, is_local)
            all_ports = set(raw.split())
            new_ports = all_ports - baseline_ports
            logger.info("agent-login: all_ports=%s baseline=%s new=%s", all_ports, baseline_ports, new_ports)
            numeric = sorted([p for p in new_ports if p.isdigit()], key=int)
            return numeric[-1] if numeric else ""
        except Exception:
            return ""

    async def _forward_to_vm(path_query: str):
        """Forward a callback request to the VM's claude callback server."""
        vm_url = f"http://localhost:{captured_port}{path_query}"
        curl_cmd = f"curl -s '{vm_url}'"
        if is_local:
            fwd = [settings.TART_PATH, "exec", tart_name, "bash", "-c", curl_cmd]
        else:
            fwd = _build_tart_ssh_cmd(worker, f"{settings.REMOTE_TART_PATH} exec {tart_name} bash -c \"{curl_cmd}\"")
        logger.info("agent-login proxy: forwarding %s to VM", path_query[:60])
        p = await asyncio.create_subprocess_exec(
            *fwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        await asyncio.wait_for(p.communicate(), timeout=15)

    async def _handle_proxy_request(reader, writer):
        """Local proxy handler: intercept browser callback and forward to VM."""
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            while True:
                line = await reader.readline()
                if line == b"\r\n" or not line:
                    break
            parts = request_line.decode().split()
            if len(parts) >= 2:
                path_query = parts[1]
                await websocket.send_json({"type": "output",
                    "data": "Browser callback received, forwarding to VM...\n"})
                await _forward_to_vm(path_query)
                body = ("<html><body style='font-family:system-ui;text-align:center;padding:60px;"
                        "background:#1a1a2e;color:#e0e0e0'>"
                        "<h2 style='color:#4ade80'>&#10003; Authentication successful</h2>"
                        "<p>You can close this tab.</p></body></html>")
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                             f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}".encode())
            else:
                writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
        except Exception as e:
            logger.warning("agent-login proxy error: %s", e)
        finally:
            await writer.drain()
            writer.close()

    # Agents that use localhost OAuth callbacks and need the proxy
    PROXY_AGENTS = {"claude"}

    async def relay_output():
        nonlocal captured_port, output_buffer, url_rewritten, proxy_server
        while True:
            chunk = await process.stdout.read(4096)
            if not chunk:
                break
            # Strip ANSI escape codes (colors, cursor, terminal mode switches)
            text = re.sub(r"\x1b[\[\(][0-9;?]*[a-zA-Z<>]", "", chunk.decode("utf-8", errors="replace"))
            # Filter noisy warnings (e.g. gemini scanning unreadable dirs)
            text = "\n".join(
                l for l in text.split("\n")
                if not l.startswith("[WARN] Skipping unreadable directory")
            )
            # For agents with OAuth callbacks: detect the redirect_uri, start a proxy
            if agent in PROXY_AGENTS and not url_rewritten:
                output_buffer += text
                # Case 1: remote redirect_uri (e.g. platform.claude.com) — rewrite + proxy
                remote_match = re.search(
                    r"redirect_uri=(https%3A%2F%2F[^&\s]+(?:callback|redirect)[^&\s]*)", output_buffer)
                # Case 2: localhost redirect_uri already present (e.g. codex, gemini)
                local_match = re.search(
                    r"redirect_uri=http%3A%2F%2Flocalhost%3A(\d+)%2F([^&\s]*)", output_buffer)
                # Also detect "localhost:<port>" in plain text output (e.g. "Starting local login server on http://localhost:1455")
                plain_match = re.search(r"localhost:(\d+)", output_buffer)
                if remote_match:
                    remote_redir = remote_match.group(1)
                    # Try to get port from output text first, then scan VM ports
                    port_from_output = plain_match.group(1) if plain_match else None
                    port = port_from_output
                    if not port:
                        # Wait for agent to start its callback server, then scan
                        for attempt in range(3):
                            await asyncio.sleep(1)
                            port = await _find_agent_port()
                            if port:
                                break
                    logger.info("agent-login: remote_match found, port_from_output=%s port_from_scan=%s", port_from_output, port)
                    if port and port.isdigit():
                        captured_port = port
                        local_redir = quote(f"http://localhost:{port}/callback", safe="")
                        output_buffer = output_buffer.replace(remote_redir, local_redir)
                        try:
                            proxy_server = await asyncio.start_server(
                                _handle_proxy_request, "127.0.0.1", int(port))
                            logger.info("agent-login: proxy on localhost:%s (rewrote URL) for %s", port, agent)
                        except OSError as e:
                            logger.warning("agent-login: proxy bind failed on %s: %s", port, e)
                    else:
                        logger.warning("agent-login: could not find agent port, URL not rewritten")
                    url_rewritten = True
                    await websocket.send_json({"type": "output", "data": output_buffer})
                    output_buffer = ""
                    continue
                elif local_match or plain_match:
                    port = local_match.group(1) if local_match else plain_match.group(1)
                    if port and port.isdigit():
                        captured_port = port
                        try:
                            proxy_server = await asyncio.start_server(
                                _handle_proxy_request, "127.0.0.1", int(port))
                            logger.info("agent-login: proxy on localhost:%s (passthrough) for %s", port, agent)
                        except OSError as e:
                            logger.warning("agent-login: proxy bind failed on %s: %s", port, e)
                    url_rewritten = True
                    await websocket.send_json({"type": "output", "data": output_buffer})
                    output_buffer = ""
                    continue
            await websocket.send_json({"type": "output", "data": text})

    async def relay_input():
        try:
            while True:
                msg = await websocket.receive_json()
                if msg.get("type") == "input" and process.stdin and not process.stdin.is_closing():
                    text = msg["data"].strip()
                    # Fallback: user pastes redirect URL or code#state manually
                    if agent == "claude" and captured_port and ("code=" in text or ("#" in text and len(text) > 20)):
                        if "code=" in text:
                            parsed = urlparse(text if text.startswith("http") else f"http://x/callback?{text}")
                            path_query = f"{parsed.path}?{parsed.query}" if parsed.query else parsed.path
                        else:
                            parts = text.split("#", 1)
                            path_query = f"/callback?code={quote(parts[0], safe='')}&state={quote(parts[1], safe='')}"
                        await websocket.send_json({"type": "output", "data": "Delivering callback to VM...\n"})
                        await _forward_to_vm(path_query)
                        continue
                    data = msg["data"].encode()
                    process.stdin.write(data)
                    await process.stdin.drain()
        except WebSocketDisconnect:
            pass

    output_task = asyncio.create_task(relay_output())
    input_task = asyncio.create_task(relay_input())

    try:
        await output_task
        await process.wait()
        await websocket.send_json({"type": "exit", "code": process.returncode})
    except WebSocketDisconnect:
        process.kill()
    finally:
        input_task.cancel()
        try:
            await input_task
        except asyncio.CancelledError:
            pass
        if proxy_server:
            proxy_server.close()
        if process.returncode is None:
            process.kill()
        try:
            await websocket.close()
        except Exception:
            pass


# --- Frontend ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    hostname = socket.gethostname().lower()
    is_dev = not hostname.startswith(("infra-", "claw-", "orchard-"))
    return templates.TemplateResponse("index.html", {
        "request": request,
        "api_token": settings.SECRET_KEY,
        "dev_mode": is_dev,
    })


# --- Error handlers ---

@app.exception_handler(HTTPException)
async def http_exception_handler(request, exc):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("tartvm.main:app", host=settings.HOST, port=settings.PORT, reload=True)
