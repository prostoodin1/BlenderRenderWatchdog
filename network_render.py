"""Token-authenticated LAN render coordinator and Blender worker."""

from __future__ import annotations

import base64
import hmac
import json
import os
import re
import secrets
import shutil
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

from device_groups import DeviceCapabilities
from frame_validation import validate_frame
from process_utils import hidden_subprocess_kwargs


MAX_WORKERS = 5
WORKER_OFFLINE_SECONDS = 30
WORKER_HEARTBEAT_SECONDS = 10.0
IMAGE_EXTENSIONS = {".bmp", ".exr", ".hdr", ".jpeg", ".jpg", ".png", ".tga", ".tif", ".tiff", ".webp"}
COMPUTE_BACKENDS = {"AUTO", "OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"}


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
            **hidden_subprocess_kwargs(),
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
    transport: str = "lan"
    ssh_host: str = ""
    ssh_port: int = 22
    ssh_user: str = ""
    ssh_private_key: str = field(default="", repr=False)

    def encode(self) -> str:
        payload_data: dict[str, object] = {"h": self.host, "p": self.port, "t": self.token}
        prefix = "BRW2-"
        if self.transport == "ssh":
            payload_data.update({"n": "ssh", "sh": self.ssh_host, "sp": self.ssh_port, "su": self.ssh_user})
            if self.ssh_private_key:
                payload_data["sk"] = self.ssh_private_key
            prefix = "BRW4-"
        elif self.transport != "lan":
            payload_data["n"] = self.transport
            prefix = "BRW3-"
        payload = json.dumps(payload_data, separators=(",", ":")).encode("utf-8")
        return prefix + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, value: str) -> "PairingCode":
        value = value.strip()
        if value.casefold().startswith("brw://join/"):
            value = value[len("brw://join/"):].strip()
        if not value.startswith(("BRW2-", "BRW3-", "BRW4-")):
            raise ValueError("Invalid Blender Render Watchdog connection code")
        encoded = value[5:]
        encoded += "=" * (-len(encoded) % 4)
        try:
            data = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
            host = str(data["h"])
            port = int(data["p"])
            token = str(data["t"])
            transport = str(data.get("n") or "lan").strip().lower()
            ssh_host = str(data.get("sh") or "").strip()
            ssh_port = int(data.get("sp") or 22)
            ssh_user = str(data.get("su") or "").strip()
            ssh_private_key = str(data.get("sk") or "")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("Invalid Blender Render Watchdog connection code") from error
        if not host or not token or not 1 <= port <= 65535 or transport not in {"lan", "tailscale", "ssh"}:
            raise ValueError("Invalid Blender Render Watchdog connection code")
        if transport == "ssh" and (not ssh_host or not ssh_user or not 1 <= ssh_port <= 65535):
            raise ValueError("Invalid Blender Render Watchdog SSH connection code")
        if ssh_private_key and transport != "ssh":
            raise ValueError("Invalid Blender Render Watchdog SSH invitation key")
        if ssh_private_key:
            encoded_key_size = len(ssh_private_key.encode("utf-8"))
            if (
                encoded_key_size > 16_384
                or not ssh_private_key.strip().startswith("-----BEGIN OPENSSH PRIVATE KEY-----")
                or not ssh_private_key.strip().endswith("-----END OPENSSH PRIVATE KEY-----")
            ):
                raise ValueError("Invalid Blender Render Watchdog SSH invitation key")
        return cls(host, port, token, transport, ssh_host, ssh_port, ssh_user, ssh_private_key)

    @property
    def invitation_link(self) -> str:
        return f"brw://join/{self.encode()}"

    def without_private_key(self) -> "PairingCode":
        return PairingCode(
            self.host,
            self.port,
            self.token,
            self.transport,
            self.ssh_host,
            self.ssh_port,
            self.ssh_user,
        )


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
    use_cpu: bool = True
    use_gpu: bool = True
    device_id: str = ""
    identity_fingerprint: str = ""
    capabilities: dict[str, object] = field(default_factory=dict)
    compute_backend: str = "AUTO"
    chunk_size: int | None = None
    current_frames: list[int] = field(default_factory=list)
    disabled: bool = False

    def __post_init__(self) -> None:
        self.device_id = str(self.device_id or self.worker_id).strip()
        self.identity_fingerprint = str(self.identity_fingerprint).strip()[:128]
        self.compute_backend = str(self.compute_backend).strip().upper()
        if self.compute_backend not in COMPUTE_BACKENDS:
            self.compute_backend = "AUTO"
        if self.chunk_size is not None:
            self.chunk_size = max(1, min(1000, int(self.chunk_size)))

    def public_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["online"] = not self.disabled and time.time() - self.last_seen < WORKER_OFFLINE_SECONDS
        data["render_device"] = render_device_label(self.use_cpu, self.use_gpu)
        return data


