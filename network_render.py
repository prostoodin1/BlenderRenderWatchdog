"""Token-authenticated LAN render coordinator and Blender worker."""

from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from frame_validation import validate_frame


MAX_WORKERS = 5
IMAGE_EXTENSIONS = {".bmp", ".exr", ".hdr", ".jpeg", ".jpg", ".png", ".tga", ".tif", ".tiff", ".webp"}


def lan_address() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def prepare_network_project(
    blender: Path,
    blend: Path,
    destination: Path,
    log: Callable[[str], None] | None = None,
) -> Path:
    """Create a packed copy so remote workers receive textures and linked assets."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    script_path = Path(tempfile.gettempdir()) / f"watchdog_pack_{uuid.uuid4().hex}.py"
    script_path.write_text(
        "import bpy, sys\n"
        "target = sys.argv[sys.argv.index('--') + 1]\n"
        "try:\n"
        "    bpy.ops.file.pack_all()\n"
        "except Exception as error:\n"
        "    print('[WATCHDOG] Pack warning:', error)\n"
        "bpy.ops.wm.save_as_mainfile(filepath=target, copy=True)\n",
        encoding="utf-8",
    )
    try:
        completed = subprocess.run(
            [str(blender), "-b", str(blend), "--python", str(script_path), "--", str(destination)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
        if completed.returncode != 0 or not destination.exists():
            raise RuntimeError((completed.stdout + completed.stderr)[-2000:] or "Could not create packed network project")
        if log:
            log(f"[NETWORK] Packed project copy: {destination}")
        return destination
    finally:
        try:
            script_path.unlink()
        except OSError:
            pass


@dataclass(slots=True)
class PairingCode:
    host: str
    port: int
    token: str

    def encode(self) -> str:
        payload = json.dumps({"h": self.host, "p": self.port, "t": self.token}, separators=(",", ":")).encode("utf-8")
        return "BRW2-" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, value: str) -> "PairingCode":
        value = value.strip()
        if not value.startswith("BRW2-"):
            raise ValueError("Invalid Blender Render Watchdog connection code")
        encoded = value[5:]
        encoded += "=" * (-len(encoded) % 4)
        try:
            data = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
            host = str(data["h"])
            port = int(data["p"])
            token = str(data["t"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Invalid Blender Render Watchdog connection code") from error
        if not host or not token or not 1 <= port <= 65535:
            raise ValueError("Invalid Blender Render Watchdog connection code")
        return cls(host, port, token)


@dataclass(slots=True)
class WorkerState:
    worker_id: str
    name: str
    hardware: str = ""
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    completed_frames: int = 0
    failed_frames: int = 0
    current_frame: int | None = None
    average_seconds: float = 0.0
    frame_start: int | None = None
    frame_end: int | None = None
    samples: int | None = None

    def public_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["online"] = time.time() - self.last_seen < 30
        return data


@dataclass(slots=True)
class FrameTask:
    frame: int
    status: str = "pending"
    worker_id: str = ""
    attempts: int = 0
    started_at: float = 0.0
    duration_seconds: float = 0.0
    error: str = ""


class NetworkRenderPlan:
    def __init__(
        self,
        blend_path: Path,
        output_folder: Path,
        start_frame: int,
        end_frame: int,
        completed_frames: set[int] | None = None,
    ) -> None:
        if start_frame > end_frame:
            raise ValueError("start_frame cannot be greater than end_frame")
        self.plan_id = uuid.uuid4().hex
        self.blend_path = blend_path
        self.output_folder = output_folder
        self.start_frame = start_frame
        self.end_frame = end_frame
        self.tasks = {frame: FrameTask(frame) for frame in range(start_frame, end_frame + 1)}
        for frame in completed_frames or set():
            if frame in self.tasks:
                self.tasks[frame].status = "completed"
        self.paused = False
        self.stopped = False
        self.created_at = time.time()
        self.integrity_retries = 0
        self.last_corrupt_frames: list[int] = []
        self.integrity_audited = False
        self._lock = threading.RLock()

    def claim(self, worker: WorkerState, reserved_ranges: list[tuple[int, int]] | None = None) -> FrameTask | None:
        with self._lock:
            if self.paused or self.stopped:
                return None
            minimum = worker.frame_start if worker.frame_start is not None else self.start_frame
            maximum = worker.frame_end if worker.frame_end is not None else self.end_frame
            candidates = [
                task for task in self.tasks.values()
                if task.status == "pending" and minimum <= task.frame <= maximum
            ]
            if worker.frame_start is None and worker.frame_end is None and reserved_ranges:
                candidates = [
                    task for task in candidates
                    if not any(start <= task.frame <= end for start, end in reserved_ranges)
                ]
            if not candidates:
                return None
            task = min(candidates, key=lambda item: (item.attempts, item.frame))
            task.status = "running"
            task.worker_id = worker.worker_id
            task.attempts += 1
            task.started_at = time.monotonic()
            worker.current_frame = task.frame
            worker.last_seen = time.time()
            return task

    def stop(self) -> None:
        with self._lock:
            self.stopped = True
            for task in self.tasks.values():
                if task.status == "pending":
                    task.status = "failed"

    def complete(self, worker: WorkerState, frame: int, success: bool, error: str = "") -> FrameTask:
        with self._lock:
            task = self.tasks[frame]
            task.duration_seconds = max(0.0, time.monotonic() - task.started_at) if task.started_at else 0.0
            task.error = error
            worker.last_seen = time.time()
            worker.current_frame = None
            if success:
                task.status = "completed"
                worker.completed_frames += 1
                count = worker.completed_frames
                worker.average_seconds = ((worker.average_seconds * (count - 1)) + task.duration_seconds) / count
            else:
                worker.failed_frames += 1
                task.status = "pending" if task.attempts < 3 and not self.stopped else "failed"
                task.worker_id = ""
            return task

    def release_stale(self, workers: dict[str, WorkerState], stale_seconds: float = 90.0) -> list[int]:
        now = time.time()
        released: list[int] = []
        with self._lock:
            for task in self.tasks.values():
                worker = workers.get(task.worker_id)
                if task.status == "running" and (worker is None or now - worker.last_seen > stale_seconds):
                    task.status = "pending"
                    task.worker_id = ""
                    released.append(task.frame)
            return released

    def release_worker(self, worker_id: str) -> list[int]:
        """Immediately return a disconnected worker's active frames to the queue."""
        released: list[int] = []
        with self._lock:
            for task in self.tasks.values():
                if task.status == "running" and task.worker_id == worker_id:
                    task.status = "pending"
                    task.worker_id = ""
                    task.error = "Worker disconnected by controller"
                    released.append(task.frame)
        return released

    def summary(self) -> dict[str, object]:
        with self._lock:
            counts = {status: 0 for status in ("pending", "running", "completed", "failed")}
            for task in self.tasks.values():
                counts[task.status] = counts.get(task.status, 0) + 1
            total = len(self.tasks)
            counts.update(
                {
                    "plan_id": self.plan_id,
                    "start_frame": self.start_frame,
                    "end_frame": self.end_frame,
                    "total": total,
                    "progress": (counts["completed"] / total * 100.0) if total else 100.0,
                    "paused": self.paused,
                    "stopped": self.stopped,
                    "finished": counts["completed"] + counts["failed"] == total,
                    "integrity_retries": self.integrity_retries,
                    "corrupt_frames": list(self.last_corrupt_frames),
                    "integrity_audited": self.integrity_audited,
                }
            )
            return counts


