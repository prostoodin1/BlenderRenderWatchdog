"""Persistent render groups and stable device identities.

The network transport may change between LAN and SSH, but a device remains the
same member because its installation identity is independent from its address.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable


GROUP_SECURITY_MODES = {"open", "approval", "code"}
DEVICE_ROLES = {"coordinator", "worker"}


def infer_compute_backends(gpus: Iterable[object], system_name: str = "") -> list[str]:
    """Return Blender Cycles backends that are plausible for detected hardware."""
    labels = " ".join(str(gpu) for gpu in gpus).casefold()
    system = str(system_name).casefold()
    backends: list[str] = []
    if "nvidia" in labels or "geforce" in labels or "quadro" in labels or "rtx" in labels:
        backends.extend(["OPTIX", "CUDA"])
    if "amd" in labels or "radeon" in labels:
        backends.append("HIP")
    if "intel" in labels or "arc" in labels:
        backends.append("ONEAPI")
    if system in {"darwin", "macos", "mac"}:
        backends.append("METAL")
    return _clean_strings(backends)


def _clean_strings(values: Iterable[object], limit: int = 32) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        key = text.casefold()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text[:160])
        if len(result) >= limit:
            break
    return result


@dataclass(slots=True)
class DeviceIdentity:
    device_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    secret: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(self.secret.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, str]:
        return {"device_id": self.device_id, "secret": self.secret}

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "DeviceIdentity":
        device_id = str(data.get("device_id") or "").strip()
        secret = str(data.get("secret") or "").strip()
        if len(device_id) < 16 or len(secret) < 24:
            return cls()
        return cls(device_id=device_id, secret=secret)


@dataclass(slots=True)
class DeviceCapabilities:
    cpu: str = ""
    gpus: list[str] = field(default_factory=list)
    memory_gb: float = 0.0
    blender_version: str = ""
    compute_backends: list[str] = field(default_factory=list)
    render_engines: list[str] = field(default_factory=lambda: ["BLENDER_EEVEE_NEXT", "CYCLES"])
    platform: str = ""

    def __post_init__(self) -> None:
        self.cpu = str(self.cpu).strip()[:200]
        self.gpus = _clean_strings(self.gpus)
        self.memory_gb = max(0.0, float(self.memory_gb or 0.0))
        self.blender_version = str(self.blender_version).strip()[:40]
        self.compute_backends = [value.upper() for value in _clean_strings(self.compute_backends)]
        self.render_engines = [value.upper() for value in _clean_strings(self.render_engines)]
        self.platform = str(self.platform).strip()[:80]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: object) -> "DeviceCapabilities":
        if not isinstance(data, dict):
            return cls()
        return cls(
            cpu=str(data.get("cpu") or ""),
            gpus=list(data.get("gpus") or []),
            memory_gb=float(data.get("memory_gb") or 0.0),
            blender_version=str(data.get("blender_version") or ""),
            compute_backends=list(data.get("compute_backends") or []),
            render_engines=list(data.get("render_engines") or ["BLENDER_EEVEE_NEXT", "CYCLES"]),
            platform=str(data.get("platform") or ""),
        )


@dataclass(slots=True)
class DeviceRecord:
    device_id: str
    name: str
    identity_fingerprint: str = ""
    role: str = "worker"
    capabilities: DeviceCapabilities = field(default_factory=DeviceCapabilities)
    addresses: list[str] = field(default_factory=list)
    trusted: bool = True
    can_coordinate: bool = True
    joined_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.device_id = str(self.device_id).strip()
        if not self.device_id:
            raise ValueError("device_id is required")
        self.name = str(self.name).strip()[:80] or "Render device"
        self.identity_fingerprint = str(self.identity_fingerprint).strip()[:128]
        self.role = self.role if self.role in DEVICE_ROLES else "worker"
        if not isinstance(self.capabilities, DeviceCapabilities):
            self.capabilities = DeviceCapabilities.from_dict(self.capabilities)
        self.addresses = _clean_strings(self.addresses, limit=8)
        self.joined_at = max(0.0, float(self.joined_at or time.time()))
        self.last_seen = max(self.joined_at, float(self.last_seen or self.joined_at))

    def update(
        self,
        *,
        name: str = "",
        identity_fingerprint: str = "",
        capabilities: DeviceCapabilities | None = None,
        address: str = "",
        role: str = "",
        seen_at: float | None = None,
    ) -> None:
        if name.strip():
            self.name = name.strip()[:80]
        if identity_fingerprint.strip():
            fingerprint = identity_fingerprint.strip()[:128]
            if self.identity_fingerprint and self.identity_fingerprint != fingerprint:
                raise ValueError("Device identity fingerprint changed")
            self.identity_fingerprint = fingerprint
        if capabilities is not None:
            self.capabilities = capabilities
        if address.strip():
            self.addresses = _clean_strings([address, *self.addresses], limit=8)
        if role in DEVICE_ROLES:
            self.role = role
        self.last_seen = max(self.last_seen, float(seen_at or time.time()))

    def is_online(self, now: float | None = None, timeout: float = 30.0) -> bool:
        return float(now or time.time()) - self.last_seen < timeout

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["capabilities"] = self.capabilities.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "DeviceRecord":
        return cls(
            device_id=str(data.get("device_id") or ""),
            name=str(data.get("name") or "Render device"),
            identity_fingerprint=str(data.get("identity_fingerprint") or ""),
            role=str(data.get("role") or "worker"),
            capabilities=DeviceCapabilities.from_dict(data.get("capabilities")),
            addresses=list(data.get("addresses") or []),
            trusted=bool(data.get("trusted", True)),
            can_coordinate=bool(data.get("can_coordinate", True)),
            joined_at=float(data.get("joined_at") or time.time()),
            last_seen=float(data.get("last_seen") or time.time()),
        )


@dataclass(slots=True)
class RenderGroup:
    name: str
    owner_device_id: str
    group_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    security_mode: str = "approval"
    visible_on_lan: bool = True
    allow_failover: bool = True
    coordinator_device_id: str = ""
    ssh_endpoint: str = ""
    members: dict[str, DeviceRecord] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        self.name = str(self.name).strip()[:80] or "Render group"
        self.owner_device_id = str(self.owner_device_id).strip()
        self.group_id = str(self.group_id).strip() or uuid.uuid4().hex
        self.security_mode = self.security_mode if self.security_mode in GROUP_SECURITY_MODES else "approval"
        self.coordinator_device_id = str(self.coordinator_device_id or self.owner_device_id).strip()
        self.ssh_endpoint = str(self.ssh_endpoint).strip()[:500]
        converted: dict[str, DeviceRecord] = {}
        for device_id, member in dict(self.members).items():
            record = member if isinstance(member, DeviceRecord) else DeviceRecord.from_dict(member)
            converted[str(device_id or record.device_id)] = record
        self.members = converted

    def register_device(
        self,
        device_id: str,
        name: str,
        *,
        identity_fingerprint: str = "",
        capabilities: DeviceCapabilities | None = None,
        address: str = "",
        role: str = "worker",
        trusted: bool = True,
    ) -> DeviceRecord:
        """Insert or refresh one stable member without creating duplicates."""
        member = self.members.get(device_id)
        if member is None:
            member = DeviceRecord(
                device_id=device_id,
                name=name,
                identity_fingerprint=identity_fingerprint,
                role=role,
                capabilities=capabilities or DeviceCapabilities(),
                addresses=[address] if address else [],
                trusted=trusted,
            )
            self.members[device_id] = member
        else:
            member.update(
                name=name,
                identity_fingerprint=identity_fingerprint,
                capabilities=capabilities,
                address=address,
                role=role,
            )
            member.trusted = member.trusted or trusted
        self.updated_at = time.time()
        return member

    def elect_coordinator(self, now: float | None = None, timeout: float = 30.0) -> str:
        current = self.members.get(self.coordinator_device_id)
        if current and current.trusted and current.can_coordinate and current.is_online(now, timeout):
            return current.device_id
        if not self.allow_failover:
            return ""
        candidates = [
            member
            for member in self.members.values()
            if member.trusted and member.can_coordinate and member.is_online(now, timeout)
        ]
        if not candidates:
            self.coordinator_device_id = ""
            return ""
        owner = next((member for member in candidates if member.device_id == self.owner_device_id), None)
        chosen = owner or min(candidates, key=lambda item: (item.joined_at, item.device_id))
        self.coordinator_device_id = chosen.device_id
        chosen.role = "coordinator"
        self.updated_at = time.time()
        return chosen.device_id

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "owner_device_id": self.owner_device_id,
            "group_id": self.group_id,
            "security_mode": self.security_mode,
            "visible_on_lan": self.visible_on_lan,
            "allow_failover": self.allow_failover,
            "coordinator_device_id": self.coordinator_device_id,
            "ssh_endpoint": self.ssh_endpoint,
            "members": {device_id: member.to_dict() for device_id, member in self.members.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "RenderGroup":
        raw_members = data.get("members") if isinstance(data.get("members"), dict) else {}
        return cls(
            name=str(data.get("name") or "Render group"),
            owner_device_id=str(data.get("owner_device_id") or ""),
            group_id=str(data.get("group_id") or uuid.uuid4().hex),
            security_mode=str(data.get("security_mode") or "approval"),
            visible_on_lan=bool(data.get("visible_on_lan", True)),
            allow_failover=bool(data.get("allow_failover", True)),
            coordinator_device_id=str(data.get("coordinator_device_id") or ""),
            ssh_endpoint=str(data.get("ssh_endpoint") or ""),
            members={
                str(device_id): DeviceRecord.from_dict(member)
                for device_id, member in raw_members.items()
                if isinstance(member, dict)
            },
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or time.time()),
        )


class GroupRegistry:
    def __init__(
        self,
        identity: DeviceIdentity | None = None,
        groups: Iterable[RenderGroup] | None = None,
        active_group_id: str = "",
    ) -> None:
        self.identity = identity or DeviceIdentity()
        self.groups = {group.group_id: group for group in groups or []}
        self.active_group_id = active_group_id if active_group_id in self.groups else ""
        if not self.active_group_id and self.groups:
            self.active_group_id = next(iter(self.groups))

    @property
    def active(self) -> RenderGroup | None:
        return self.groups.get(self.active_group_id)

    def create_group(self, name: str, security_mode: str = "approval") -> RenderGroup:
        group = RenderGroup(name=name, owner_device_id=self.identity.device_id, security_mode=security_mode)
        group.register_device(
            self.identity.device_id,
            "Main PC",
            identity_fingerprint=self.identity.fingerprint,
            role="coordinator",
        )
        self.groups[group.group_id] = group
        self.active_group_id = group.group_id
        return group

    def remember_group(self, group: RenderGroup) -> RenderGroup:
        current = self.groups.get(group.group_id)
        if current is None or group.updated_at >= current.updated_at:
            self.groups[group.group_id] = group
        if not self.active_group_id:
            self.active_group_id = group.group_id
        return self.groups[group.group_id]

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 1,
            "identity": self.identity.to_dict(),
            "active_group_id": self.active_group_id,
            "groups": [group.to_dict() for group in self.groups.values()],
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path) -> "GroupRegistry":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        identity = DeviceIdentity.from_dict(data.get("identity") if isinstance(data.get("identity"), dict) else {})
        groups: list[RenderGroup] = []
        for raw_group in data.get("groups") if isinstance(data.get("groups"), list) else []:
            if not isinstance(raw_group, dict):
                continue
            try:
                groups.append(RenderGroup.from_dict(raw_group))
            except (TypeError, ValueError):
                continue
        return cls(identity, groups, str(data.get("active_group_id") or ""))
