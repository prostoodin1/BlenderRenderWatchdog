"""Stable or rotating credentials shared by desktop and mobile clients."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass


ACCESS_MODE_ROTATE = "rotate"
ACCESS_MODE_PERSISTENT = "persistent"
ACCESS_MODES = (ACCESS_MODE_ROTATE, ACCESS_MODE_PERSISTENT)
DEFAULT_NETWORK_PORT = 48620
DEFAULT_MOBILE_PORT = 48621


def normalize_access_mode(value: str | None) -> str:
    return ACCESS_MODE_PERSISTENT if str(value or "").strip().lower() == ACCESS_MODE_PERSISTENT else ACCESS_MODE_ROTATE


def generate_access_key() -> str:
    """Return a readable secret suitable for saving in the desktop config."""
    return secrets.token_urlsafe(18)


def validate_access_key(value: str) -> str:
    key = value.strip()
    if len(key) < 8:
        raise ValueError("Access key must contain at least 8 characters")
    return key


def service_token(access_key: str, service: str) -> str:
    """Derive independent bearer tokens without exposing the saved master key."""
    key = validate_access_key(access_key).encode("utf-8")
    digest = hmac.new(key, f"BlenderRenderWatchdog/2.4.1/{service}".encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest[:21]).decode("ascii").rstrip("=")


@dataclass(frozen=True, slots=True)
class ServiceAccess:
    token: str
    port: int


def resolve_service_access(mode: str, access_key: str, service: str) -> ServiceAccess:
    """Choose fixed credentials in persistent mode and fresh credentials otherwise."""
    if normalize_access_mode(mode) == ACCESS_MODE_PERSISTENT:
        ports = {"network": DEFAULT_NETWORK_PORT, "mobile": DEFAULT_MOBILE_PORT}
        if service not in ports:
            raise ValueError(f"Unknown access service: {service}")
        return ServiceAccess(service_token(access_key, service), ports[service])
    return ServiceAccess(secrets.token_urlsafe(18), 0)


@dataclass(frozen=True, slots=True)
class MobileSyncCode:
    host: str
    port: int
    token: str
    name: str
    version: str = "2.4.1"

    def encode(self) -> str:
        payload = json.dumps(
            {"h": self.host, "p": self.port, "t": self.token, "n": self.name, "v": self.version},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return "BRWM1-" + base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, value: str) -> "MobileSyncCode":
        raw = value.strip()
        if not raw.startswith("BRWM1-"):
            raise ValueError("Invalid mobile sync code")
        encoded = raw[6:] + "=" * (-len(raw[6:]) % 4)
        try:
            data = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")).decode("utf-8"))
            result = cls(
                host=str(data["h"]).strip(),
                port=int(data["p"]),
                token=str(data["t"]).strip(),
                name=str(data.get("n") or data["h"]).strip(),
                version=str(data.get("v") or ""),
            )
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Invalid mobile sync code") from error
        if not result.host or not result.token or not result.name or not 1 <= result.port <= 65535:
            raise ValueError("Invalid mobile sync code")
        return result