class RenderCoordinator:
    def __init__(
        self,
        bind_host: str = "0.0.0.0",
        port: int = 0,
        advertised_host: str | None = None,
        token: str | None = None,
        controller_name: str | None = None,
        controller_hardware: str = "",
        on_event: Callable[[str], None] | None = None,
        on_frame: Callable[[int, Path, float], None] | None = None,
    ) -> None:
        self.bind_host = bind_host
        self.port = port
        self.advertised_host = advertised_host or lan_address()
        self.token = token or secrets.token_urlsafe(18)
        self.controller_name = controller_name or socket.gethostname()
        self.controller_hardware = controller_hardware
        self.on_event = on_event
        self.on_frame = on_frame
        self.workers: dict[str, WorkerState] = {}
        self.plan: NetworkRenderPlan | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._integrity_lock = threading.RLock()

    @property
    def pairing_code(self) -> str:
        if not self.port:
            raise RuntimeError("Coordinator has not started")
        return PairingCode(self.advertised_host, self.port, self.token).encode()

    def event(self, message: str) -> None:
        if self.on_event:
            self.on_event(message)

    def start(self) -> str:
        coordinator = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "BlenderRenderWatchdog/2.4"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _authorized(self) -> bool:
                query_token = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("token", [""])[0]
                return self.headers.get("X-Watchdog-Token", "") == coordinator.token or query_token == coordinator.token

            def _json_body(self) -> dict[str, object]:
                length = min(int(self.headers.get("Content-Length", "0") or 0), 1_500_000_000)
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8"))
                return data if isinstance(data, dict) else {}

            def _send_json(self, data: dict[str, object], status: int = 200) -> None:
                payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _reject(self, status: int = 401, message: str = "Unauthorized") -> None:
                self._send_json({"ok": False, "error": message}, status)

            def do_GET(self) -> None:  # noqa: N802
                if not self._authorized():
                    self._reject()
                    return
                route = urllib.parse.urlparse(self.path).path
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                if route == "/api/status":
                    self._send_json(coordinator.status())
                    return
                if route == "/api/project":
                    plan = coordinator.plan
                    if plan is None or not plan.blend_path.exists():
                        self._reject(404, "No active project")
                        return
                    payload = plan.blend_path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("X-Project-Id", plan.plan_id)
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if route == "/api/task":
                    worker_id = query.get("worker_id", [""])[0]
                    self._send_json(coordinator.claim_task(worker_id))
                    return
                self._reject(404, "Not found")

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    self._reject()
                    return
                route = urllib.parse.urlparse(self.path).path
                try:
                    data = self._json_body()
                    if route == "/api/join":
                        result, status = coordinator.join(str(data.get("name") or "Worker"), str(data.get("hardware") or ""))
                        self._send_json(result, status)
                    elif route == "/api/heartbeat":
                        self._send_json(coordinator.heartbeat(str(data.get("worker_id") or "")))
                    elif route == "/api/result":
                        self._send_json(coordinator.accept_result(data))
                    else:
                        self._reject(404, "Not found")
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    self._reject(400, str(error))

        self._server = ThreadingHTTPServer((self.bind_host, self.port), Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, name="render-coordinator", daemon=True)
        self._thread.start()
        self.event(f"[NETWORK] Controller listening on {self.advertised_host}:{self.port}")
        return self.pairing_code

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=3)
        self._server = None
        self._thread = None

    def start_plan(
        self,
        blend_path: Path,
        output_folder: Path,
        start_frame: int,
        end_frame: int,
        completed_frames: set[int] | None = None,
    ) -> NetworkRenderPlan:
        output_folder.mkdir(parents=True, exist_ok=True)
        self.plan = NetworkRenderPlan(blend_path, output_folder, start_frame, end_frame, completed_frames)
        self.event(f"[NETWORK] Distributed render started: {start_frame}-{end_frame}")
        return self.plan

    def join(self, name: str, hardware: str) -> tuple[dict[str, object], int]:
        with self._lock:
            online = [worker for worker in self.workers.values() if time.time() - worker.last_seen < 30]
            if len(online) >= MAX_WORKERS:
                return {"ok": False, "error": f"Maximum {MAX_WORKERS} workers reached"}, 409
            worker = WorkerState(uuid.uuid4().hex, name[:80] or "Worker", hardware[:200])
            self.workers[worker.worker_id] = worker
        self.event(f"[NETWORK] Connected: {worker.name} ({worker.hardware or 'unknown hardware'})")
        return {"ok": True, "worker_id": worker.worker_id, "max_workers": MAX_WORKERS}, 200

    def heartbeat(self, worker_id: str) -> dict[str, object]:
        worker = self.workers.get(worker_id)
        if worker is None:
            return {"ok": False, "state": "disconnected", "error": "Disconnected by controller"}
        worker.last_seen = time.time()
        if self.plan:
            self.plan.release_stale(self.workers)
        return {"ok": True}

    def claim_task(self, worker_id: str) -> dict[str, object]:
        worker = self.workers.get(worker_id)
        if worker is None:
            return {"ok": False, "state": "disconnected", "error": "Disconnected by controller"}
        worker.last_seen = time.time()
        plan = self.plan
        if plan is None:
            return {"ok": True, "state": "idle"}
        plan.release_stale(self.workers)
        summary = plan.summary()
        if summary["finished"] and int(summary["failed"]) == 0 and not plan.integrity_audited:
            self.audit_completed_outputs(plan)
            summary = plan.summary()
        if summary["finished"]:
            return {"ok": True, "state": "finished", "summary": summary}
        if plan.paused:
            return {"ok": True, "state": "paused", "summary": summary}
        reserved_ranges = [
            (
                other.frame_start if other.frame_start is not None else plan.start_frame,
                other.frame_end if other.frame_end is not None else plan.end_frame,
            )
            for other in self.workers.values()
            if (
                other.worker_id != worker_id
                and time.time() - other.last_seen < 30
                and (other.frame_start is not None or other.frame_end is not None)
            )
        ]
        task = plan.claim(worker, reserved_ranges)
        if task is None:
            return {"ok": True, "state": "waiting", "summary": summary}
        return {
            "ok": True,
            "state": "task",
            "frame": task.frame,
            "plan_id": plan.plan_id,
            "project_name": plan.blend_path.name,
            "samples": worker.samples,
        }

    def accept_result(self, data: dict[str, object]) -> dict[str, object]:
        worker_id = str(data.get("worker_id") or "")
        worker = self.workers.get(worker_id)
        plan = self.plan
        if worker is None or plan is None:
            return {"ok": False, "error": "Unknown worker or inactive plan"}
        frame = int(data.get("frame") or 0)
        if frame not in plan.tasks or plan.tasks[frame].worker_id != worker_id:
            return {"ok": False, "error": "Frame was not assigned to this worker"}
        success = bool(data.get("success"))
        output_path: Path | None = None
        if success:
            encoded = str(data.get("file_base64") or "")
            extension = str(data.get("extension") or ".png").lower()
            if extension not in IMAGE_EXTENSIONS:
                extension = ".png"
            payload = base64.b64decode(encoded, validate=True)
            output_path = plan.output_folder / f"frame_{frame:04d}{extension}"
            temporary = output_path.with_suffix(output_path.suffix + ".part")
            temporary.write_bytes(payload)
            temporary.replace(output_path)
        task = plan.complete(worker, frame, success, str(data.get("error") or ""))
        if success and output_path:
            self.event(f"[NETWORK] Frame {frame} received from {worker.name}")
            if self.on_frame:
                self.on_frame(frame, output_path, task.duration_seconds)
        else:
            self.event(f"[NETWORK] Frame {frame} failed on {worker.name}; retry scheduled")
        summary = plan.summary()
        if summary["finished"] and int(summary["failed"]) == 0 and not plan.integrity_audited:
            self.audit_completed_outputs(plan)
        return {"ok": True, "state": task.status, "summary": plan.summary()}

    def audit_completed_outputs(self, plan: NetworkRenderPlan) -> list[int]:
        """Verify every final frame and requeue corrupt outputs for another worker."""
        with self._integrity_lock:
            if self.plan is not plan:
                return []
            corrupt: list[int] = []
            for frame, task in plan.tasks.items():
                if task.status != "completed":
                    continue
                candidates = sorted(
                    (
                        path for path in plan.output_folder.glob(f"frame_{frame:04d}.*")
                        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
                    ),
                    key=lambda path: path.stat().st_mtime,
                    reverse=True,
                )
                output = candidates[0] if candidates else None
                valid, reason = validate_frame(output) if output else (False, "frame file is missing")
                if valid:
                    continue
                if output is not None:
                    quarantine = output.with_name(f"{output.name}.corrupt-{int(time.time())}-{uuid.uuid4().hex[:6]}")
                    try:
                        output.replace(quarantine)
                    except OSError:
                        pass
                task.error = f"Integrity check failed: {reason}"
                task.worker_id = ""
                task.status = "pending" if task.attempts < 3 and not plan.stopped else "failed"
                plan.integrity_retries += 1
                corrupt.append(frame)
            plan.last_corrupt_frames = corrupt
            if corrupt:
                plan.integrity_audited = False
                self.event(f"[NETWORK] Integrity check requeued corrupt frames: {', '.join(map(str, corrupt))}")
            else:
                plan.integrity_audited = True
                self.event("[NETWORK] Integrity check passed for all completed frames")
            return corrupt

    def set_worker_settings(
        self,
        worker_id: str,
        start_frame: int | None,
        end_frame: int | None,
        samples: int | None,
    ) -> bool:
        worker = self.workers.get(worker_id)
        if worker is None:
            return False
        if start_frame is not None and end_frame is not None and start_frame > end_frame:
            raise ValueError("start_frame cannot be greater than end_frame")
        if samples is not None and samples < 1:
            raise ValueError("samples must be greater than 0")
        worker.frame_start = start_frame
        worker.frame_end = end_frame
        worker.samples = samples
        return True

    def set_worker_range(self, worker_id: str, start_frame: int | None, end_frame: int | None) -> bool:
        return self.set_worker_settings(worker_id, start_frame, end_frame, self.workers.get(worker_id).samples if worker_id in self.workers else None)

    def disconnect_worker(self, worker_id: str) -> bool:
        with self._lock:
            worker = self.workers.pop(worker_id, None)
            if worker is None:
                return False
            released = self.plan.release_worker(worker_id) if self.plan else []
            worker.current_frame = None
        suffix = f"; requeued frames: {', '.join(map(str, released))}" if released else ""
        self.event(f"[NETWORK] Disconnected by controller: {worker.name}{suffix}")
        return True

    def status(self) -> dict[str, object]:
        worker_rows = [worker.public_dict() for worker in self.workers.values()]
        local_worker = next((worker for worker in worker_rows if worker["name"] == self.controller_name), None)
        controller_row: dict[str, object] = {
            "worker_id": "controller",
            "name": self.controller_name,
            "hardware": self.controller_hardware,
            "online": True,
            "current_frame": None,
            "completed_frames": 0,
            "average_seconds": 0.0,
            "frame_start": None,
            "frame_end": None,
            "samples": None,
            "is_controller": True,
            "settings_worker_id": "",
        }
        if local_worker is not None:
            controller_row.update(local_worker)
            controller_row["settings_worker_id"] = local_worker["worker_id"]
            controller_row["worker_id"] = "controller"
            controller_row["is_controller"] = True
        devices = [controller_row, *(worker for worker in worker_rows if worker is not local_worker)]
        return {
            "ok": True,
            "controller": {"name": self.controller_name, "host": self.advertised_host},
            "workers": worker_rows,
            "devices": devices,
            "plan": self.plan.summary() if self.plan else None,
        }