def render_device_label(use_cpu: bool, use_gpu: bool) -> str:
    if use_gpu and use_cpu:
        return "GPU + CPU"
    if use_gpu:
        return "GPU"
    return "CPU"


def worker_device_script(
    use_cpu: bool,
    use_gpu: bool,
    samples: int | None,
    compute_backend: str = "AUTO",
) -> str:
    """Build the per-frame Cycles device setup used by remote workers."""
    return f'''import bpy
USE_CPU = {bool(use_cpu)!r}
USE_GPU = {bool(use_gpu)!r}
SAMPLES = {int(samples) if samples is not None else None!r}
COMPUTE_BACKEND = {str(compute_backend).strip().upper()!r}
scene = bpy.context.scene
if scene.render.engine == "CYCLES":
    prefs = bpy.context.preferences.addons["cycles"].preferences
    selected_backend = None
    if USE_GPU:
        backend_order = (COMPUTE_BACKEND,) if COMPUTE_BACKEND != "AUTO" else ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL")
        for backend in backend_order:
            try:
                prefs.compute_device_type = backend
                prefs.get_devices()
                if any(device.type != "CPU" for device in prefs.devices):
                    selected_backend = backend
                    break
            except Exception:
                pass
    if USE_GPU and selected_backend:
        scene.cycles.device = "GPU"
        for device in prefs.devices:
            device.use = bool(device.type != "CPU" or USE_CPU)
    else:
        if USE_GPU and not USE_CPU:
            raise RuntimeError("Requested GPU backend is unavailable on this worker")
        scene.cycles.device = "CPU"
        try:
            prefs.get_devices()
            for device in prefs.devices:
                device.use = bool(device.type == "CPU")
        except Exception:
            pass
    if SAMPLES is not None:
        scene.cycles.samples = SAMPLES
'''


@dataclass(slots=True)
class FrameTask:
    frame: int
    status: str = "pending"
    worker_id: str = ""
    attempts: int = 0
    started_at: float = 0.0
    duration_seconds: float = 0.0
    error: str = ""


@dataclass(slots=True)
class FrameBatch:
    frames: list[int]
    worker_id: str

    @property
    def first_frame(self) -> int | None:
        return self.frames[0] if self.frames else None


