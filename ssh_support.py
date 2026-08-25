"""Windows OpenSSH installation helpers and a hidden local forwarding tunnel."""

from __future__ import annotations

import os
import base64
import hashlib
import re
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
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


@dataclass(frozen=True, slots=True)
class SshInviteKey:
    private_path: Path
    public_path: Path
    private_key: str
    public_key: str
    fingerprint: str


def find_ssh_client() -> Path | None:
    located = shutil.which("ssh") or shutil.which("ssh.exe")
    candidates = [Path(located)] if located else []
    windows = os.environ.get("WINDIR")
    if windows:
        candidates.append(Path(windows) / "System32" / "OpenSSH" / "ssh.exe")
    return next((path for path in candidates if path.is_file()), None)


def find_ssh_keygen() -> Path | None:
    located = shutil.which("ssh-keygen") or shutil.which("ssh-keygen.exe")
    candidates = [Path(located)] if located else []
    windows = os.environ.get("WINDIR")
    if windows:
        candidates.append(Path(windows) / "System32" / "OpenSSH" / "ssh-keygen.exe")
    return next((path for path in candidates if path.is_file()), None)


def _safe_key_name(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip()).strip(".-")
    return (cleaned or "render-group")[:64]


def _validate_private_key(value: str) -> str:
    private_key = value.strip() + "\n"
    if not private_key.startswith("-----BEGIN OPENSSH PRIVATE KEY-----") or not private_key.rstrip().endswith(
        "-----END OPENSSH PRIVATE KEY-----"
    ):
        raise ValueError("Invalid OpenSSH private key")
    if len(private_key.encode("utf-8")) > 16_384:
        raise ValueError("OpenSSH private key is too large")
    return private_key


