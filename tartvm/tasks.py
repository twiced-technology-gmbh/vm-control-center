"""Task management for background operations."""
import asyncio
import json
import logging
import secrets
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, List, Optional, Set, Tuple

import aiohttp

from .config import settings
from .models import TaskModel, TaskStatus, TartImageModel, VMModel, VMStatus

logger = logging.getLogger(__name__)


@dataclass
class TaskManager:
    """Manages background tasks and Orchard API communication."""

    tasks: Dict[str, TaskModel] = field(default_factory=dict)
    _task_subscribers: Dict[str, Set[asyncio.Queue]] = field(default_factory=dict)
    _task_cleanup_task: Optional[asyncio.Task] = None

    inventory: Dict[str, VMModel] = field(default_factory=dict)
    inventory_last_refresh: Optional[float] = None
    _inventory_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _refresh_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _inventory_monitor_task: Optional[asyncio.Task] = None
    _os_cache: Dict[str, Optional[str]] = field(default_factory=dict)
    _inventory_subscribers: Set[asyncio.Queue] = field(default_factory=set)
    _images: List[TartImageModel] = field(default_factory=list)
    _orchard_session: Optional[aiohttp.ClientSession] = field(default=None, repr=False)

    # --- Task CRUD ---

    async def create_task(self, action: str, **kwargs) -> TaskModel:
        task = TaskModel(id=secrets.token_urlsafe(8), action=action, **kwargs)
        self.tasks[task.id] = task
        self._task_subscribers[task.id] = set()
        return task

    async def update_task(
        self,
        task_id: str,
        status: Optional[TaskStatus] = None,
        command: Optional[List[str]] = None,
        exit_code: Optional[int] = None,
        result: Optional[Dict] = None,
        error: Optional[str] = None,
        stderr: Optional[str] = None,
        log: Optional[str] = None,
    ) -> Optional[TaskModel]:
        if task_id not in self.tasks:
            return None
        task = self.tasks[task_id]
        if status:
            task.status = status
        if command is not None:
            task.command = command
        if exit_code is not None:
            task.exit_code = exit_code
        if result is not None:
            task.result = result
        if error is not None:
            task.error = error
        if stderr is not None:
            task.stderr = stderr
        if log is not None:
            task.logs.append(log)
            if len(task.logs) > settings.MAX_TASK_LOGS:
                task.logs = task.logs[-settings.MAX_TASK_LOGS:]
        task.updated_at = time.time()
        await self._notify_subscribers(task_id)
        return task

    async def get_task(self, task_id: str) -> Optional[TaskModel]:
        return self.tasks.get(task_id)

    async def subscribe_to_task(self, task_id: str) -> AsyncGenerator[TaskModel, None]:
        if task_id not in self._task_subscribers:
            self._task_subscribers[task_id] = set()
        queue: asyncio.Queue = asyncio.Queue()
        self._task_subscribers[task_id].add(queue)
        try:
            while True:
                task = await queue.get()
                if task is None:
                    break
                yield task
        finally:
            self._task_subscribers[task_id].discard(queue)

    async def _notify_subscribers(self, task_id: str) -> None:
        if task_id not in self._task_subscribers:
            return
        task = self.tasks[task_id]
        for queue in self._task_subscribers[task_id]:
            await queue.put(task)

    # --- Orchard API ---

    def _get_orchard_session(self) -> aiohttp.ClientSession:
        """Get or create a persistent aiohttp session for Orchard API calls."""
        if self._orchard_session is None or self._orchard_session.closed:
            if not settings.ORCHARD_URL or not settings.ORCHARD_USER or not settings.ORCHARD_TOKEN:
                raise RuntimeError("Orchard not configured (ORCHARD_URL, ORCHARD_USER, ORCHARD_TOKEN)")
            auth = aiohttp.BasicAuth(settings.ORCHARD_USER, settings.ORCHARD_TOKEN)
            self._orchard_session = aiohttp.ClientSession(auth=auth)
        return self._orchard_session

    async def close(self) -> None:
        """Close persistent connections. Call on app shutdown."""
        if self._orchard_session and not self._orchard_session.closed:
            await self._orchard_session.close()

    async def _orchard_request(
        self,
        method: str,
        path: str,
        json_body: Any = None,
        timeout: Optional[float] = None,
    ) -> Tuple[int, Any]:
        """Make an authenticated request to the Orchard API. Returns (status_code, response_body)."""
        session = self._get_orchard_session()
        t = aiohttp.ClientTimeout(total=timeout or settings.ORCHARD_TIMEOUT)
        url = f"{settings.ORCHARD_URL}{path}"
        async with session.request(method, url, json=json_body, timeout=t) as resp:
            try:
                body = await resp.json()
            except Exception:
                body = await resp.text()
            return resp.status, body

    # --- Inventory (from Orchard) ---

    async def get_inventory(self) -> List[VMModel]:
        async with self._inventory_lock:
            return [self.inventory[name] for name in sorted(self.inventory.keys())]

    async def refresh_inventory(self, task_id: Optional[str] = None) -> List[VMModel]:
        async with self._refresh_lock:
            vms = await self._inventory_from_orchard(task_id=task_id)
            async with self._inventory_lock:
                self.inventory = {vm.name: vm for vm in vms}
                self.inventory_last_refresh = time.time()
            await self._refresh_images()
            await self._notify_inventory_subscribers()
            return vms

    async def refresh_inventory_best_effort(self, task_id: Optional[str] = None) -> None:
        try:
            await self.refresh_inventory(task_id=task_id)
        except Exception:
            pass

    def subscribe_inventory(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._inventory_subscribers.add(q)
        return q

    def unsubscribe_inventory(self, q: asyncio.Queue) -> None:
        self._inventory_subscribers.discard(q)

    async def _notify_inventory_subscribers(self) -> None:
        for q in list(self._inventory_subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(True)
            except asyncio.QueueFull:
                pass

    def _detect_os(self, image: str) -> str:
        """Detect OS from Orchard image name. Falls back to tart get for local images."""
        image_lower = image.lower()
        if "macos" in image_lower or "darwin" in image_lower:
            return "macOS"
        if "ubuntu" in image_lower or "linux" in image_lower or "debian" in image_lower:
            return "Linux"
        if not image:
            return "Linux"
        # Local tart image — query tart get (cached)
        if image in self._os_cache:
            return self._os_cache[image]
        try:
            result = subprocess.run(
                [settings.TART_PATH, "get", image, "--format", "json"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                tart_os = data.get("OS", "").lower()
                if tart_os == "darwin":
                    self._os_cache[image] = "macOS"
                else:
                    self._os_cache[image] = "Linux"
            else:
                # VM not on local tart (e.g. remote worker) — default to Linux
                self._os_cache[image] = "Linux"
        except Exception:
            self._os_cache[image] = "Linux"
        return self._os_cache[image]

    async def _inventory_from_orchard(self, task_id: Optional[str] = None) -> List[VMModel]:
        status_code, body = await self._orchard_request("GET", "/v1/vms")
        if status_code != 200:
            raise RuntimeError(f"Orchard /v1/vms returned {status_code}: {body}")

        vms: List[VMModel] = []
        for vm_data in body:
            status_str = (vm_data.get("status") or vm_data.get("Status") or "unknown").lower()
            status_map = {
                "running": VMStatus.RUNNING,
                "stopped": VMStatus.STOPPED,
                "creating": VMStatus.CREATING,
                "failed": VMStatus.FAILED,
            }
            image = vm_data.get("image") or vm_data.get("Image") or ""
            os_type = self._detect_os(image)

            vms.append(VMModel(
                name=vm_data.get("name") or vm_data.get("Name", ""),
                status=status_map.get(status_str, VMStatus.UNKNOWN),
                os=os_type,
                worker=vm_data.get("worker") or vm_data.get("assignedWorker") or vm_data.get("Worker"),
                image=image or None,
                cpu=vm_data.get("cpu") or vm_data.get("CPU"),
                memory=vm_data.get("memory") or vm_data.get("Memory"),
                disk_size=vm_data.get("disk_size") or vm_data.get("Disk"),
                restart_policy=vm_data.get("restartPolicy") or vm_data.get("restart_policy"),
                created_at=vm_data.get("createdAt") or vm_data.get("created_at"),
            ))
        return vms

    # --- Tart Images ---

    async def get_images(self) -> List[TartImageModel]:
        return list(self._images)

    async def _refresh_images(self) -> None:
        try:
            self._images = await self._list_tart_images()
        except Exception:
            logger.warning("Failed to refresh Tart images", exc_info=True)

    async def _list_tart_images(self) -> List[TartImageModel]:
        proc = await asyncio.create_subprocess_exec(
            settings.TART_PATH, "list", "--format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            return []

        raw = json.loads(stdout)

        # Build image→VM mapping from current inventory
        image_to_vms: Dict[str, List[str]] = {}
        async with self._inventory_lock:
            for vm in self.inventory.values():
                if vm.image:
                    image_to_vms.setdefault(vm.image, []).append(vm.name)

        images: List[TartImageModel] = []
        for item in raw:
            name = item.get("Name", "")
            source = item.get("Source", "local")

            if name.startswith("orchard-"):
                continue
            if "@sha256:" in name:
                continue

            images.append(TartImageModel(
                name=name,
                source=source,
                disk_size=item.get("Disk"),
                size=item.get("Size"),
                os=self._detect_os(name),
                last_accessed=item.get("Accessed"),
                used_by=image_to_vms.get(name, []),
            ))

        return images

    # --- VM Operations (via Orchard) ---

    async def create_vm(
        self,
        name: str,
        image: str,
        cpu: int = 4,
        memory: int = 8192,
        disk_size: Optional[int] = None,
        startup_script: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> dict:
        if task_id:
            await self.update_task(task_id, log=f"Creating VM '{name}' from image {image}")

        body: Dict[str, Any] = {
            "name": name,
            "image": image,
            "cpu": cpu,
            "memory": memory,
            "headless": True,
        }
        if disk_size:
            body["disk_size"] = disk_size
        if startup_script:
            body["startup_script"] = startup_script

        status_code, resp = await self._orchard_request(
            "POST", "/v1/vms", json_body=body, timeout=600,
        )

        if status_code not in (200, 201):
            msg = resp.get("message", resp) if isinstance(resp, dict) else str(resp)
            raise RuntimeError(f"Failed to create VM: {msg}")

        if task_id:
            await self.update_task(task_id, log=f"VM '{name}' created successfully")

        await self.refresh_inventory_best_effort()
        return resp

    async def delete_vm(self, name: str, task_id: Optional[str] = None) -> dict:
        if task_id:
            await self.update_task(task_id, log=f"Destroying VM '{name}'")

        status_code, resp = await self._orchard_request("DELETE", f"/v1/vms/{name}")

        if status_code not in (200, 204):
            msg = resp.get("message", resp) if isinstance(resp, dict) else str(resp)
            raise RuntimeError(f"Failed to destroy VM: {msg}")

        if task_id:
            await self.update_task(task_id, log=f"VM '{name}' destroyed")

        await self.refresh_inventory_best_effort()
        return resp if isinstance(resp, dict) else {}

    async def get_vm(self, name: str) -> dict:
        status_code, body = await self._orchard_request("GET", f"/v1/vms/{name}")
        if status_code != 200:
            raise RuntimeError(f"VM '{name}' not found")
        return body

    async def get_workers(self) -> list:
        """Get all Orchard workers. Returns list of worker dicts."""
        status_code, body = await self._orchard_request("GET", "/v1/workers")
        if status_code != 200:
            return []
        return body

    async def orchard_create_vm(self, body: dict, timeout: float = 600) -> Tuple[int, Any]:
        """Create a VM via the Orchard API. Returns (status_code, response_body)."""
        return await self._orchard_request("POST", "/v1/vms", json_body=body, timeout=timeout)

    # --- VM Resolution ---

    async def resolve_tart_name(self, vm_name: str) -> tuple:
        """Resolve Orchard VM name to actual Tart clone name and worker.
        Returns (tart_name, worker_name).
        """
        status_code, body = await self._orchard_request("GET", f"/v1/vms/{vm_name}")
        if status_code != 200:
            raise RuntimeError(f"VM '{vm_name}' not found in Orchard")
        tart_name = body.get("tartName")
        worker = body.get("worker")
        if not tart_name:
            raise RuntimeError(f"VM '{vm_name}' has no tartName (not yet scheduled?)")
        return tart_name, worker

    # --- Monitoring ---

    def start_inventory_monitoring(self, interval_seconds: float = 10.0) -> None:
        if self._inventory_monitor_task and not self._inventory_monitor_task.done():
            return
        self._inventory_monitor_task = asyncio.create_task(self._inventory_monitor_loop(interval_seconds))

    async def stop_inventory_monitoring(self) -> None:
        task = self._inventory_monitor_task
        self._inventory_monitor_task = None
        if not task:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _inventory_monitor_loop(self, interval_seconds: float) -> None:
        while True:
            try:
                await self.refresh_inventory_best_effort()
            except Exception:
                pass
            await asyncio.sleep(interval_seconds)

    # --- Task cleanup ---

    def start_task_cleanup(self, interval_seconds: float = 300.0, ttl_seconds: float = 3600.0) -> None:
        if self._task_cleanup_task and not self._task_cleanup_task.done():
            return
        self._task_cleanup_task = asyncio.create_task(
            self._task_cleanup_loop(interval_seconds, ttl_seconds)
        )

    async def stop_task_cleanup(self) -> None:
        task = self._task_cleanup_task
        self._task_cleanup_task = None
        if not task:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _task_cleanup_loop(self, interval_seconds: float, ttl_seconds: float) -> None:
        while True:
            try:
                await self._cleanup_old_tasks(ttl_seconds)
            except Exception:
                logger.exception("Error during task cleanup")
            await asyncio.sleep(interval_seconds)

    async def _cleanup_old_tasks(self, ttl_seconds: float) -> None:
        now = time.time()
        tasks_to_remove = [
            tid for tid, t in self.tasks.items()
            if t.status in (TaskStatus.COMPLETED, TaskStatus.FAILED)
            and (now - t.updated_at) > ttl_seconds
        ]
        for task_id in tasks_to_remove:
            self.tasks.pop(task_id, None)
            self._task_subscribers.pop(task_id, None)
        if tasks_to_remove:
            logger.info(f"Cleaned up {len(tasks_to_remove)} old tasks")


# Global task manager instance
task_manager = TaskManager()