class NetworkRenderPlan:
    def __init__(
        self,
        blend_path: Path,
        output_folder: Path,
        start_frame: int,
        end_frame: int,
        completed_frames: set[int] | None = None,
        chunk_mode: str = "adaptive",
        chunk_size: int = 10,
        project_id: str = "",
        source_fingerprint: str = "",
    ) -> None:
        if start_frame > end_frame:
            raise ValueError("start_frame cannot be greater than end_frame")
        self.plan_id = uuid.uuid4().hex
        self.project_id = str(project_id or self.plan_id)
        self.source_fingerprint = str(source_fingerprint)
        self.blend_path = blend_path
        self.output_folder = output_folder
        self.start_frame = start_frame
        self.end_frame = end_frame
        self.tasks = {frame: FrameTask(frame) for frame in range(start_frame, end_frame + 1)}
        for frame in completed_frames or set():
            if frame in self.tasks:
                self.tasks[frame].status = "completed"
        self.initial_completed = sum(task.status == "completed" for task in self.tasks.values())
        self.paused = False
        self.stopped = False
        self.created_at = time.time()
        self.chunk_mode = "fixed" if chunk_mode == "fixed" else "adaptive"
        self.chunk_size = max(1, min(1000, int(chunk_size)))
        self.integrity_retries = 0
        self.last_corrupt_frames: list[int] = []
        self.integrity_audited = False
        self._lock = threading.RLock()

    def _candidate_tasks(
        self,
        worker: WorkerState,
        reserved_ranges: list[tuple[int, int]] | None = None,
    ) -> list[FrameTask]:
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
        return sorted(candidates, key=lambda item: (item.attempts, item.frame))

    def _adaptive_chunk_size(self, worker: WorkerState) -> int:
        if worker.chunk_size is not None:
            return worker.chunk_size
        if self.chunk_mode == "fixed" or worker.average_seconds <= 0:
            return self.chunk_size
        target_seconds = 180.0
        calculated = round(target_seconds / max(0.1, worker.average_seconds))
        return max(1, min(self.chunk_size * 2, calculated))

    def claim_batch(
        self,
        worker: WorkerState,
        reserved_ranges: list[tuple[int, int]] | None = None,
        maximum: int | None = None,
    ) -> FrameBatch | None:
        with self._lock:
            if self.paused or self.stopped or worker.disabled:
                return None
            candidates = self._candidate_tasks(worker, reserved_ranges)
            if not candidates:
                return None
            target_size = max(1, int(maximum or self._adaptive_chunk_size(worker)))
            first = candidates[0]
            candidate_by_frame = {task.frame: task for task in candidates}
            selected: list[FrameTask] = []
            for frame in range(first.frame, first.frame + target_size):
                task = candidate_by_frame.get(frame)
                if task is None:
                    break
                selected.append(task)
            started_at = time.monotonic()
            for task in selected:
                task.status = "running"
                task.worker_id = worker.worker_id
                task.attempts += 1
                task.started_at = started_at
            worker.current_frames = [task.frame for task in selected]
            worker.current_frame = worker.current_frames[0] if worker.current_frames else None
            worker.last_seen = time.time()
            return FrameBatch(list(worker.current_frames), worker.worker_id)

    def claim(self, worker: WorkerState, reserved_ranges: list[tuple[int, int]] | None = None) -> FrameTask | None:
        batch = self.claim_batch(worker, reserved_ranges, maximum=1)
        return self.tasks[batch.frames[0]] if batch else None

    def stop(self) -> None:
        with self._lock:
            self.stopped = True
            for task in self.tasks.values():
                if task.status == "pending":
                    task.status = "failed"

    def complete(
        self,
        worker: WorkerState,
        frame: int,
        success: bool,
        error: str = "",
        duration_seconds: float | None = None,
    ) -> FrameTask:
        with self._lock:
            task = self.tasks[frame]
            measured = time.monotonic() - task.started_at if task.started_at else 0.0
            task.duration_seconds = max(0.0, float(duration_seconds if duration_seconds is not None else measured))
            task.error = error
            worker.last_seen = time.time()
            if frame in worker.current_frames:
                worker.current_frames.remove(frame)
            worker.current_frame = worker.current_frames[0] if worker.current_frames else None
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
                    if worker and task.frame in worker.current_frames:
                        worker.current_frames.remove(task.frame)
                        worker.current_frame = worker.current_frames[0] if worker.current_frames else None
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

    def summary(self, workers: dict[str, WorkerState] | None = None) -> dict[str, object]:
        with self._lock:
            counts = {status: 0 for status in ("pending", "running", "completed", "failed")}
            for task in self.tasks.values():
                counts[task.status] = counts.get(task.status, 0) + 1
            total = len(self.tasks)
            elapsed_seconds = max(0.0, time.time() - self.created_at)
            rendered_now = max(0, counts["completed"] - self.initial_completed)
            frames_per_minute = (rendered_now / elapsed_seconds * 60.0) if elapsed_seconds > 0 and rendered_now else 0.0
            if workers:
                worker_rate = sum(
                    60.0 / worker.average_seconds
                    for worker in workers.values()
                    if not worker.disabled
                    and time.time() - worker.last_seen < WORKER_OFFLINE_SECONDS
                    and worker.average_seconds > 0
                )
                if worker_rate > 0:
                    frames_per_minute = worker_rate
            remaining_frames = counts["pending"] + counts["running"]
            eta_seconds = (remaining_frames / frames_per_minute * 60.0) if frames_per_minute > 0 else 0.0
            counts.update(
                {
                    "plan_id": self.plan_id,
                    "project_id": self.project_id,
                    "project_name": self.blend_path.name,
                    "source_fingerprint": self.source_fingerprint,
                    "start_frame": self.start_frame,
                    "end_frame": self.end_frame,
                    "total": total,
                    "progress": (counts["completed"] / total * 100.0) if total else 100.0,
                    "remaining_frames": remaining_frames,
                    "elapsed_seconds": elapsed_seconds,
                    "frames_per_minute": frames_per_minute,
                    "frames_per_hour": frames_per_minute * 60.0,
                    "eta_seconds": eta_seconds,
                    "chunk_mode": self.chunk_mode,
                    "chunk_size": self.chunk_size,
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
        transport: str = "lan",
        token: str | None = None,
        ssh_host: str = "",
        ssh_port: int = 22,
        ssh_user: str = "",
        require_pairing_code: bool = True,
        trusted_tokens: set[str] | None = None,
        on_trusted_token: Callable[[str, str], None] | None = None,
        controller_name: str | None = None,
        controller_hardware: str = "",
        on_event: Callable[[str], None] | None = None,
        on_frame: Callable[[int, Path, float], None] | None = None,
        group_id: str = "",
        controller_device_id: str = "",
    ) -> None:
        self.bind_host = bind_host
        self.port = port
        self.advertised_host = advertised_host or lan_address()
        self.transport = transport if transport in {"lan", "tailscale", "ssh"} else "lan"
        self.token = token or secrets.token_urlsafe(18)
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.ssh_user = ssh_user
        self.require_pairing_code = bool(require_pairing_code)
        self.trusted_tokens = set(trusted_tokens or set())
        self.on_trusted_token = on_trusted_token
        self.pairing_pin = self._new_pairing_pin()
        self.pairing_pin_expires_at = time.time() + 600
        self._pair_attempts: dict[str, list[float]] = {}
        self.controller_name = controller_name or socket.gethostname()
        self.group_id = str(group_id or uuid.uuid4().hex)
        self.controller_device_id = str(controller_device_id or "controller")
        self.controller_hardware = controller_hardware
        self.on_event = on_event
        self.on_frame = on_frame
        self.workers: dict[str, WorkerState] = {}
        self.workers_by_device: dict[str, str] = {}
        self.plan: NetworkRenderPlan | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._integrity_lock = threading.RLock()

    @property
    def pairing_code(self) -> str:
        if not self.port:
            raise RuntimeError("Coordinator has not started")
        return PairingCode(
            self.advertised_host,
            self.port,
            self.token,
            self.transport,
            self.ssh_host,
            self.ssh_port,
            self.ssh_user,
        ).encode()

    @property
    def invitation_link(self) -> str:
        return f"brw://join/{self.pairing_code}"

    @staticmethod
    def _new_pairing_pin() -> str:
        return f"{secrets.randbelow(1_000_000):06d}"

    def rotate_pairing_pin(self) -> str:
        self.pairing_pin = self._new_pairing_pin()
        self.pairing_pin_expires_at = time.time() + 600
        return self.pairing_pin

    def pair_device(self, device_name: str, pin: str, remote_address: str) -> tuple[dict[str, object], int]:
        now = time.time()
        recent = [stamp for stamp in self._pair_attempts.get(remote_address, []) if now - stamp < 60]
        if len(recent) >= 5:
            return {"ok": False, "error": "Too many pairing attempts. Wait one minute."}, 429
        if len(self.trusted_tokens) >= 128:
            return {"ok": False, "error": "Trusted device limit reached."}, 409
        if self.require_pairing_code:
            valid = now <= self.pairing_pin_expires_at and hmac.compare_digest(pin.strip(), self.pairing_pin)
            if not valid:
                recent.append(now)
                self._pair_attempts[remote_address] = recent
                return {"ok": False, "error": "The one-time code is invalid or expired."}, 403
        token = secrets.token_urlsafe(32)
        self.trusted_tokens.add(token)
        name = device_name.strip()[:80] or "Worker"
        if self.on_trusted_token:
            self.on_trusted_token(token, name)
        if self.require_pairing_code:
            self.rotate_pairing_pin()
        connection = PairingCode(self.advertised_host, self.port, token, "lan").encode()
        self.event(f"[NETWORK] Trusted device paired: {name}")
        return {"ok": True, "connection_code": connection, "controller": self.controller_name}, 200

    def event(self, message: str) -> None:
        if self.on_event:
            self.on_event(message)

    def start(self) -> str:
        coordinator = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "BlenderRenderWatchdog/3.0.1"

            def log_message(self, _format: str, *_args: object) -> None:
                return

            def _authorized(self) -> bool:
                query_token = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("token", [""])[0]
                supplied = self.headers.get("X-Watchdog-Token", "") or query_token
                return bool(supplied) and (hmac.compare_digest(supplied, coordinator.token) or supplied in coordinator.trusted_tokens)

            def _json_body(self, maximum: int = 1_500_000_000) -> dict[str, object]:
                length = int(self.headers.get("Content-Length", "0") or 0)
                if length < 0 or length > maximum:
                    raise ValueError("Request body is too large")
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
                route = urllib.parse.urlparse(self.path).path
                try:
                    data = self._json_body(32_768 if route == "/api/pair" else 1_500_000_000)
                    if route == "/api/pair":
                        result, status = coordinator.pair_device(
                            str(data.get("name") or "Worker"),
                            str(data.get("pin") or ""),
                            self.client_address[0],
                        )
                        self._send_json(result, status)
                    elif not self._authorized():
                        self._reject()
                    elif route == "/api/join":
                        result, status = coordinator.join(
                            str(data.get("name") or "Worker"),
                            str(data.get("hardware") or ""),
                            bool(data.get("use_cpu", True)),
                            bool(data.get("use_gpu", True)),
                            device_id=str(data.get("device_id") or ""),
                            identity_fingerprint=str(data.get("identity_fingerprint") or ""),
                            capabilities=data.get("capabilities") if isinstance(data.get("capabilities"), dict) else None,
                            compute_backend=str(data.get("compute_backend") or "AUTO"),
                        )
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
        chunk_mode: str = "adaptive",
        chunk_size: int = 10,
        project_id: str = "",
        source_fingerprint: str = "",
    ) -> NetworkRenderPlan:
        output_folder.mkdir(parents=True, exist_ok=True)
        self.plan = NetworkRenderPlan(
            blend_path,
            output_folder,
            start_frame,
            end_frame,
            completed_frames,
            chunk_mode=chunk_mode,
            chunk_size=chunk_size,
            project_id=project_id,
            source_fingerprint=source_fingerprint,
        )
        self.event(f"[NETWORK] Distributed render started: {start_frame}-{end_frame}")
        return self.plan

    def join(
        self,
        name: str,
        hardware: str,
        use_cpu: bool = True,
        use_gpu: bool = True,
        *,
        device_id: str = "",
        identity_fingerprint: str = "",
        capabilities: dict[str, object] | None = None,
        compute_backend: str = "AUTO",
    ) -> tuple[dict[str, object], int]:
        with self._lock:
            stable_device_id = str(device_id).strip() or uuid.uuid4().hex
            existing_worker_id = self.workers_by_device.get(stable_device_id)
            worker = self.workers.get(existing_worker_id or "")
            online = [
                current for current in self.workers.values()
                if not current.disabled and time.time() - current.last_seen < WORKER_OFFLINE_SECONDS
            ]
            if worker is None and len(online) >= MAX_WORKERS:
                return {"ok": False, "error": f"Maximum {MAX_WORKERS} workers reached"}, 409
            normalized_capabilities = DeviceCapabilities.from_dict(capabilities).to_dict()
            if worker is None:
                worker = WorkerState(
                    uuid.uuid4().hex,
                    name[:80] or "Worker",
                    hardware[:200],
                    use_cpu=bool(use_cpu),
                    use_gpu=bool(use_gpu),
                    device_id=stable_device_id,
                    identity_fingerprint=identity_fingerprint,
                    capabilities=normalized_capabilities,
                    compute_backend=compute_backend,
                )
                self.workers[worker.worker_id] = worker
                self.workers_by_device[stable_device_id] = worker.worker_id
            else:
                fingerprint = str(identity_fingerprint).strip()
                if worker.identity_fingerprint and fingerprint and worker.identity_fingerprint != fingerprint:
                    return {"ok": False, "error": "Device identity changed"}, 403
                worker.name = name[:80] or worker.name
                worker.hardware = hardware[:200]
                worker.identity_fingerprint = worker.identity_fingerprint or fingerprint
                worker.capabilities = normalized_capabilities or worker.capabilities
                worker.use_cpu = bool(use_cpu)
                worker.use_gpu = bool(use_gpu)
                requested_backend = str(compute_backend).strip().upper()
                worker.compute_backend = requested_backend if requested_backend in COMPUTE_BACKENDS else "AUTO"
                worker.disabled = False
                worker.last_seen = time.time()
        self.event(f"[NETWORK] Connected: {worker.name} ({worker.hardware or 'unknown hardware'})")
        return {
            "ok": True,
            "worker_id": worker.worker_id,
            "device_id": worker.device_id,
            "group_id": self.group_id,
            "reconnected": existing_worker_id is not None,
            "max_workers": MAX_WORKERS,
        }, 200

    def heartbeat(self, worker_id: str) -> dict[str, object]:
        worker = self.workers.get(worker_id)
        if worker is None or worker.disabled:
            return {"ok": False, "state": "disconnected", "error": "Disconnected by controller"}
        worker.last_seen = time.time()
        if self.plan:
            self.plan.release_stale(self.workers)
        return {"ok": True}

    def claim_task(self, worker_id: str) -> dict[str, object]:
        worker = self.workers.get(worker_id)
        if worker is None or worker.disabled:
            return {"ok": False, "state": "disconnected", "error": "Disconnected by controller"}
        worker.last_seen = time.time()
        plan = self.plan
        if plan is None:
            return {"ok": True, "state": "idle"}
        plan.release_stale(self.workers)
        summary = plan.summary(self.workers)
        if summary["finished"] and int(summary["failed"]) == 0 and not plan.integrity_audited:
            self.audit_completed_outputs(plan)
            summary = plan.summary(self.workers)
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
                and time.time() - other.last_seen < WORKER_OFFLINE_SECONDS
                and (other.frame_start is not None or other.frame_end is not None)
            )
        ]
        batch = plan.claim_batch(worker, reserved_ranges)
        if batch is None:
            return {"ok": True, "state": "waiting", "summary": summary}
        return {
            "ok": True,
            "state": "task",
            "frame": batch.frames[0],
            "frames": batch.frames,
            "plan_id": plan.plan_id,
            "project_id": plan.project_id,
            "project_name": plan.blend_path.name,
            "source_fingerprint": plan.source_fingerprint,
            "samples": worker.samples,
            "use_cpu": worker.use_cpu,
            "use_gpu": worker.use_gpu,
            "compute_backend": worker.compute_backend,
            "render_device": render_device_label(worker.use_cpu, worker.use_gpu),
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
        duration_value = data.get("duration_seconds")
        duration_seconds = float(duration_value) if duration_value is not None else None
        task = plan.complete(worker, frame, success, str(data.get("error") or ""), duration_seconds)
        if success and output_path:
            self.event(f"[NETWORK] Frame {frame} received from {worker.name}")
            if self.on_frame:
                self.on_frame(frame, output_path, task.duration_seconds)
        else:
            self.event(f"[NETWORK] Frame {frame} failed on {worker.name}; retry scheduled")
        summary = plan.summary(self.workers)
        if summary["finished"] and int(summary["failed"]) == 0 and not plan.integrity_audited:
            self.audit_completed_outputs(plan)
        return {"ok": True, "state": task.status, "summary": plan.summary(self.workers)}

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
        use_cpu: bool | None = None,
        use_gpu: bool | None = None,
        compute_backend: str | None = None,
        chunk_size: int | None = None,
    ) -> bool:
        worker = self.workers.get(worker_id)
        if worker is None:
            return False
        if start_frame is not None and end_frame is not None and start_frame > end_frame:
            raise ValueError("start_frame cannot be greater than end_frame")
        if samples is not None and samples < 1:
            raise ValueError("samples must be greater than 0")
        selected_cpu = worker.use_cpu if use_cpu is None else bool(use_cpu)
        selected_gpu = worker.use_gpu if use_gpu is None else bool(use_gpu)
        if not selected_cpu and not selected_gpu:
            raise ValueError("At least CPU or GPU must be enabled")
        worker.frame_start = start_frame
        worker.frame_end = end_frame
        worker.samples = samples
        worker.use_cpu = selected_cpu
        worker.use_gpu = selected_gpu
        if compute_backend is not None:
            backend = str(compute_backend).strip().upper()
            if backend not in COMPUTE_BACKENDS:
                raise ValueError("Unsupported compute backend")
            supported = {str(value).upper() for value in worker.capabilities.get("compute_backends", [])}
            if backend != "AUTO" and supported and backend not in supported:
                raise ValueError(f"{backend} is not available on this device")
            worker.compute_backend = backend
        if chunk_size is not None:
            worker.chunk_size = max(1, min(1000, int(chunk_size)))
        return True

    def set_worker_range(self, worker_id: str, start_frame: int | None, end_frame: int | None) -> bool:
        return self.set_worker_settings(worker_id, start_frame, end_frame, self.workers.get(worker_id).samples if worker_id in self.workers else None)

    def disconnect_worker(self, worker_id: str) -> bool:
        with self._lock:
            worker = self.workers.get(worker_id)
            if worker is None:
                return False
            released = self.plan.release_worker(worker_id) if self.plan else []
            worker.current_frame = None
            worker.current_frames.clear()
            worker.disabled = True
            worker.last_seen = 0.0
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
            "use_cpu": True,
            "use_gpu": True,
            "render_device": "GPU + CPU",
            "is_controller": True,
            "settings_worker_id": "",
            "device_id": self.controller_device_id,
            "identity_fingerprint": "",
            "capabilities": {},
            "compute_backend": "AUTO",
            "chunk_size": None,
            "current_frames": [],
            "disabled": False,
        }
        if local_worker is not None:
            controller_row.update(local_worker)
            controller_row["settings_worker_id"] = local_worker["worker_id"]
            controller_row["worker_id"] = "controller"
            controller_row["is_controller"] = True
        devices = [controller_row, *(worker for worker in worker_rows if worker is not local_worker)]
        return {
            "ok": True,
            "controller": {
                "name": self.controller_name,
                "host": self.advertised_host,
                "transport": self.transport,
                "device_id": self.controller_device_id,
                "group_id": self.group_id,
            },
            "workers": worker_rows,
            "devices": devices,
            "plan": self.plan.summary(self.workers) if self.plan else None,
        }