def _restrict_private_key(
    path: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass
    if os.name != "nt" or shutil.which("icacls.exe") is None:
        return
    user = os.environ.get("USERNAME", "").strip()
    domain = os.environ.get("USERDOMAIN", "").strip()
    if not user:
        return
    principal = f"{domain}\\{user}" if domain else user
    try:
        runner(
            ["icacls.exe", str(path), "/inheritance:r", "/grant:r", f"{principal}:(R)"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            **hidden_subprocess_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        pass


def ensure_ssh_invite_key(
    directory: Path,
    key_name: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> SshInviteKey:
    """Create or reuse a dedicated Ed25519 credential for one render group."""
    keygen = find_ssh_keygen()
    if keygen is None:
        raise FileNotFoundError("OpenSSH key generator is not installed")
    directory.mkdir(parents=True, exist_ok=True)
    private_path = directory / f"watchdog-{_safe_key_name(key_name)}-ed25519"
    public_path = private_path.with_suffix(".pub")
    if not private_path.is_file():
        completed = runner(
            [
                str(keygen),
                "-q",
                "-t",
                "ed25519",
                "-N",
                "",
                "-C",
                f"BlenderRenderWatchdog:{_safe_key_name(key_name)}",
                "-f",
                str(private_path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            **hidden_subprocess_kwargs(),
        )
        if completed.returncode != 0 or not private_path.is_file():
            detail = f"{completed.stdout}\n{completed.stderr}".strip()
            raise RuntimeError(detail or "Could not create the SSH invitation key")
    if not public_path.is_file():
        completed = runner(
            [str(keygen), "-y", "-f", str(private_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            **hidden_subprocess_kwargs(),
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            raise RuntimeError(completed.stderr.strip() or "Could not recover the SSH public key")
        public_path.write_text(completed.stdout.strip() + "\n", encoding="utf-8")
    _restrict_private_key(private_path, runner=runner)
    private_key = _validate_private_key(private_path.read_text(encoding="utf-8"))
    public_key = public_path.read_text(encoding="utf-8").strip()
    if not public_key.startswith("ssh-ed25519 ") or "\n" in public_key or "\r" in public_key or len(public_key) > 4096:
        raise ValueError("Invalid OpenSSH public key")
    fingerprint = hashlib.sha256(public_key.encode("utf-8")).hexdigest()[:16]
    return SshInviteKey(private_path, public_path, private_key, public_key, fingerprint)


def store_invitation_private_key(private_key: str, directory: Path) -> Path:
    """Materialize an embedded invitation key without keeping it in app config."""
    normalized = _validate_private_key(private_key)
    fingerprint = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"invite-{fingerprint}-ed25519"
    if not destination.is_file() or destination.read_text(encoding="utf-8") != normalized:
        destination.write_text(normalized, encoding="utf-8")
    _restrict_private_key(destination)
    return destination


def _host_setup_script(public_key: str, ssh_user: str, result_path: Path) -> str:
    key64 = base64.b64encode(public_key.encode("utf-8")).decode("ascii")
    user64 = base64.b64encode(ssh_user.encode("utf-8")).decode("ascii")
    result64 = base64.b64encode(str(result_path).encode("utf-8")).decode("ascii")
    return rf"""$ErrorActionPreference = 'Stop'
$key = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{key64}')).Trim()
$userName = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{user64}')).Trim()
$resultPath = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{result64}'))
try {{
    $client = Get-WindowsCapability -Online -Name 'OpenSSH.Client*' | Select-Object -First 1
    if ($client.State -ne 'Installed') {{ Add-WindowsCapability -Online -Name $client.Name | Out-Null }}
    $server = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
    if ($server.State -ne 'Installed') {{ Add-WindowsCapability -Online -Name $server.Name | Out-Null }}
    Start-Service sshd
    Set-Service -Name sshd -StartupType Automatic
    if (-not (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue)) {{
        New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server (sshd)' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 | Out-Null
    }}
    $account = New-Object System.Security.Principal.NTAccount($userName)
    $sid = $account.Translate([System.Security.Principal.SecurityIdentifier]).Value
    $profile = Get-CimInstance Win32_UserProfile | Where-Object {{ $_.SID -eq $sid }} | Select-Object -First 1
    if (-not $profile.LocalPath) {{ throw "Windows profile for $userName was not found" }}
    $sshd = Join-Path $env:WINDIR 'System32\OpenSSH\sshd.exe'
    $effective = & $sshd -T -C "user=$userName,host=$env:COMPUTERNAME,addr=127.0.0.1" 2>$null
    $setting = ($effective | Where-Object {{ $_ -like 'authorizedkeysfile *' }} | Select-Object -First 1) -replace '^authorizedkeysfile\s+', ''
    $keyFile = ($setting -split '\s+')[0]
    if (-not $keyFile) {{ $keyFile = '.ssh/authorized_keys' }}
    $keyFile = $keyFile.Replace('__PROGRAMDATA__', $env:ProgramData).Replace('%h', $profile.LocalPath)
    if (-not [IO.Path]::IsPathRooted($keyFile)) {{ $keyFile = Join-Path $profile.LocalPath $keyFile }}
    $keyFolder = Split-Path -Parent $keyFile
    New-Item -ItemType Directory -Path $keyFolder -Force | Out-Null
    $existing = if (Test-Path -LiteralPath $keyFile) {{ Get-Content -LiteralPath $keyFile -ErrorAction SilentlyContinue }} else {{ @() }}
    if ($existing -notcontains $key) {{
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [IO.File]::AppendAllText($keyFile, $key + [Environment]::NewLine, $utf8NoBom)
    }}
    if ($keyFile.StartsWith($env:ProgramData, [StringComparison]::OrdinalIgnoreCase)) {{
        & icacls.exe $keyFile /inheritance:r /grant:r '*S-1-5-18:(F)' /grant:r '*S-1-5-32-544:(F)' | Out-Null
    }} else {{
        & icacls.exe $keyFile /inheritance:r /grant:r "*$($sid):(F)" /grant:r '*S-1-5-18:(F)' | Out-Null
    }}
    Set-Content -LiteralPath $resultPath -Value 'OK' -Encoding utf8
}} catch {{
    Set-Content -LiteralPath $resultPath -Value ('ERROR:' + $_.Exception.Message) -Encoding utf8
    exit 1
}}
"""


def configure_openssh_host(public_key: str, ssh_user: str, timeout: float = 180.0) -> None:
    """Elevate once, enable Windows OpenSSH and authorize a generated group key."""
    if os.name != "nt":
        raise OSError("Automatic OpenSSH host setup is available on Windows only")
    if not ssh_user.strip():
        raise ValueError("SSH user is required")
    token = uuid.uuid4().hex
    temp = Path(tempfile.gettempdir())
    script = temp / f"blender_watchdog_ssh_host_{token}.ps1"
    result_path = temp / f"blender_watchdog_ssh_host_{token}.result"
    script.write_text(_host_setup_script(public_key, ssh_user, result_path), encoding="utf-8-sig")
    import ctypes

    arguments = f'-NoProfile -ExecutionPolicy Bypass -File "{script}"'
    result = ctypes.windll.shell32.ShellExecuteW(None, "runas", "powershell.exe", arguments, None, 1)
    if int(result) <= 32:
        raise PermissionError("Windows administrator approval was cancelled")
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            if result_path.is_file():
                outcome = result_path.read_text(encoding="utf-8-sig").strip()
                if outcome == "OK":
                    return
                raise RuntimeError(outcome.removeprefix("ERROR:") or "OpenSSH host setup failed")
            time.sleep(0.25)
        raise TimeoutError("OpenSSH host setup timed out")
    finally:
        for path in (script, result_path):
            try:
                path.unlink()
            except OSError:
                pass


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
