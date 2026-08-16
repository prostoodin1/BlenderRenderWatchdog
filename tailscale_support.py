"""Small Windows Tailscale integration used by the distributed render UI."""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from process_utils import hidden_subprocess_kwargs


TAILSCALE_DOWNLOAD_PAGE = "https://tailscale.com/download/windows"
TAILSCALE_WINDOWS_INSTALLER_URL = "https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe"


@dataclass(frozen=True, slots=True)
class TailscaleState:
    installed: bool
    online: bool = False
    backend_state: str = ""
    ipv4: str = ""
    dns_name: str = ""
    executable: Path | None = None
    message: str = ""


def _candidate_executables() -> list[Path]:
    candidates: list[Path] = []
    located = shutil.which("tailscale") or shutil.which("tailscale.exe")
    if located:
        candidates.append(Path(located))
    for variable in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / "Tailscale" / "tailscale.exe")
    return candidates


def find_tailscale_cli() -> Path | None:
    seen: set[str] = set()
    for candidate in _candidate_executables():
        normalized = str(candidate).casefold()
        if normalized in seen:
            continue
        seen.add(normalized)
        if candidate.is_file():
            return candidate
    return None


def _valid_tailscale_ipv4(values: object) -> str:
    if not isinstance(values, list):
        return ""
    fallback = ""
    for value in values:
        candidate = str(value or "").strip()
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.version != 4:
            continue
        if address in ipaddress.ip_network("100.64.0.0/10"):
            return candidate
        fallback = fallback or candidate
    return fallback


def parse_tailscale_status(payload: str, executable: Path | None = None) -> TailscaleState:
    try:
        data = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        return TailscaleState(True, executable=executable, message=f"Invalid Tailscale status: {error}")
    if not isinstance(data, dict):
        return TailscaleState(True, executable=executable, message="Invalid Tailscale status")

    self_state = data.get("Self") if isinstance(data.get("Self"), dict) else {}
    backend = str(data.get("BackendState") or self_state.get("BackendState") or "")
    addresses = self_state.get("TailscaleIPs") or data.get("TailscaleIPs") or []
    ipv4 = _valid_tailscale_ipv4(addresses)
    dns_name = str(self_state.get("DNSName") or "").rstrip(".")
    online = backend.casefold() == "running" and bool(ipv4)
    if online:
        message = f"Connected as {dns_name or ipv4}"
    elif backend.casefold() in {"needslogin", "nostate", "stopped"}:
        message = "Tailscale sign-in is required"
    else:
        message = f"Tailscale is not ready ({backend or 'unknown state'})"
    return TailscaleState(True, online, backend, ipv4, dns_name, executable, message)


def query_tailscale_status(
    timeout: float = 6.0,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> TailscaleState:
    executable = find_tailscale_cli()
    if executable is None:
        return TailscaleState(False, message="Tailscale is not installed")
    try:
        completed = runner(
            [str(executable), "status", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        return TailscaleState(True, executable=executable, message=f"Could not read Tailscale status: {error}")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "Tailscale is not ready").strip()
        return TailscaleState(True, executable=executable, message=detail)
    return parse_tailscale_status(completed.stdout, executable)


def download_tailscale_installer(destination: Path | None = None) -> Path:
    target = destination or Path(tempfile.gettempdir()) / "tailscale-setup-latest.exe"
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(
        TAILSCALE_WINDOWS_INSTALLER_URL,
        headers={"User-Agent": "BlenderRenderWatchdog/2.5.1"},
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        if temporary.stat().st_size < 1_000_000:
            raise OSError("The downloaded Tailscale installer is incomplete")
        temporary.replace(target)
        return target
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def launch_tailscale_installer(installer: Path) -> subprocess.Popen[bytes]:
    return subprocess.Popen([str(installer)])


def start_tailscale_login(executable: Path | None = None) -> subprocess.Popen[bytes]:
    cli = executable or find_tailscale_cli()
    if cli is None:
        raise FileNotFoundError("Tailscale is not installed")
    return subprocess.Popen([str(cli), "up"], **hidden_subprocess_kwargs())