def _request_json(url: str, token: str, data: dict[str, object] | None = None, timeout: float = 30.0) -> dict[str, object]:
    payload = json.dumps(data).encode("utf-8") if data is not None else None
    request = urllib.request.Request(url, data=payload, headers={"X-Watchdog-Token": token})
    if payload is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    return result if isinstance(result, dict) else {}


def request_pairing(host: str, port: int, device_name: str, pin: str = "", timeout: float = 8.0) -> str:
    """Exchange a short one-time PIN for a device-specific reusable token."""
    if not host or not 1 <= int(port) <= 65535:
        raise ValueError("Invalid controller address")
    try:
        result = _request_json(
            f"http://{host}:{int(port)}/api/pair",
            "",
            {"name": device_name, "pin": pin.strip()},
            timeout=timeout,
        )
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read().decode("utf-8")).get("error")
        except (OSError, ValueError, json.JSONDecodeError):
            detail = ""
        raise ConnectionError(str(detail or f"Pairing failed ({error.code})")) from error
    code = str(result.get("connection_code") or "")
    PairingCode.decode(code)
    return code


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
        use_cpu: bool = True,
        use_gpu: bool = True,
        device_id: str = "",
        identity_fingerprint: str = "",
        capabilities: DeviceCapabilities | dict[str, object] | None = None,
        compute_backend: str = "AUTO",
    ) -> None:
        self.connection = PairingCode.decode(code)
        self.blender = blender
        self.name = name or socket.gethostname()
        self.hardware = hardware
        self.cache_folder = cache_folder or Path(tempfile.gettempdir()) / "BlenderRenderWatchdogWorker"
        self.on_event = on_event
        self.render_frame = render_frame
        self.use_cpu = bool(use_cpu)
        self.use_gpu = bool(use_gpu)
        self.device_id = str(device_id or uuid.uuid4().hex)
        self.identity_fingerprint = str(identity_fingerprint)
        if isinstance(capabilities, DeviceCapabilities):
            self.capabilities = capabilities.to_dict()
        else:
            self.capabilities = DeviceCapabilities.from_dict(capabilities).to_dict()
        requested_backend = str(compute_backend).strip().upper()
        self.compute_backend = requested_backend if requested_backend in COMPUTE_BACKENDS else "AUTO"
        self.worker_id = ""
        self.stop_event = threading.Event()
        self._project_id = ""
        self._project_path: Path | None = None
        self.status_snapshot: dict[str, object] = {}
        self._finished_plan_id = ""

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
            {
                "name": self.name,
                "hardware": self.hardware,
                "use_cpu": self.use_cpu,
                "use_gpu": self.use_gpu,
                "device_id": self.device_id,
                "identity_fingerprint": self.identity_fingerprint,
                "capabilities": self.capabilities,
                "compute_backend": self.compute_backend,
            },
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
        self._cleanup_project()
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

    def _is_inside_cache(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.cache_folder.resolve())
            return True
        except (OSError, ValueError):
            return False

    def _cleanup_frame_output(self, output: Path | None) -> None:
        if output is None or not self._is_inside_cache(output):
            return
        frames_root = self.cache_folder / "frames"
        try:
            output.resolve().relative_to(frames_root.resolve())
        except (OSError, ValueError):
            return
        shutil.rmtree(output.parent, ignore_errors=True)

    def _cleanup_project(self) -> None:
        project = self._project_path
        if project is not None and self._is_inside_cache(project):
            try:
                project.unlink(missing_ok=True)
            except OSError:
                pass
        self._project_id = ""
        self._project_path = None

    def cleanup_cache(self) -> None:
        """Remove only files created inside this worker's dedicated cache."""
        self._cleanup_project()
        frames_root = self.cache_folder / "frames"
        if self._is_inside_cache(frames_root):
            shutil.rmtree(frames_root, ignore_errors=True)
        try:
            self.cache_folder.rmdir()
        except OSError:
            pass

    def _render_frame(
        self,
        frame: int,
        project: Path,
        samples: int | None = None,
        use_cpu: bool | None = None,
        use_gpu: bool | None = None,
    ) -> tuple[bool, Path | None, str]:
        frame_folder = self.cache_folder / "frames" / uuid.uuid4().hex
        frame_folder.mkdir(parents=True, exist_ok=True)
        device_script = frame_folder / "watchdog_worker_device.py"
        device_script.write_text(
            worker_device_script(
                self.use_cpu if use_cpu is None else bool(use_cpu),
                self.use_gpu if use_gpu is None else bool(use_gpu),
                samples,
                self.compute_backend,
            ),
            encoding="utf-8",
        )
        command = [
            str(self.blender),
            "-b",
            str(project),
            "-o",
            str(frame_folder / "frame_####"),
            "--python",
            str(device_script),
        ]
        command.extend(["-f", str(frame)])
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
        candidates = [path for path in frame_folder.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS]
        output = max(candidates, key=lambda path: path.stat().st_mtime, default=None)
        error = (completed.stdout + completed.stderr)[-2000:]
        return completed.returncode == 0 and output is not None, output, error

    def _render_batch(
        self,
        frames: list[int],
        project: Path,
        samples: int | None = None,
        use_cpu: bool | None = None,
        use_gpu: bool | None = None,
        compute_backend: str | None = None,
    ) -> tuple[dict[int, tuple[bool, Path | None, str]], Path]:
        """Render one contiguous frame chunk in a single hidden Blender process."""
        if not frames:
            raise ValueError("Render batch is empty")
        ordered = sorted(dict.fromkeys(int(frame) for frame in frames))
        if ordered != list(range(ordered[0], ordered[-1] + 1)):
            raise ValueError("Render batch must contain contiguous frames")
        frame_folder = self.cache_folder / "frames" / uuid.uuid4().hex
        frame_folder.mkdir(parents=True, exist_ok=True)
        device_script = frame_folder / "watchdog_worker_device.py"
        device_script.write_text(
            worker_device_script(
                self.use_cpu if use_cpu is None else bool(use_cpu),
                self.use_gpu if use_gpu is None else bool(use_gpu),
                samples,
                compute_backend or self.compute_backend,
            ),
            encoding="utf-8",
        )
        command = [
            str(self.blender),
            "-b",
            str(project),
            "-o",
            str(frame_folder / "frame_####"),
            "--python",
            str(device_script),
            "-s",
            str(ordered[0]),
            "-e",
            str(ordered[-1]),
            "-a",
        ]
        started_at = time.monotonic()
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
        elapsed = max(0.0, time.monotonic() - started_at)
        error = (completed.stdout + completed.stderr)[-2000:]
        outputs: dict[int, Path] = {}
        for path in frame_folder.iterdir():
            if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                continue
            match = re.search(r"(\d+)(?=\.[^.]+$)", path.name)
            if match:
                outputs[int(match.group(1))] = path
        per_frame_seconds = elapsed / len(ordered) if ordered else 0.0
        results = {
            frame: (
                completed.returncode == 0 and frame in outputs,
                outputs.get(frame),
                "" if completed.returncode == 0 and frame in outputs else error or "Blender did not create the frame",
            )
            for frame in ordered
        }
        self._last_batch_frame_seconds = per_frame_seconds
        return results, frame_folder

    def _heartbeat_loop(self, finished: threading.Event, interval: float) -> None:
        while not finished.wait(max(0.01, interval)) and not self.stop_event.is_set():
            try:
                heartbeat = _request_json(
                    self.base_url + "/api/heartbeat",
                    self.connection.token,
                    {"worker_id": self.worker_id},
                    timeout=15,
                )
                if not heartbeat.get("ok") and heartbeat.get("state") == "disconnected":
                    self.event("[NETWORK] Disconnected by the main computer")
                    self.stop_event.set()
                    return
            except (OSError, urllib.error.URLError, ConnectionError, json.JSONDecodeError) as error:
                self.event(f"[NETWORK] Heartbeat warning: {error}")

    def run(
        self,
        poll_seconds: float = 2.0,
        stay_connected: bool = False,
        heartbeat_seconds: float = WORKER_HEARTBEAT_SECONDS,
    ) -> None:
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
                    heartbeat_finished = threading.Event()
                    heartbeat_thread = threading.Thread(
                        target=self._heartbeat_loop,
                        args=(heartbeat_finished, heartbeat_seconds),
                        name="render-worker-heartbeat",
                        daemon=True,
                    )
                    heartbeat_thread.start()
                    try:
                        plan_id = str(task["plan_id"])
                        self._finished_plan_id = ""
                        if self._project_id != plan_id or self._project_path is None or not self._project_path.exists():
                            self.event("[NETWORK] Downloading project...")
                            self._download_project(plan_id, str(task.get("project_name") or "project.blend"))
                        raw_frames = task.get("frames")
                        frames = (
                            [int(frame) for frame in raw_frames]
                            if isinstance(raw_frames, list) and raw_frames
                            else [int(task["frame"])]
                        )
                        samples = int(task["samples"]) if task.get("samples") is not None else None
                        self.use_cpu = bool(task.get("use_cpu", self.use_cpu))
                        self.use_gpu = bool(task.get("use_gpu", self.use_gpu))
                        backend = str(task.get("compute_backend") or self.compute_backend).strip().upper()
                        self.compute_backend = backend if backend in COMPUTE_BACKENDS else "AUTO"
                        range_label = str(frames[0]) if len(frames) == 1 else f"{frames[0]}–{frames[-1]}"
                        self.event(f"[NETWORK] Rendering frames {range_label}")
                        if self.render_frame:
                            rendered = {}
                            batch_folder = None
                            for frame in frames:
                                started_at = time.monotonic()
                                success, output, error = self.render_frame(frame, self._project_path)
                                rendered[frame] = (success, output, error, max(0.0, time.monotonic() - started_at))
                        else:
                            batch_results, batch_folder = self._render_batch(
                                frames,
                                self._project_path,
                                samples,
                                self.use_cpu,
                                self.use_gpu,
                                self.compute_backend,
                            )
                            rendered = {
                                frame: (*batch_results[frame], getattr(self, "_last_batch_frame_seconds", 0.0))
                                for frame in frames
                            }
                        try:
                            for frame in frames:
                                success, output, error, duration_seconds = rendered[frame]
                                result: dict[str, object] = {
                                    "worker_id": self.worker_id,
                                    "frame": frame,
                                    "success": success,
                                    "error": "" if success else error,
                                    "duration_seconds": duration_seconds,
                                }
                                if success and output:
                                    result["extension"] = output.suffix.lower()
                                    result["file_base64"] = base64.b64encode(output.read_bytes()).decode("ascii")
                                _request_json(self.base_url + "/api/result", self.connection.token, result, timeout=600)
                        finally:
                            if batch_folder is not None and self._is_inside_cache(batch_folder):
                                shutil.rmtree(batch_folder, ignore_errors=True)
                            else:
                                for _success, output, _error, _duration in rendered.values():
                                    self._cleanup_frame_output(output)
                    finally:
                        heartbeat_finished.set()
                        heartbeat_thread.join(timeout=2)
                    if self.stop_event.is_set():
                        return
                elif state == "finished" and not stay_connected:
                    self.cleanup_cache()
                    self.event("[NETWORK] Distributed render is complete")
                    return
                elif state == "finished":
                    plan_id = str((task.get("summary") or {}).get("plan_id") or "") if isinstance(task.get("summary"), dict) else ""
                    if plan_id and plan_id != self._finished_plan_id:
                        self.cleanup_cache()
                        self._finished_plan_id = plan_id
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
