"""Dependency-free discovery of Blender Render Watchdog controllers on a LAN."""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import asdict, dataclass


DISCOVERY_PORT = 48622
DISCOVERY_MAGIC = "BRW_DISCOVERY_2"
LEGACY_DISCOVERY_MAGIC = "BRW_DISCOVERY_1"
DISCOVERY_QUERY = b"BRW_DISCOVER_2"
LEGACY_DISCOVERY_QUERY = b"BRW_DISCOVER_1"


@dataclass(frozen=True, slots=True)
class DiscoveredController:
    controller_id: str
    name: str
    host: str
    port: int
    requires_code: bool
    version: str = "2.6.0"
    group_id: str = ""
    group_name: str = ""
    coordinator_device_id: str = ""
    device_count: int = 1
    security_mode: str = ""
    joinable: bool = True
    coordinator_online: bool = True

    @property
    def effective_group_id(self) -> str:
        return self.group_id or self.controller_id

    @property
    def effective_group_name(self) -> str:
        return self.group_name or self.name


def encode_announcement(controller: DiscoveredController) -> bytes:
    payload = asdict(controller)
    payload["group_id"] = controller.effective_group_id
    payload["group_name"] = controller.effective_group_name
    if not payload.get("security_mode"):
        payload["security_mode"] = "code" if controller.requires_code else "open"
    payload["magic"] = DISCOVERY_MAGIC
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def decode_announcement(payload: bytes, sender_host: str = "") -> DiscoveredController:
    try:
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict) or data.get("magic") not in {DISCOVERY_MAGIC, LEGACY_DISCOVERY_MAGIC}:
            raise ValueError
        host = str(sender_host or data.get("host") or "").strip()
        result = DiscoveredController(
            controller_id=str(data["controller_id"]).strip(),
            name=str(data["name"]).strip(),
            host=host,
            port=int(data["port"]),
            requires_code=bool(data.get("requires_code", True)),
            version=str(data.get("version") or ""),
            group_id=str(data.get("group_id") or data.get("controller_id") or "").strip(),
            group_name=str(data.get("group_name") or data.get("name") or "").strip(),
            coordinator_device_id=str(data.get("coordinator_device_id") or "").strip(),
            device_count=max(0, int(data.get("device_count") or 1)),
            security_mode=str(data.get("security_mode") or ("code" if data.get("requires_code", True) else "open")),
            joinable=bool(data.get("joinable", True)),
            coordinator_online=bool(data.get("coordinator_online", True)),
        )
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid Blender Render Watchdog LAN announcement") from error
    valid_port = 1 <= result.port <= 65535 if result.coordinator_online else 0 <= result.port <= 65535
    if not result.controller_id or not result.name or not result.host or not valid_port:
        raise ValueError("Invalid Blender Render Watchdog LAN announcement")
    return result


class LanDiscoveryAdvertiser:
    """Answer UDP probes without exposing a controller access token."""

    def __init__(self, controller: DiscoveredController, discovery_port: int = DISCOVERY_PORT) -> None:
        self.controller = controller
        self.discovery_port = discovery_port
        self._stop = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()

    def update(self, controller: DiscoveredController) -> None:
        with self._lock:
            self.controller = controller

    def start(self) -> None:
        if self._thread is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("0.0.0.0", self.discovery_port))
        sock.settimeout(0.5)
        self._socket = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="lan-discovery", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        assert self._socket is not None
        while not self._stop.is_set():
            try:
                payload, address = self._socket.recvfrom(512)
            except socket.timeout:
                continue
            except OSError:
                break
            if payload.strip() not in {DISCOVERY_QUERY, LEGACY_DISCOVERY_QUERY}:
                continue
            try:
                with self._lock:
                    announcement = encode_announcement(self.controller)
                self._socket.sendto(announcement, address)
            except OSError:
                continue

    def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._socket = None
        self._thread = None


def discover_controllers(timeout: float = 1.0, discovery_port: int = DISCOVERY_PORT) -> list[DiscoveredController]:
    """Probe the local broadcast domain and return de-duplicated controllers."""
    found: dict[str, DiscoveredController] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("0.0.0.0", 0))
        sock.settimeout(min(0.2, max(0.05, timeout)))
        for query in (DISCOVERY_QUERY, LEGACY_DISCOVERY_QUERY):
            for target in (("255.255.255.255", discovery_port), ("127.0.0.1", discovery_port)):
                try:
                    sock.sendto(query, target)
                except OSError:
                    continue
        deadline = time.monotonic() + max(0.05, timeout)
        while time.monotonic() < deadline:
            try:
                payload, address = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                controller = decode_announcement(payload, address[0])
            except ValueError:
                continue
            group_key = controller.effective_group_id
            previous = found.get(group_key)
            if previous is None or (controller.coordinator_online and not previous.coordinator_online):
                found[group_key] = controller
    finally:
        sock.close()
    return sorted(found.values(), key=lambda item: item.name.casefold())


def discover_groups(timeout: float = 1.0, discovery_port: int = DISCOVERY_PORT) -> list[DiscoveredController]:
    """Group-oriented alias used by the unified network screen."""
    return discover_controllers(timeout=timeout, discovery_port=discovery_port)
