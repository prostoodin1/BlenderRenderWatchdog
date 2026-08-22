"""Windows OpenSSH installation helpers and a hidden local forwarding tunnel."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from process_utils import hidden_subprocess_kwargs


@dataclass(frozen=True, slots=True)
class OpenSshState:
    client_installed: bool
    server_installed: bool = False
    server_running: bool = False
    client_path: Path | None = None
    message: str = ""


def find_ssh_client() -> Path | None:
    located = shutil.which("ssh") or shutil.which("ssh.exe")
    candidates = [Path(located)] if located else []
    windows = os.environ.get("WINDIR")
    if windows:
        candidates.append(Path(windows) / "System32" / "OpenSSH" / "ssh.exe")
    return next((path for path in candidates if path.is_file()), None)


def query_openssh_state(
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> OpenSshState:
    client = find_ssh_client()
    if os.name != "nt":
        return OpenSshState(bool(client), client_path=client, message="OpenSSH client is ready" if client else "OpenSSH client is not installed")
    try:
        completed = runner(
            ["sc.exe", "query", "sshd"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            **hidden_subprocess_kwargs(),
        )
        output = f"{completed.stdout}\n{completed.stderr}".upper()
        server_installed = completed.returncode == 0 and "SERVICE_NAME: SSHD" in output
        server_running = server_installed and "RUNNING" in output
    except (OSError, subprocess.SubprocessError):
        server_installed = False
        server_running = False
    if server_running:
        message = "OpenSSH client and server are ready"
    elif server_installed:
        message = "OpenSSH server is installed but stopped"
    elif client:
        message = "OpenSSH client is ready"
    else:
        message = "OpenSSH is not installed"
    return OpenSshState(bool(client), server_installed, server_running, client, message)


def launch_openssh_install(install_server: bool) -> Path:
    """Launch an elevated official Windows Capability install script."""
    if os.name != "nt":
        raise OSError("Automatic OpenSSH installation is available on Windows only")
    script = Path(tempfile.gettempdir()) / "blender_watchdog_install_openssh.ps1"
    server_lines = ""
    if install_server:
        server_lines = """
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0 -ErrorAction Stop | Out-Null
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic
if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
}
"""
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0 -ErrorAction Stop | Out-Null\n"
        + server_lines
        + "Write-Host 'OpenSSH setup is complete. You can close this window.'\nPause\n",
        encoding="utf-8-sig",
    )
    import ctypes

    arguments = f'-NoProfile -ExecutionPolicy Bypass -File "{script}"'
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", arguments, None, 1)
    if int(result) <= 32:
        raise OSError("Windows did not start the OpenSSH installer")
    return script


def _available_local_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class SshTunnel:
    def __init__(
        self,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        remote_port: int,
        identity_file: Path | None = None,
    ) -> None:
        self.ssh_host = ssh_host
        self.ssh_port = ssh_port
        self.ssh_user = ssh_user
        self.remote_port = remote_port
        self.identity_file = identity_file
        self.local_port = 0
        self.process: subprocess.Popen[str] | None = None

    def start(self, timeout: float = 8.0) -> int:
        client = find_ssh_client()
        if client is None:
            raise FileNotFoundError("OpenSSH client is not installed")
        self.local_port = _available_local_port()
        command = [
            str(client),
            "-N",
            "-T",
            "-L",
            f"127.0.0.1:{self.local_port}:127.0.0.1:{self.remote_port}",
            "-p",
            str(self.ssh_port),
            "-l",
            self.ssh_user,
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=15",
            "-o",
            "ServerAliveCountMax=3",
            "-o",
            "StrictHostKeyChecking=accept-new",
        ]
        if self.identity_file:
            command.extend(["-i", str(self.identity_file)])
        command.append(self.ssh_host)
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                detail = self.process.stderr.read().strip() if self.process.stderr else ""
                self.process = None
                raise ConnectionError(detail or "SSH tunnel could not be started")
            try:
                with socket.create_connection(("127.0.0.1", self.local_port), timeout=0.2):
                    return self.local_port
            except OSError:
                time.sleep(0.1)
        self.stop()
        raise TimeoutError("SSH tunnel timed out. Configure an SSH key or ssh-agent and check the server address.")

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.process = None