def _request_json(url: str, token: str, data: dict[str, object] | None = None, timeout: float = 30.0) -> dict[str, object]:
    payload = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=payload, headers={"X-Watchdog-Token": token})
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result if isinstance(result, dict) else {}


class NetworkWorker:
    def __init__(
        self,
        code: str,
        blender: Path,
        name: str | None = None,
        hardware: str = "",
        cache_folder: Path | None = None,
        on_event: Callable[[str], None] | None = None,
        render_frame: Callable[[int, Path], tuple[bool, Path | None, str]] | None = None,
    ) -> None:
        self.connection = PairingCode.decode(code)
        self.blender = blender
        self.name = name or socket.gethostname()
        self.hardware = hardware
        self.cache_folder = cache_folder or Path(tempfile.gettempdir()) / "BlenderRenderWatchdogWorker"
        self.on_event = on_event
        self.render_frame = render_frame
        self.worker_id = ""
        self.stop_event = threading.Event()
        self._project_id = ""
        self._project_path: Path | None = None
        self.status_snapshot: dict[str, object] = {}

    @property
    def base_url(self) -> str:
        return f"http://{self.connection.host}:{self.connection.port}"

    def event(self, message: str) -> None:
        if self.on_event:
            self.on_event(message)

    def join(self) -> str:
        result = _request_json(
            self.base_url + "/api/join",
            self.connection.token,
            {"name": self.name, "hardware": self.hardware},
        )
        if not result.get("ok"):
            raise ConnectionError(str(result.get("error") or "Connection refused"))
        self.worker_id = str(result["worker_id"])
        self.event(f"[NETWORK] Connected to {self.connection.host}:{self.connection.port}")
        self.refresh_status()
        return self.worker_id

    def refresh_status(self) -> dict[str, object]:
        self.status_snapshot = _request_json(
            self.base_url + "/api/status",
            self.connection.token,
            timeout=15,
        )
        return dict(self.status_snapshot)

    def _download_project(self, plan_id: str, project_name: str) -> Path:
        self.cache_folder.mkdir(parents=True, exist_ok=True)
        safe_name = Path(project_name).name or "network_project.blend"
        destination = self.cache_folder / f"{plan_id}_{safe_name}"
        request = urllib.request.Request(self.base_url + "/api/project", headers={"X-Watchdog-Token": self.connection.token})
        with urllib.request.urlopen(request, timeout=300) as response:
            temporary = destination.with_suffix(destination.suffix + ".part")
            with temporary.open("wb") as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    stream.write(chunk)
            temporary.replace(destination)
        self._project_id = plan_id
        self._project_path = destination
        return destination

    def _render_frame(self, frame: int, project: Path, samples: int | None = None) -> tuple[bool, Path | None, str]:
        frame_folder = self.cache_folder / "frames" / uuid.uuid4().hex
        frame_folder.mkdir(parents=True, exist_ok=True)
        command = [str(self.blender), "-b", str(project), "-o", str(frame_folder / "frame_####")]
        if samples is not None:
            command.extend(["--python-expr", f"import bpy; bpy.context.scene.cycles.samples={samples}"])
        command.extend(["-f", str(frame)])
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
        candidates = [path for path in frame_folder.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS]
        output = max(candidates, key=lambda path: path.stat().st_mtime, default=None)
        error = (completed.stdout + completed.stderr)[-2000:]
        return completed.returncode == 0 and output is not None, output, error

    def run(self, poll_seconds: float = 2.0, stay_connected: bool = False) -> None:
        if not self.blender.exists():
            raise FileNotFoundError(f"Blender not found: {self.blender}")
        if not self.worker_id:
            self.join()
        while not self.stop_event.is_set():
            try:
                query = urllib.parse.urlencode({"worker_id": self.worker_id})
                task = _request_json(self.base_url + f"/api/task?{query}", self.connection.token, timeout=45)
                state = str(task.get("state") or "")
                if not task.get("ok") and state == "disconnected":
                    self.event("[NETWORK] Disconnected by the main computer")
                    return
                if state == "task":
                    plan_id = str(task["plan_id"])
                    if self._project_id != plan_id or self._project_path is None or not self._project_path.exists():
                        self.event("[NETWORK] Downloading project...")
                        self._download_project(plan_id, str(task.get("project_name") or "project.blend"))
                    frame = int(task["frame"])
                    samples = int(task["samples"]) if task.get("samples") is not None else None
                    self.event(f"[NETWORK] Rendering frame {frame}")
                    if self.render_frame:
                        success, output, error = self.render_frame(frame, self._project_path)
                    else:
                        success, output, error = self._render_frame(frame, self._project_path, samples)
                    result: dict[str, object] = {
                        "worker_id": self.worker_id,
                        "frame": frame,
                        "success": success,
                        "error": "" if success else error,
                    }
                    if success and output:
                        result["extension"] = output.suffix.lower()
                        result["file_base64"] = base64.b64encode(output.read_bytes()).decode("ascii")
                    _request_json(self.base_url + "/api/result", self.connection.token, result, timeout=600)
                elif state == "finished" and not stay_connected:
                    self.event("[NETWORK] Distributed render is complete")
                    return
                elif state == "finished":
                    self.stop_event.wait(poll_seconds)
                else:
                    self.stop_event.wait(poll_seconds)
                heartbeat = _request_json(
                    self.base_url + "/api/heartbeat",
                    self.connection.token,
                    {"worker_id": self.worker_id},
                    timeout=15,
                )
                if not heartbeat.get("ok") and heartbeat.get("state") == "disconnected":
                    self.event("[NETWORK] Disconnected by the main computer")
                    return
                self.refresh_status()
            except (OSError, urllib.error.URLError, ConnectionError, json.JSONDecodeError) as error:
                self.event(f"[NETWORK] Connection error: {error}")
                self.stop_event.wait(max(2.0, poll_seconds))

    def stop(self) -> None:
        self.stop_event.set()
