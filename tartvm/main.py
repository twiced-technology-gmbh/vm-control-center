"""Main FastAPI application for VM Control Center."""
import asyncio
import json
import logging
import os
import re
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import quote, urlparse

import httpx
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


# Set up logging
logging.basicConfig(level=logging.INFO)
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

    if _pipeline_client and not _pipeline_client.is_closed:
        await _pipeline_client.aclose()

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
    if not x_local_token or x_local_token != settings.SECRET_KEY:
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
    if not token or token != settings.SECRET_KEY:
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
    vms_data = [vm.dict() for vm in vms]

    images = await task_manager.get_images()
    images_data = [img.dict() for img in images]

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
    import time
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


_VM_CONFIGS_DIR = Path.home() / ".vm-control-center" / "vm-configs"


def _save_vm_config(vm_name: str, config: dict) -> None:
    """Persist VM config to disk so Start can recreate after Stop."""
    _VM_CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    (_VM_CONFIGS_DIR / f"{vm_name}.json").write_text(json.dumps(config, indent=2))


def _load_vm_config(vm_name: str) -> dict | None:
    """Load a previously saved VM config from disk."""
    path = _VM_CONFIGS_DIR / f"{vm_name}.json"
    if path.exists():
        return json.loads(path.read_text())
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
    if not token or token != settings.SECRET_KEY:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    task = await task_manager.get_task(task_id)
    if not task:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    await websocket.send_json(task.dict())

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
                await websocket.send_json(update.dict())
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
        "login": "gh auth login --web -p https",
        "test": (
            'gh --version && echo "---" && '
            '(gh auth status 2>&1 || echo "Not logged in")'
        ),
    },
}


@app.websocket("/ws/agent-login/{vm_name}")
async def websocket_agent_login(websocket: WebSocket, vm_name: str):
    """Bidirectional WebSocket for interactive agent login inside a VM."""
    token = websocket.query_params.get("token")
    if not token or token != settings.SECRET_KEY:
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

    local_hostname = socket.gethostname().lower()
    is_local = worker and worker.lower() == local_hostname

    if is_local:
        cmd = [settings.TART_PATH, "exec", "-i", tart_name, "bash", "-c", cmd_str]
    else:
        # Single-quote the command to preserve inner double quotes and shell metacharacters
        escaped_cmd = cmd_str.replace("'", "'\\''")
        inner = f"/opt/homebrew/bin/tart exec -i {tart_name} bash -c '{escaped_cmd}'"
        cmd = ["ssh", "-i", str(Path.home() / ".ssh" / "ddMini"),
               "-o", "StrictHostKeyChecking=no",
               f"max@{worker}", inner]

    async def _find_agent_port_raw(tn, wk, loc):
        """List all listening ports inside the VM (works on macOS and Linux)."""
        port_cmd = (
            "(lsof -iTCP -sTCP:LISTEN -nP 2>/dev/null | grep -oE ':[0-9]+' | tr -d ':') || "
            "(ss -tlnp 2>/dev/null | grep -oP '(?<=:)\\d+')"
        )
        if loc:
            fp = [settings.TART_PATH, "exec", tn, "bash", "-c", port_cmd]
        else:
            fp = ["ssh", "-i", str(Path.home() / ".ssh" / "ddMini"),
                  "-o", "StrictHostKeyChecking=no", f"max@{wk}",
                  f"/opt/homebrew/bin/tart exec {tn} bash -c \"{port_cmd}\""]
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
            fwd = ["ssh", "-i", str(Path.home() / ".ssh" / "ddMini"),
                   "-o", "StrictHostKeyChecking=no", f"max@{worker}",
                   f"/opt/homebrew/bin/tart exec {tart_name} bash -c \"{curl_cmd}\""]
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


# --- Pipeline proxy ---

_pipeline_client: Optional[httpx.AsyncClient] = None


def _get_pipeline_client() -> httpx.AsyncClient:
    global _pipeline_client
    if _pipeline_client is None or _pipeline_client.is_closed:
        _pipeline_client = httpx.AsyncClient(
            base_url=settings.PIPELINE_CONTROLLER_URL,
            timeout=10.0,
        )
    return _pipeline_client


async def _pipeline_proxy(method: str, path: str, unavailable_fallback=None) -> JSONResponse:
    """Proxy a request to the pipeline controller with standard error handling."""
    if unavailable_fallback is None:
        unavailable_fallback = {"detail": "Pipeline controller unavailable"}
    try:
        resp = await _get_pipeline_client().request(method, path)
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except httpx.ConnectError:
        return JSONResponse(content=unavailable_fallback, status_code=200)
    except Exception as e:
        logger.warning(f"Pipeline proxy error: {e}")
        return JSONResponse(content={"detail": str(e)}, status_code=502)


@app.get("/api/pipeline/runs", dependencies=[Depends(verify_token)])
async def pipeline_runs():
    return await _pipeline_proxy("GET", "/api/runs", unavailable_fallback=[])


@app.get("/api/pipeline/runs/{run_id}", dependencies=[Depends(verify_token)])
async def pipeline_run_detail(run_id: str):
    return await _pipeline_proxy("GET", f"/api/runs/{run_id}")


@app.post("/api/pipeline/runs/{run_id}/retry", dependencies=[Depends(verify_token)])
async def pipeline_retry_run(run_id: str):
    return await _pipeline_proxy("POST", f"/api/runs/{run_id}/retry")


@app.get("/api/pipeline/health", dependencies=[Depends(verify_token)])
async def pipeline_health():
    return await _pipeline_proxy("GET", "/api/health", unavailable_fallback={"status": "unreachable"})


# --- Frontend ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "api_token": settings.SECRET_KEY,
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
