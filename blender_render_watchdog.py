#!/usr/bin/env python3
"""
Simple Blender render watchdog.

Run this script, choose a .blend file, choose the folder with rendered frames,
and it will start Blender. If Blender crashes, the script finds the latest
rendered frame in that folder and restarts Blender from the next frame.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from access_codes import (
    ACCESS_MODE_PERSISTENT,
    MobileSyncCode,
    generate_access_key,
    normalize_access_mode,
    resolve_service_access,
    validate_access_key,
)
from auto_fix import AutoFixIssue, apply_safe_fixes, inspect_render_setup
from appearance import THEME_LABELS, build_palette, normalize_color, normalize_theme
from device_groups import DeviceCapabilities, GroupRegistry, RenderGroup, infer_compute_backends
from glass_ui import ConnectionStatusIcon, GlassCard, GlassTabView, GlassWidgetFactory
from localization import LANGUAGE_LABELS, language_code_from_label, normalize_language, translate
from lan_discovery import DiscoveredController, LanDiscoveryAdvertiser, discover_controllers
from mobile_dashboard import MobileDashboardServer
from network_render import MAX_WORKERS, WORKER_OFFLINE_SECONDS, NetworkWorker, PairingCode, RenderCoordinator, request_pairing
from process_utils import hidden_subprocess_kwargs
from render_analytics import RenderHistory, RenderSession, estimate_render
from render_queue import RenderJob, RenderQueue
from resume_startup import (
    RESUME_FILE_NAME,
    arm_resume,
    build_launch_command,
    clear_resume_artifacts,
    load_resume_state,
    mark_resume_attempt,
    windows_startup_dir,
)
from ssh_support import (
    OpenSshState,
    SshTunnel,
    configure_openssh_host,
    ensure_ssh_invite_key,
    launch_openssh_install,
    query_openssh_state,
    store_invitation_private_key,
)
from render_sandbox import SandboxVariant, recommend_variant, run_sandbox
from video_tools import VIDEO_FORMATS, compose_video, video_output_path


IMAGE_EXTENSIONS = {
    ".bmp",
    ".cin",
    ".dpx",
    ".exr",
    ".hdr",
    ".jpeg",
    ".jpg",
    ".jp2",
    ".png",
    ".rgb",
    ".tga",
    ".tif",
    ".tiff",
    ".webp",
}

def app_config_dir() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
        return base / "BlenderRenderWatchdog"
    return Path.home() / ".config" / "BlenderRenderWatchdog"


LEGACY_CONFIG_PATH = Path(__file__).with_name("blender_render_watchdog_config.json")
CONFIG_PATH = app_config_dir() / "blender_render_watchdog_config.json"
QUEUE_PATH = app_config_dir() / "render_queue.json"
HISTORY_PATH = app_config_dir() / "render_history.json"
GROUPS_PATH = app_config_dir() / "render_groups.json"
RESUME_STATE_PATH = app_config_dir() / "unfinished_render.json"
COMPUTE_BACKENDS = ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL")
APP_VERSION = "3.0.0"
DEFAULT_GITHUB_REPOSITORY = "prostoodin1/BlenderRenderWatchdog"
DEFAULT_UPDATE_MANIFEST_URL = f"https://raw.githubusercontent.com/{DEFAULT_GITHUB_REPOSITORY}/main/update_manifest.json"
DEFAULT_RELEASE_EXE_URL = f"https://github.com/{DEFAULT_GITHUB_REPOSITORY}/releases/latest/download/BlenderRenderWatchdog.exe"


def unique_existing_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()

    for path in paths:
        try:
            normalized = str(path.expanduser().resolve()).lower()
        except OSError:
            normalized = str(path).lower()

        if normalized in seen or not path.exists():
            continue

        seen.add(normalized)
        result.append(path)

    return result


def load_config() -> dict[str, str]:
    config_path = CONFIG_PATH if CONFIG_PATH.exists() else LEGACY_CONFIG_PATH
    if not config_path.exists():
        return {}

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(data, dict):
        return {}

    return {str(key): str(value) for key, value in data.items() if value}


def save_config(config: dict[str, str]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def load_trusted_network_devices(value: str) -> dict[str, str]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        str(token): str(name)[:80]
        for token, name in data.items()
        if len(str(token)) >= 24 and str(name).strip()
    }


def encode_trusted_network_devices(devices: dict[str, str]) -> str:
    return json.dumps(devices, ensure_ascii=False, separators=(",", ":"))


def load_saved_network_connections(value: str) -> dict[str, str]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(controller_id): str(code) for controller_id, code in data.items() if str(code).startswith("BRW")}


def send_notification(title: str, message: str) -> None:
    if os.name != "nt":
        return

    safe_title = title.replace("'", "''")
    safe_message = message.replace("'", "''")
    script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$notify = New-Object System.Windows.Forms.NotifyIcon
$notify.Icon = [System.Drawing.SystemIcons]::Information
$notify.BalloonTipTitle = '{safe_title}'
$notify.BalloonTipText = '{safe_message}'
$notify.Visible = $true
$notify.ShowBalloonTip(5000)
Start-Sleep -Seconds 6
$notify.Dispose()
"""
    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **hidden_subprocess_kwargs(),
        )
    except Exception:
        pass

    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass



def stable_hardware_snapshot(
    previous_cpu: str,
    previous_gpus: list[str],
    detected_cpu: str,
    detected_gpus: list[str],
) -> tuple[str, list[str]]:
    """Keep the last real hardware list when a transient Windows query fails."""
    invalid_gpu_labels = {"", "No GPU detected by Windows"}
    usable_gpus = [gpu.strip() for gpu in detected_gpus if gpu.strip() not in invalid_gpu_labels]
    previous_usable = [gpu.strip() for gpu in previous_gpus if gpu.strip() not in invalid_gpu_labels]
    cpu = detected_cpu.strip()
    if not cpu or cpu == "Unknown CPU":
        cpu = previous_cpu.strip() or "Unknown CPU"
    gpus = usable_gpus or previous_usable or ["No GPU detected by Windows"]
    return cpu, gpus


def detect_hardware() -> tuple[str, list[str]]:
    cpu = platform.processor() or platform.machine() or "Unknown CPU"
    gpus: list[str] = []

    if os.name == "nt":
        try:
            command = [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name; "
                "$gpus = @(Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name); "
                "@{cpu=$cpu;gpus=$gpus} | ConvertTo-Json -Compress",
            ]
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                **hidden_subprocess_kwargs(),
            )
            if completed.returncode == 0:
                hardware = json.loads(completed.stdout.strip().lstrip("\ufeff"))
                if isinstance(hardware, dict):
                    detected_cpu = str(hardware.get("cpu") or "").strip()
                    if detected_cpu:
                        cpu = detected_cpu
                    raw_gpus = hardware.get("gpus") or []
                    if isinstance(raw_gpus, str):
                        raw_gpus = [raw_gpus]
                    if isinstance(raw_gpus, list):
                        gpus = [str(item).strip() for item in raw_gpus if str(item).strip()]
        except Exception:
            gpus = []

        if not gpus:
            try:
                completed = subprocess.run(
                    ["wmic", "path", "win32_VideoController", "get", "name"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=8,
                    **hidden_subprocess_kwargs(),
                )
                if completed.returncode == 0:
                    gpus = [
                        line.strip()
                        for line in completed.stdout.splitlines()
                        if line.strip() and line.strip().lower() != "name"
                    ]
            except Exception:
                pass

    if not gpus:
        gpus = ["No GPU detected by Windows"]

    return cpu, gpus


def create_device_script(
    use_cpu: bool,
    use_gpu: bool,
    optimize_options: dict[str, object] | None = None,
) -> Path:
    optimize_options = optimize_options or {}
    script = f'''
import bpy

USE_CPU = {use_cpu!r}
USE_GPU = {use_gpu!r}
OPTIMIZE = {optimize_options!r}
REQUESTED_BACKEND = str(OPTIMIZE.get("compute_backend", "AUTO")).upper()
BACKENDS = (REQUESTED_BACKEND,) if REQUESTED_BACKEND != "AUTO" else {tuple(COMPUTE_BACKENDS)!r}


def set_if_exists(target, name, value):
    if hasattr(target, name):
        try:
            setattr(target, name, value)
            print(f"[WATCHDOG] Set {{target.__class__.__name__}}.{{name}} = {{value}}")
            return True
        except Exception as error:
            print(f"[WATCHDOG] Could not set {{name}}: {{error}}")
    return False


scene = bpy.context.scene
if scene.render.engine != "CYCLES":
    print("[WATCHDOG] Render engine is not Cycles. CPU/GPU and optimization settings were not applied.")
else:
    prefs = bpy.context.preferences.addons["cycles"].preferences
    selected_backend = None

    if USE_GPU:
        for backend in BACKENDS:
            try:
                prefs.compute_device_type = backend
                prefs.get_devices()
                gpu_devices = [device for device in prefs.devices if device.type != "CPU"]
                if gpu_devices:
                    selected_backend = backend
                    break
            except Exception as error:
                print(f"[WATCHDOG] Backend {{backend}} unavailable: {{error}}")

    if USE_GPU and selected_backend:
        scene.cycles.device = "GPU"
        print(f"[WATCHDOG] Cycles backend: {{selected_backend}}")
        for device in prefs.devices:
            device.use = bool(device.type != "CPU" or USE_CPU)
            print(f"[WATCHDOG] Device {{device.name}} ({{device.type}}): {{'ON' if device.use else 'OFF'}}")
    else:
        if USE_GPU and not USE_CPU:
            raise RuntimeError("Requested GPU backend is unavailable on this computer")
        scene.cycles.device = "CPU"
        try:
            prefs.get_devices()
            for device in prefs.devices:
                device.use = bool(device.type == "CPU")
                print(f"[WATCHDOG] Device {{device.name}} ({{device.type}}): {{'ON' if device.use else 'OFF'}}")
        except Exception:
            pass
        print("[WATCHDOG] Cycles backend: CPU")

    if OPTIMIZE.get("enabled"):
        print("[WATCHDOG] Applying render optimization settings.")
        cycles = scene.cycles
        render = scene.render

        if OPTIMIZE.get("adaptive_sampling"):
            set_if_exists(cycles, "use_adaptive_sampling", True)
            set_if_exists(cycles, "adaptive_threshold", float(OPTIMIZE.get("adaptive_threshold", 0.02)))

        if OPTIMIZE.get("denoise"):
            set_if_exists(cycles, "use_denoising", True)
            set_if_exists(cycles, "denoiser", str(OPTIMIZE.get("denoiser", "OPENIMAGEDENOISE")))

        samples = int(OPTIMIZE.get("samples", 0) or 0)
        if samples > 0:
            set_if_exists(cycles, "samples", samples)
            set_if_exists(cycles, "preview_samples", max(16, min(samples, 64)))

        if OPTIMIZE.get("persistent_data"):
            set_if_exists(render, "use_persistent_data", True)

        if OPTIMIZE.get("fast_bounces"):
            set_if_exists(cycles, "max_bounces", int(OPTIMIZE.get("max_bounces", 6)))
            set_if_exists(cycles, "diffuse_bounces", int(OPTIMIZE.get("diffuse_bounces", 2)))
            set_if_exists(cycles, "glossy_bounces", int(OPTIMIZE.get("glossy_bounces", 3)))
            set_if_exists(cycles, "transmission_bounces", int(OPTIMIZE.get("transmission_bounces", 4)))
            set_if_exists(cycles, "transparent_max_bounces", int(OPTIMIZE.get("transparent_bounces", 4)))

        if OPTIMIZE.get("simplify"):
            set_if_exists(render, "use_simplify", True)
            set_if_exists(render, "simplify_subdivision_render", int(OPTIMIZE.get("simplify_subdivision", 1)))
            set_if_exists(render, "simplify_child_particles_render", float(OPTIMIZE.get("simplify_particles", 0.5)))
            set_if_exists(render, "simplify_volumes", float(OPTIMIZE.get("simplify_volumes", 0.5)))

        tile_size = int(OPTIMIZE.get("tile_size", 0) or 0)
        if tile_size > 0:
            set_if_exists(cycles, "tile_size", tile_size)

        resolution_percent = int(OPTIMIZE.get("resolution_percent", 100) or 100)
        if resolution_percent != 100:
            set_if_exists(render, "resolution_percentage", resolution_percent)
'''
    script_path = Path(tempfile.gettempdir()) / "blender_render_watchdog_devices.py"
    script_path.write_text(script, encoding="utf-8")
    return script_path

def query_frame_range(blender: Path, blend: Path, log: callable | None = None) -> tuple[int, int] | None:
    script = 'import bpy; s=bpy.context.scene; print("WATCHDOG_FRAME_RANGE:%s:%s" % (s.frame_start, s.frame_end))'
    script_path = Path(tempfile.gettempdir()) / "blender_render_watchdog_range.py"
    script_path.write_text(script, encoding="utf-8")

    try:
        completed = subprocess.run(
            [str(blender), "-b", str(blend), "--python", str(script_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            **hidden_subprocess_kwargs(),
        )
    except Exception as error:
        if log:
            log(f"[WATCHDOG] Could not read scene frame range: {error}")
        return None

    output = completed.stdout + "\n" + completed.stderr
    match = re.search(r"WATCHDOG_FRAME_RANGE:(-?\d+):(-?\d+)", output)
    if not match:
        if log:
            log("[WATCHDOG] Could not find scene frame range in Blender output.")
        return None

    return int(match.group(1)), int(match.group(2))


def version_tuple(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version)
    return tuple(int(part) for part in parts) if parts else (0,)


def is_newer_version(remote_version: str, local_version: str = APP_VERSION) -> bool:
    return version_tuple(remote_version) > version_tuple(local_version)


def default_update_source() -> str:
    return f"github:{DEFAULT_GITHUB_REPOSITORY}"


def normalize_update_source(source: str | None) -> str:
    value = (source or "").strip()
    if not value or "YOUR_USERNAME" in value or "YOUR_REPO" in value:
        return default_update_source()
    return value


def builtin_update_manifest(notes: str = "No published GitHub release found yet.") -> dict[str, object]:
    return {
        "version": APP_VERSION,
        "exe_url": DEFAULT_RELEASE_EXE_URL,
        "notes": notes,
    }


def normalize_update_manifest(manifest: dict[str, object]) -> dict[str, object]:
    version = str(manifest.get("version") or manifest.get("tag_name") or "").strip()
    if version.startswith("v"):
        version = version[1:]

    exe_url = str(manifest.get("exe_url") or "").strip()
    if not exe_url:
        exe_url = DEFAULT_RELEASE_EXE_URL

    notes = str(manifest.get("notes") or manifest.get("body") or "").strip()
    digest = str(manifest.get("sha256") or manifest.get("digest") or "").strip().lower()
    if digest.startswith("sha256:"):
        digest = digest.split(":", 1)[1]
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        digest = ""
    return {"version": version, "exe_url": exe_url, "notes": notes, "sha256": digest}


def fetch_github_release_manifest(repository: str) -> dict[str, object]:
    import urllib.request

    api_url = f"https://api.github.com/repos/{repository}/releases/latest"
    request = urllib.request.Request(api_url, headers={"User-Agent": "BlenderRenderWatchdog"})
    with urllib.request.urlopen(request, timeout=20) as response:
        release = json.loads(response.read().decode("utf-8"))

    if not isinstance(release, dict):
        raise ValueError("GitHub release response must be a JSON object.")

    exe_url = ""
    exe_digest = ""
    for asset in release.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").lower()
        if name == "blenderrenderwatchdog.exe" or name.endswith(".exe"):
            exe_url = str(asset.get("browser_download_url") or "")
            exe_digest = str(asset.get("digest") or "")
            break

    return normalize_update_manifest(
        {
            "version": release.get("tag_name") or release.get("name") or "",
            "exe_url": exe_url,
            "digest": exe_digest,
            "notes": release.get("body") or "",
        }
    )


def fetch_update_manifest(update_source: str | None = None) -> dict[str, object]:
    import urllib.error
    import urllib.request

    source = normalize_update_source(update_source)
    if source in ("builtin", "local"):
        return builtin_update_manifest()

    try:
        if source.startswith("github:"):
            repository = source.split(":", 1)[1].strip() or DEFAULT_GITHUB_REPOSITORY
            return fetch_github_release_manifest(repository)

        with urllib.request.urlopen(source, timeout=20) as response:
            data = response.read().decode("utf-8")
        manifest = json.loads(data)
        if not isinstance(manifest, dict):
            raise ValueError("Update manifest must be a JSON object.")
        return normalize_update_manifest(manifest)
    except urllib.error.HTTPError as error:
        if error.code in (404, 403):
            return builtin_update_manifest("GitHub update source is not published yet.")
        raise
    except OSError:
        return builtin_update_manifest("Network is unavailable; using local version information.")


def app_target_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable)
    return Path(__file__)


def resume_startup_file_path() -> Path:
    return windows_startup_dir() / RESUME_FILE_NAME


def resume_launch_command() -> str:
    if getattr(sys, "frozen", False):
        return build_launch_command(app_target_path())
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return build_launch_command(pythonw if pythonw.exists() else Path(sys.executable), app_target_path())


def update_check_command_path() -> Path:
    return app_target_path().parent / "Check Update.cmd"


def write_update_check_cmd() -> Path:
    cmd_path = update_check_command_path()
    if getattr(sys, "frozen", False):
        launch_command = f'"{app_target_path()}" --check-update --install-update'
    else:
        launch_command = f'"{sys.executable}" "{Path(__file__)}" --check-update --install-update'

    cmd_path.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        "title Blender Render Watchdog Update\r\n"
        "echo Checking Blender Render Watchdog updates...\r\n"
        f"{launch_command}\r\n"
        "echo.\r\n"
        "pause\r\n",
        encoding="utf-8",
    )
    return cmd_path


def check_update_cli(update_source: str | None, install: bool) -> int:
    source = normalize_update_source(update_source or load_config().get("update_manifest_url"))
    print(f"Current version: {APP_VERSION}", flush=True)
    print(f"Update source: {source}", flush=True)

    try:
        manifest = fetch_update_manifest(source)
        version = str(manifest.get("version") or "").strip()
        if not version:
            raise ValueError("Update manifest does not contain version.")
    except Exception as error:
        print(f"Update check failed: {error}", flush=True)
        return 1

    if not is_newer_version(version):
        print(f"Already latest: {APP_VERSION}", flush=True)
        return 0

    print(f"Update available: {version}", flush=True)
    print(f"Download: {manifest.get('exe_url')}", flush=True)
    if not install:
        return 2

    try:
        install_update_from_manifest(manifest)
    except Exception as error:
        print(f"Update install failed: {error}", flush=True)
        return 1

    print("Updater started. This window can be closed after the app restarts.", flush=True)
    return 0


def powershell_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def build_update_script(
    exe_url: str,
    temp_exe: Path,
    target: Path,
    current_pid: int,
    expected_sha256: str = "",
) -> str:
    target_directory = target.parent
    backup = target.with_suffix(target.suffix + ".previous")
    expected_sha256 = expected_sha256.lower() if re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) else ""
    return f'''$ErrorActionPreference = "Stop"
$Url = {powershell_literal(exe_url)}
$TempExe = {powershell_literal(temp_exe)}
$Target = {powershell_literal(target)}
$TargetDirectory = {powershell_literal(target_directory)}
$Backup = {powershell_literal(backup)}
$ExpectedSha256 = {powershell_literal(expected_sha256)}
$PidToWait = {int(current_pid)}
Invoke-WebRequest -Uri $Url -OutFile $TempExe -UseBasicParsing
if ((Get-Item -LiteralPath $TempExe).Length -lt 1000000) {{ throw "Downloaded executable is unexpectedly small" }}
if ($ExpectedSha256) {{
    $ActualSha256 = (Get-FileHash -LiteralPath $TempExe -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualSha256 -ne $ExpectedSha256) {{ throw "Downloaded executable checksum does not match" }}
}}
for ($Attempt = 0; $Attempt -lt 90; $Attempt++) {{
    if (-not (Get-Process -Id $PidToWait -ErrorAction SilentlyContinue)) {{ break }}
    Start-Sleep -Seconds 1
}}
Start-Sleep -Seconds 3
if (Test-Path -LiteralPath $Backup) {{ Remove-Item -LiteralPath $Backup -Force }}
Copy-Item -LiteralPath $Target -Destination $Backup -Force
$Installed = $false
for ($Attempt = 0; $Attempt -lt 30; $Attempt++) {{
    try {{
        Move-Item -LiteralPath $TempExe -Destination $Target -Force
        $Installed = $true
        break
    }} catch {{
        Start-Sleep -Seconds 1
    }}
}}
if (-not $Installed) {{ throw "Could not replace the running executable" }}
Start-Sleep -Seconds 4
$env:PYINSTALLER_RESET_ENVIRONMENT = "1"
$Started = Start-Process -FilePath $Target -WorkingDirectory $TargetDirectory -PassThru
Start-Sleep -Seconds 6
if ($Started.HasExited) {{
    Copy-Item -LiteralPath $Backup -Destination $Target -Force
    Start-Process -FilePath $Target -WorkingDirectory $TargetDirectory
    throw "Updated app failed to start; the previous version was restored"
}}
Remove-Item -LiteralPath $Backup -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
'''


def install_update_from_manifest(manifest: dict[str, object]) -> None:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Auto-install updates is available only in the exe build.")

    exe_url = str(manifest.get("exe_url") or DEFAULT_RELEASE_EXE_URL).strip()
    if not exe_url:
        raise ValueError("Update manifest does not contain exe_url.")

    target = app_target_path()
    update_id = uuid.uuid4().hex
    temp_exe = Path(tempfile.gettempdir()) / f"BlenderRenderWatchdog_update_{update_id}.exe"
    updater_script = Path(tempfile.gettempdir()) / f"BlenderRenderWatchdog_apply_update_{update_id}.ps1"
    current_pid = os.getpid()
    script = build_update_script(
        exe_url,
        temp_exe,
        target,
        current_pid,
        str(manifest.get("sha256") or ""),
    )
    updater_script.write_text(script, encoding="utf-8")
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(updater_script),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **hidden_subprocess_kwargs(),
    )


def schedule_system_shutdown(seconds: int = 60) -> None:
    if os.name != "nt":
        return
    try:
        subprocess.Popen(
            [
                "shutdown",
                "/s",
                "/t",
                str(seconds),
                "/c",
                "Blender Render Watchdog: render finished successfully.",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **hidden_subprocess_kwargs(),
        )
    except Exception:
        pass

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restart Blender render after crash and continue from the last frame."
    )
    parser.add_argument("--blender", help="Path to blender.exe. Optional.")
    parser.add_argument("--blend", help="Path to the .blend file. Optional.")
    parser.add_argument("--frames", help="Folder where rendered frames are saved. Optional.")
    parser.add_argument(
        "--sleep",
        type=int,
        default=10,
        help="Seconds to wait before restarting Blender after a crash.",
    )
    parser.add_argument(
        "--padding",
        type=int,
        default=4,
        help="Frame number padding for Blender output path, default: 4.",
    )
    parser.add_argument(
        "--start",
        type=int,
        default=None,
        help="Start frame if no frames exist in the selected folder.",
    )
    parser.add_argument(
        "--end",
        type=int,
        default=None,
        help="Optional end frame. If omitted, Blender uses the scene end frame.",
    )
    parser.add_argument(
        "--extra",
        nargs=argparse.REMAINDER,
        default=[],
        help="Extra Blender arguments, placed before -a.",
    )
    parser.add_argument("--check-update", action="store_true", help="Check GitHub for a newer app version and exit.")
    parser.add_argument("--install-update", action="store_true", help="Install update when used with --check-update.")
    parser.add_argument("--update-source", default=None, help="Update source, default: github:prostoodin1/BlenderRenderWatchdog.")
    parser.add_argument("--write-update-cmd", action="store_true", help="Create Check Update.cmd next to the app and exit.")
    parser.add_argument("--worker-code", default=None, help="Run as a network worker using a BRW2 or BRW4 connection code.")
    parser.add_argument("--worker-name", default=None, help="Display name used in network worker mode.")
    parser.add_argument("--resume-unfinished", action="store_true", help="Resume the render saved by Windows startup recovery.")
    return parser.parse_args()


def choose_file(title: str, filetypes: list[tuple[str, str]]) -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        value = input(f"{title}: ").strip().strip('"')
        return Path(value) if value else None

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    value = filedialog.askopenfilename(title=title, filetypes=filetypes)
    root.destroy()
    return Path(value) if value else None


def choose_folder(title: str) -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        value = input(f"{title}: ").strip().strip('"')
        return Path(value) if value else None

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    value = filedialog.askdirectory(title=title)
    root.destroy()
    return Path(value) if value else None


def steam_roots_from_registry() -> list[Path]:
    if os.name != "nt":
        return []

    try:
        import winreg
    except Exception:
        return []

    roots: list[Path] = []
    registry_locations = [
        (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Valve\Steam"),
    ]

    for hive, key_path in registry_locations:
        try:
            with winreg.OpenKey(hive, key_path) as key:
                for value_name in ("SteamPath", "InstallPath"):
                    try:
                        value, _ = winreg.QueryValueEx(key, value_name)
                    except FileNotFoundError:
                        continue
                    if value:
                        roots.append(Path(str(value).replace("/", "\\")))
        except OSError:
            continue

    return roots


def steam_roots() -> list[Path]:
    candidates = steam_roots_from_registry()

    for base in (
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramFiles"),
    ):
        if base:
            candidates.append(Path(base) / "Steam")

    return unique_existing_paths(candidates)


def steam_library_paths(steam_root: Path) -> list[Path]:
    libraries = [steam_root]
    library_file = steam_root / "steamapps" / "libraryfolders.vdf"

    if not library_file.exists():
        return unique_existing_paths(libraries)

    try:
        text = library_file.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return unique_existing_paths(libraries)

    for value in re.findall(r'"path"\s+"([^"]+)"', text):
        libraries.append(Path(value.replace("\\\\", "\\")))

    for value in re.findall(r'"\d+"\s+"([^"]+)"', text):
        libraries.append(Path(value.replace("\\\\", "\\")))

    return unique_existing_paths(libraries)


def steam_blender_candidates() -> list[Path]:
    candidates: list[Path] = []

    for steam_root in steam_roots():
        for library in steam_library_paths(steam_root):
            common = library / "steamapps" / "common"
            candidates.append(common / "Blender" / "blender.exe")
            if common.exists():
                candidates.extend(common.glob("Blender*/blender.exe"))

    return candidates


def find_blender(ask_if_missing: bool = True) -> Path | None:
    env_path = os.environ.get("BLENDER_EXE")
    if env_path and Path(env_path).exists():
        return Path(env_path)

    path_from_shell = shutil.which("blender")
    if path_from_shell:
        return Path(path_from_shell)

    candidates: list[Path] = steam_blender_candidates()
    for base in (
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
    ):
        if not base:
            continue
        blender_root = Path(base) / "Blender Foundation"
        if blender_root.exists():
            candidates.extend(blender_root.glob("Blender */blender.exe"))

    existing = unique_existing_paths(candidates)
    if existing:
        return sorted(existing, reverse=True)[0]

    if not ask_if_missing:
        return None

    return choose_file("Choose blender.exe", [("Blender executable", "blender.exe"), ("EXE", "*.exe")])


def find_last_frame(
    frames_folder: Path,
    min_frame: int | None = None,
    max_frame: int | None = None,
) -> int | None:
    latest_frame: int | None = None

    if not frames_folder.exists():
        return None

    for file_path in frames_folder.iterdir():
        if not file_path.is_file() or file_path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue

        frame = frame_number_from_path(file_path)
        if frame is None:
            continue

        if min_frame is not None and frame < min_frame:
            continue
        if max_frame is not None and frame > max_frame:
            continue

        if latest_frame is None or frame > latest_frame:
            latest_frame = frame

    return latest_frame


def frame_number_from_path(file_path: Path) -> int | None:
    numbers = re.findall(r"\d+", file_path.stem)
    if not numbers:
        return None

    return int(numbers[-1])


def rendered_frame_files(frames_folder: Path) -> dict[str, Path]:
    frames: dict[str, Path] = {}

    if not frames_folder.exists():
        return frames

    for file_path in frames_folder.iterdir():
        if not file_path.is_file() or file_path.suffix.lower() not in IMAGE_EXTENSIONS:
            continue

        try:
            key = str(file_path.resolve()).lower()
        except OSError:
            key = str(file_path).lower()

        frames[key] = file_path

    return frames



def query_scene_settings(blender: Path, blend: Path, log: callable | None = None) -> dict[str, object] | None:
    script = r'''
import bpy
import json
import os
scene = bpy.context.scene
missing = []
for image in bpy.data.images:
    path = bpy.path.abspath(image.filepath) if image.filepath else ""
    if image.source == "FILE" and path and not os.path.exists(path):
        missing.append(path)
for library in bpy.data.libraries:
    path = bpy.path.abspath(library.filepath) if library.filepath else ""
    if path and not os.path.exists(path):
        missing.append(path)
print("WATCHDOG_SCENE_SETTINGS:" + json.dumps({
    "frame_start": scene.frame_start,
    "frame_end": scene.frame_end,
    "output_path": bpy.path.abspath(scene.render.filepath),
    "resolution_x": scene.render.resolution_x,
    "resolution_y": scene.render.resolution_y,
    "resolution_percentage": scene.render.resolution_percentage,
    "engine": scene.render.engine,
    "samples": getattr(scene.cycles, "samples", 0),
    "fps": scene.render.fps / max(1.0, scene.render.fps_base),
    "file_format": scene.render.image_settings.file_format,
    "missing_external_files": missing,
}))
'''
    script_path = Path(tempfile.gettempdir()) / "blender_render_watchdog_scene_settings.py"
    script_path.write_text(script, encoding="utf-8")

    try:
        completed = subprocess.run(
            [str(blender), "-b", str(blend), "--python", str(script_path)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
            **hidden_subprocess_kwargs(),
        )
    except Exception as error:
        if log:
            log(f"[WATCHDOG] Could not read scene settings: {error}")
        return None

    output = completed.stdout + "\n" + completed.stderr
    match = re.search(r"WATCHDOG_SCENE_SETTINGS:(\{.*\})", output)
    if not match:
        if log:
            log("[WATCHDOG] Could not find scene settings in Blender output.")
        return None

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as error:
        if log:
            log(f"[WATCHDOG] Could not parse scene settings: {error}")
        return None

    return data if isinstance(data, dict) else None


def output_folder_from_scene_path(output_path: str, blend: Path) -> Path:
    if not output_path:
        return blend.parent

    normalized = output_path.replace("/", os.sep)
    if output_path.endswith(("/", "\\")):
        return Path(normalized)

    path = Path(normalized)
    parent = path.parent
    if str(parent) in ("", "."):
        return blend.parent
    return parent

def build_output_pattern(frames_folder: Path, padding: int) -> str:
    hashes = "#" * padding
    return str(frames_folder / f"frame_{hashes}")


def run_blender(
    blender: Path,
    blend: Path,
    frames_folder: Path,
    start_frame: int | None,
    end_frame: int | None,
    padding: int,
    extra_args: list[str],
    device_script: Path | None = None,
) -> int:
    command = [
        str(blender),
        "-b",
        str(blend),
    ]

    if frames_folder is not None:
        command.extend(["-o", build_output_pattern(frames_folder, padding)])

    if start_frame is not None:
        command.extend(["-s", str(start_frame)])

    if end_frame is not None:
        command.extend(["-e", str(end_frame)])

    command.extend(extra_args)

    if device_script is not None:
        command.extend(["--python", str(device_script)])

    command.append("-a")

    print("\nStarting Blender:")
    print(" ".join(f'"{part}"' if " " in part else part for part in command), flush=True)

    completed = subprocess.run(command)
    return completed.returncode


def build_blender_command(
    blender: Path,
    blend: Path,
    frames_folder: Path | None,
    start_frame: int | None,
    end_frame: int | None,
    padding: int,
    extra_args: list[str],
    device_script: Path | None = None,
) -> list[str]:
    command = [
        str(blender),
        "-b",
        str(blend),
    ]

    if frames_folder is not None:
        command.extend(["-o", build_output_pattern(frames_folder, padding)])

    if start_frame is not None:
        command.extend(["-s", str(start_frame)])

    if end_frame is not None:
        command.extend(["-e", str(end_frame)])

    command.extend(extra_args)

    if device_script is not None:
        command.extend(["--python", str(device_script)])

    command.append("-a")
    return command


def format_command(command: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in command)

def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def run_blender_process(
    command: list[str],
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    frames_folder: Path | None = None,
    known_frames: dict[str, Path] | None = None,
    log: callable | None = None,
    on_frame_rendered: callable | None = None,
) -> int:
    blender_saved_frame = threading.Event()

    def write(message: str) -> None:
        if log:
            log(message)
        else:
            print(message, flush=True)

    def read_output() -> None:
        if process.stdout is None:
            return

        for line in process.stdout:
            text = line.rstrip()
            if text:
                write(text)
                lower_text = text.lower()
                if "saved:" in lower_text or "writing:" in lower_text:
                    blender_saved_frame.set()

    def terminate_process() -> int:
        process.terminate()
        try:
            return_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait()
        output_thread.join(timeout=2)
        log_new_frames()
        return return_code

    def log_new_frames() -> bool:
        if frames_folder is None or known_frames is None:
            return False

        current_frames = rendered_frame_files(frames_folder)
        new_keys = [key for key in current_frames if key not in known_frames]
        new_files = [current_frames[key] for key in new_keys]
        new_files.sort(key=lambda path: (frame_number_from_path(path) or -1, path.name.lower()))

        for file_path in new_files:
            frame_number = frame_number_from_path(file_path)
            if frame_number is None:
                write(f"[FRAME] Rendered: {file_path.name}")
            else:
                write(f"[FRAME] Rendered frame {frame_number}: {file_path.name}")
                if on_frame_rendered:
                    on_frame_rendered(frame_number, file_path)

        known_frames.update(current_frames)
        return bool(new_files)

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **hidden_subprocess_kwargs(),
    )
    output_thread = threading.Thread(target=read_output, daemon=True)
    output_thread.start()

    while True:
        return_code = process.poll()
        if return_code is not None:
            output_thread.join(timeout=2)
            log_new_frames()
            return return_code

        if stop_event and stop_event.is_set():
            return terminate_process()

        new_frame_found = log_new_frames()
        frame_finished = new_frame_found or blender_saved_frame.is_set()
        if frame_finished:
            blender_saved_frame.clear()
        if pause_event and pause_event.is_set() and frame_finished:
            write("[WATCHDOG] Pause requested. Stopping after current frame.")
            terminate_process()
            return 131

        time.sleep(1)


def run_watchdog(
    blender: Path,
    blend: Path,
    frames_folder: Path,
    sleep_seconds: int = 10,
    padding: int = 4,
    start: int | None = None,
    end: int | None = None,
    extra_args: list[str] | None = None,
    stop_event: threading.Event | None = None,
    pause_event: threading.Event | None = None,
    log: callable | None = None,
    progress: callable | None = None,
    use_cpu: bool = True,
    use_gpu: bool = True,
    optimize_options: dict[str, object] | None = None,
    output_override: bool = True,
    max_restarts: int | None = None,
    frame_observer: callable | None = None,
) -> int:
    def write(message: str) -> None:
        if log:
            log(message)
        else:
            print(message, flush=True)

    def update_progress(done_frames: int, total_frames: int | None, text: str) -> None:
        if not progress:
            return

        if not total_frames or total_frames <= 0:
            progress(0.0, text)
            return

        percent = max(0.0, min(100.0, (done_frames / total_frames) * 100.0))
        progress(percent, text)

    extra_args = extra_args or []
    frames_folder.mkdir(parents=True, exist_ok=True)

    scene_range = query_frame_range(blender, blend, log=write)
    scene_start = scene_range[0] if scene_range else None
    scene_end = scene_range[1] if scene_range else None
    effective_end = end if end is not None else scene_end
    progress_start: int | None = None
    total_frames: int | None = None
    render_timer_start: float | None = None
    restart_count = 0

    device_script = create_device_script(use_cpu=use_cpu, use_gpu=use_gpu, optimize_options=optimize_options)

    write("Watchdog is running. Close this window only if you want to stop it.")
    write(f"Blender executable: {blender}")
    write(f"Blend file: {blend}")
    write(f"Frames folder: {frames_folder}")
    write(f"Output path: {'manual override' if output_override else '.blend file setting'}")
    write(f"Render devices: CPU={'ON' if use_cpu else 'OFF'}, GPU={'ON' if use_gpu else 'OFF'}")
    if scene_range:
        write(f"Scene frame range: {scene_start}-{scene_end}")

    while not (stop_event and stop_event.is_set()):
        last_frame = find_last_frame(
            frames_folder,
            min_frame=start if start is not None else scene_start,
            max_frame=effective_end,
        )
        if last_frame is None:
            start_frame = start if start is not None else scene_start
            if start_frame is None:
                write("No rendered frames found. Starting from the scene start frame.")
            else:
                write(f"No rendered frames found. Starting from frame {start_frame}.")
        else:
            start_frame = last_frame + 1
            write(f"Last rendered frame found: {last_frame}. Starting from {start_frame}.")

        if start_frame is not None and effective_end is not None and start_frame > effective_end:
            update_progress(1, 1, "Render complete")
            write("Render is already complete.")
            return 0

        if progress_start is None:
            progress_start = start_frame
            if progress_start is not None and effective_end is not None:
                total_frames = max(1, effective_end - progress_start + 1)
                update_progress(0, total_frames, f"0 / {total_frames} frames")
            else:
                update_progress(0, None, "Rendering")

        if render_timer_start is None:
            render_timer_start = time.monotonic()

        def on_frame_rendered(frame_number: int, file_path: Path) -> None:
            if frame_observer:
                frame_observer(frame_number, file_path)
            if progress_start is None or effective_end is None or total_frames is None:
                update_progress(0, None, f"Rendered frame {frame_number}")
                return

            done = max(0, min(total_frames, frame_number - progress_start + 1))
            elapsed = time.monotonic() - (render_timer_start or time.monotonic())
            average = elapsed / done if done else 0
            remaining = max(0, total_frames - done)
            eta = average * remaining
            details = f"{done} / {total_frames} frames · avg {format_duration(average)} · ETA {format_duration(eta)}"
            update_progress(done, total_frames, details)

        command = build_blender_command(
            blender=blender,
            blend=blend,
            frames_folder=frames_folder if output_override else None,
            start_frame=start_frame,
            end_frame=effective_end,
            padding=padding,
            extra_args=extra_args,
            device_script=device_script,
        )

        write("")
        write("Starting Blender:")
        write(format_command(command))

        known_frames = rendered_frame_files(frames_folder)
        return_code = run_blender_process(
            command,
            stop_event=stop_event,
            pause_event=pause_event,
            frames_folder=frames_folder,
            known_frames=known_frames,
            log=write,
            on_frame_rendered=on_frame_rendered,
        )

        if pause_event and pause_event.is_set():
            write("Watchdog paused after current frame.")
            return 131

        if stop_event and stop_event.is_set():
            write("Watchdog stopped by user.")
            return 130

        if return_code == 0:
            update_progress(1, 1, "Render complete")
            write("")
            write("Blender finished normally. Watchdog stopped.")
            return 0

        restart_count += 1
        if max_restarts is not None and restart_count > max_restarts:
            write(
                f"[WATCHDOG] Blender failed {restart_count} times. "
                "Restart limit reached."
            )
            return return_code

        write("")
        write(
            f"Blender crashed or closed with exit code {return_code}. "
            f"Restarting in {sleep_seconds} seconds."
        )

        for _ in range(sleep_seconds):
            if stop_event and stop_event.is_set():
                write("Watchdog stopped by user.")
                return 130
            time.sleep(1)

    write("Watchdog stopped by user.")
    return 130

def run_gui(args: argparse.Namespace) -> int:
    import tkinter as tk
    from tkinter import colorchooser, filedialog, messagebox, scrolledtext, simpledialog, ttk

    class WatchdogApp:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.root.title(f"Blender Render Watchdog {APP_VERSION}")
            self.root.geometry("1280x860")
            self.root.minsize(1080, 720)

            self.config = load_config()
            self.theme_code = normalize_theme(self.config.get("theme"))
            self.custom_accent = normalize_color(self.config.get("custom_accent"))
            self.colors = build_palette(self.theme_code, self.custom_accent)
            self.language_code = normalize_language(self.config.get("language"))
            self.localizable_widgets: list[object] = []
            self.localizable_headings: list[tuple[object, str, str]] = []
            self.localized_variables: dict[str, tuple[tk.StringVar, str, dict[str, object]]] = {}
            self.log_queue: queue.Queue[str | tuple[str, float, str]] = queue.Queue()
            self.stop_event: threading.Event | None = None
            self.pause_event: threading.Event | None = None
            self.worker: threading.Thread | None = None
            self.is_paused = False
            self.queue_running = False
            self.paused_queue = False
            self.active_queue_job_id: str | None = None
            self.render_queue = RenderQueue.load(QUEUE_PATH)
            self.group_registry = GroupRegistry.load(GROUPS_PATH)
            self.render_history = RenderHistory.load(HISTORY_PATH)
            self.network_controller: RenderCoordinator | None = None
            self.network_worker: NetworkWorker | None = None
            self.network_connect_attempt = ""
            self.lan_advertiser: LanDiscoveryAdvertiser | None = None
            self.discovered_controllers: dict[str, DiscoveredController] = {}
            self.lan_discovery_running = False
            self.last_lan_discovery_at = 0.0
            self.last_group_save_at = 0.0
            self.ssh_tunnel: SshTunnel | None = None
            self.network_device_dialog = None
            self.mobile_dashboard: MobileDashboardServer | None = None
            self.network_session: RenderSession | None = None
            self.network_history_saved = False
            self.openssh_state = OpenSshState(False, message="OpenSSH status has not been checked")
            self.openssh_status_running = False
            self.latest_frame_path: Path | None = None
            self.current_analysis_issues: list[AutoFixIssue] = []
            self.current_analysis_output: Path | None = None
            self.hardware_poll_running = False
            self.mobile_state_cache: dict[str, object] = {}
            self.glass_cards: list[GlassCard] = []
            self.card_reveal_index = 0
            self.progress_animation_id: str | None = None
            self.progress_animation_target = 0.0
            self.tab_scroll_canvases: list[object] = []
            self.tab_scroll_refreshers: list[object] = []
            self.mousewheel_bound = False
            self.current_render_frame: int | None = None
            self.render_frame_count = 0
            self.render_average_seconds = 0.0
            self.last_frame_observed_at: float | None = None

            saved_blender = args.blender or self.config.get("blender") or ""
            if not saved_blender:
                found_blender = find_blender(ask_if_missing=False)
                saved_blender = str(found_blender) if found_blender else ""

            cpu_name, gpu_names = detect_hardware()
            self.cpu_name = cpu_name
            self.gpu_names = gpu_names
            self.local_capabilities = DeviceCapabilities(
                cpu=cpu_name,
                gpus=gpu_names,
                compute_backends=infer_compute_backends(gpu_names, platform.system()),
                platform=f"{platform.system()} {platform.release()}",
            )
            if self.group_registry.active is None:
                migrated_group_name = self.config.get("network_group_name") or self.config.get("network_controller_name") or "My render group"
                migrated_security = "code" if self.config.get("network_require_pairing", "1") != "0" else "open"
                self.group_registry.create_group(migrated_group_name, migrated_security)
            active_group = self.group_registry.active
            assert active_group is not None
            if active_group.security_mode == "approval":
                active_group.security_mode = "code"
            active_group.register_device(
                self.group_registry.identity.device_id,
                platform.node() or "This PC",
                identity_fingerprint=self.group_registry.identity.fingerprint,
                capabilities=self.local_capabilities,
                role="coordinator" if active_group.owner_device_id == self.group_registry.identity.device_id else "worker",
            )
            self.group_registry.save(GROUPS_PATH)
            self.cpu_info_var = tk.StringVar(value=cpu_name)
            self.gpu_info_var = tk.StringVar(value="; ".join(gpu_names))

            self.blender_var = tk.StringVar(value=saved_blender)
            self.blend_var = tk.StringVar(value=args.blend or self.config.get("blend") or "")
            self.frames_var = tk.StringVar(value=args.frames or self.config.get("frames") or "")
            self.start_frame_var = tk.StringVar(value=str(args.start) if args.start is not None else self.config.get("start_frame", ""))
            self.end_frame_var = tk.StringVar(value=str(args.end) if args.end is not None else self.config.get("end_frame", ""))
            default_scene_range = "1" if not self.start_frame_var.get().strip() and not self.end_frame_var.get().strip() else "0"
            self.use_scene_range_var = tk.BooleanVar(value=(self.config.get("use_scene_range", default_scene_range) == "1"))
            self.use_scene_output_var = tk.BooleanVar(value=(self.config.get("use_scene_output", "0") == "1"))
            self.language_var = tk.StringVar(value=LANGUAGE_LABELS[self.language_code])
            self.theme_var = tk.StringVar()
            self.lightweight_motion_var = tk.BooleanVar(value=(self.config.get("lightweight_motion", "1") != "0"))
            self.resume_unfinished_var = tk.BooleanVar(value=(self.config.get("resume_unfinished_enabled", "0") == "1"))
            self.resume_status_var = tk.StringVar()
            self.auto_resume_active = False
            self.status_trace_id: str | None = None
            self.update_theme_label()
            self.refresh_resume_status()
            self.status_var = tk.StringVar()
            self.status_detail_var = tk.StringVar()
            self.set_localized(self.status_var, "Ready")
            self.set_localized(self.status_detail_var, "Waiting for render setup")
            self.progress_var = tk.DoubleVar(value=0.0)
            self.progress_text_var = tk.StringVar(value="0%")
            self.remaining_time_var = tk.StringVar()
            self.set_localized(self.remaining_time_var, "Approx. remaining time appears after the first frame")
            self.use_cpu_var = tk.BooleanVar(value=(self.config.get("use_cpu", "1") != "0"))
            self.use_gpu_var = tk.BooleanVar(value=(self.config.get("use_gpu", "1") != "0"))
            self.optimize_enabled_var = tk.BooleanVar(value=(self.config.get("optimize_enabled", "0") == "1"))
            self.auto_optimize_var = tk.BooleanVar(value=(self.config.get("auto_optimize", "0") == "1"))
            self.adaptive_var = tk.BooleanVar(value=(self.config.get("adaptive_sampling", "1") != "0"))
            self.denoise_var = tk.BooleanVar(value=(self.config.get("denoise", "1") != "0"))
            self.persistent_data_var = tk.BooleanVar(value=(self.config.get("persistent_data", "1") != "0"))
            self.fast_bounces_var = tk.BooleanVar(value=(self.config.get("fast_bounces", "1") != "0"))
            self.simplify_var = tk.BooleanVar(value=(self.config.get("simplify", "0") == "1"))
            self.tile_size_var = tk.StringVar(value=self.config.get("tile_size", "256"))
            self.samples_var = tk.StringVar(value=self.config.get("samples", "256"))
            self.resolution_percent_var = tk.StringVar(value=self.config.get("resolution_percent", "100"))
            self.max_restarts_var = tk.StringVar(value=self.config.get("max_restarts", "3"))
            self.render_mode_var = tk.StringVar(value=self.config.get("render_mode", "frames"))
            self.compose_video_var = tk.BooleanVar(value=(self.config.get("compose_video", "0") == "1"))
            self.video_format_var = tk.StringVar(value=self.config.get("video_format", "MP4 (H.264)"))
            self.video_fps_var = tk.StringVar(value=self.config.get("video_fps", "24"))
            self.smart_queue_var = tk.BooleanVar(value=(self.config.get("smart_queue", "1") != "0"))
            self.chunk_mode_var = tk.StringVar(value=self.config.get("chunk_mode", "adaptive"))
            self.chunk_size_var = tk.StringVar(value=self.config.get("chunk_size", "10"))
            self.render_device_mode_var = tk.StringVar(value=self.config.get("render_device_mode", "AUTO"))
            self.compute_backend_var = tk.StringVar(value=self.config.get("compute_backend", "AUTO"))
            self.active_project_var = tk.StringVar(value="No active project")
            self.update_manifest_url_var = tk.StringVar(value=normalize_update_source(self.config.get("update_manifest_url")))
            self.check_updates_on_start_var = tk.BooleanVar(value=(self.config.get("check_updates_on_start", "1") == "1"))
            self.auto_install_updates_var = tk.BooleanVar(value=(self.config.get("auto_install_updates", "0") == "1"))
            self.shutdown_after_render_var = tk.BooleanVar(value=(self.config.get("shutdown_after_render", "0") == "1"))
            self.mobile_enabled_var = tk.BooleanVar(value=(self.config.get("mobile_enabled", "0") == "1"))
            self.mobile_url_var = tk.StringVar()
            self.mobile_sync_code_var = tk.StringVar()
            self.access_mode = normalize_access_mode(self.config.get("access_mode"))
            self.access_mode_var = tk.StringVar()
            self.access_key_var = tk.StringVar(value=self.config.get("access_key") or generate_access_key())
            self.prediction_var = tk.StringVar()
            self.memory_prediction_var = tk.StringVar()
            self.autofix_var = tk.StringVar()
            self.set_localized(self.mobile_url_var, "Mobile dashboard is stopped")
            self.set_localized(self.mobile_sync_code_var, "Start the mobile service to create a sync code")
            self.update_access_mode_label()
            self.set_localized(self.prediction_var, "Select a project and run Analyze")
            self.set_localized(self.memory_prediction_var, "Memory: —")
            self.set_localized(self.autofix_var, "Preflight has not been run")
            self.network_code_var = tk.StringVar(value="")
            self.network_join_code_var = tk.StringVar(value=self.config.get("network_join_code", ""))
            self.ssh_share_invite_var = tk.StringVar(value="")
            self.ssh_invite_private_key = ""
            self.ssh_setup_running = False
            self.network_role_var = tk.StringVar(value=self.config.get("network_role", "connect"))
            self.network_transport = "ssh" if self.config.get("network_transport") == "ssh" else "lan"
            self.network_transport_var = tk.StringVar()
            self.openssh_status_var = tk.StringVar()
            self.network_use_local_var = tk.BooleanVar(value=(self.config.get("network_use_local", "1") != "0"))
            self.network_advertise_lan_var = tk.BooleanVar(value=(self.config.get("network_advertise_lan", "1") != "0"))
            self.network_require_pairing_var = tk.BooleanVar(value=(self.config.get("network_require_pairing", "1") != "0"))
            self.network_controller_id = active_group.group_id
            self.network_group_name_var = tk.StringVar(value=active_group.name)
            self.network_group_selector_var = tk.StringVar(value="")
            self.saved_group_labels: dict[str, RenderGroup] = {}
            self.network_group_security_var = tk.StringVar(value=active_group.security_mode)
            self.network_group_security_label_var = tk.StringVar()
            self.network_allow_failover_var = tk.BooleanVar(value=active_group.allow_failover)
            self.network_pairing_pin_var = tk.StringVar(value="—")
            self.network_pairing_input_var = tk.StringVar(value="")
            self.lan_controller_var = tk.StringVar(value="")
            self.discovered_controller_labels: dict[str, DiscoveredController] = {}
            self.trusted_network_devices = load_trusted_network_devices(self.config.get("network_trusted_devices", ""))
            self.saved_lan_connections = load_saved_network_connections(self.config.get("network_saved_connections", ""))
            self.ssh_host_var = tk.StringVar(value=self.config.get("ssh_host", ""))
            self.ssh_port_var = tk.StringVar(value=self.config.get("ssh_port", "22"))
            self.ssh_user_var = tk.StringVar(value=self.config.get("ssh_user") or os.environ.get("USERNAME", ""))
            self.ssh_identity_var = tk.StringVar(value=self.config.get("ssh_identity", ""))
            self.network_range_mode_var = tk.StringVar(value=self.config.get("network_range_mode", "resume"))
            self.network_range_mode_label_var = tk.StringVar()
            self.controller_name_var = tk.StringVar(value=self.config.get("network_controller_name", platform.node() or "Main PC"))
            self.network_manual_start_var = tk.StringVar(value=self.config.get("network_manual_start", ""))
            self.network_manual_end_var = tk.StringVar(value=self.config.get("network_manual_end", ""))
            self.network_status_var = tk.StringVar()
            self.network_progress_var = tk.DoubleVar(value=0.0)
            self.network_progress_text_var = tk.StringVar()
            self.network_eta_var = tk.StringVar()
            self.worker_name_var = tk.StringVar(value=self.config.get("network_worker_name") or platform.node() or self.tr("Render worker"))
            self.set_localized(self.network_status_var, "Controller is stopped")
            self.set_localized(self.openssh_status_var, "OpenSSH: checking…")
            self.set_localized(self.network_progress_text_var, "Waiting for network render")
            self.set_localized(self.network_eta_var, "No ETA yet")
            self.update_network_transport_label()
            self.update_group_security_label()
            self.worker_range_start_var = tk.StringVar(value="")
            self.worker_range_end_var = tk.StringVar(value="")
            self.worker_samples_var = tk.StringVar(value="")
            self.worker_backend_var = tk.StringVar(value="AUTO")
            self.worker_chunk_size_var = tk.StringVar(value="")
            self.sandbox_frame_var = tk.StringVar(value="1")
            self.sandbox_parallel_var = tk.BooleanVar(value=False)
            self.sandbox_status_var = tk.StringVar()
            self.set_localized(self.sandbox_status_var, "Ready to compare Draft, Balanced and Quality")
            self.config_save_after_id: str | None = None
            self.update_status_var = tk.StringVar()
            self.set_localized(self.update_status_var, "Current version: {version}", version=APP_VERSION)
            self.latest_update_manifest: dict[str, object] | None = None

            self.build_style(ttk)
            self.build_layout(tk, ttk, scrolledtext)
            self.initialize_active_project()
            self.bind_config_autosave()
            self.update_manual_controls()
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)
            self.root.after(150, self.drain_log_queue)
            self.root.after(1000, self.refresh_network_state)
            self.root.after(300, self.refresh_openssh_status)
            self.root.after(500, self.refresh_lan_controllers)
            self.root.after(4000, self.schedule_hardware_poll)
            self.animate_window_in()

            if saved_blender:
                self.log(f"Blender found: {saved_blender}")
            else:
                self.log("Blender was not found automatically. Choose blender.exe manually.")

            if self.check_updates_on_start_var.get():
                self.root.after(800, self.check_for_updates)
            if self.mobile_enabled_var.get():
                self.root.after(1200, self.start_mobile_dashboard)
            if args.resume_unfinished:
                self.root.after(650, self.resume_unfinished_render)

        def tr(self, source: str, **values: object) -> str:
            return translate(source, self.language_code, **values)

        def register_localizable_widget(self, widget) -> None:
            self.localizable_widgets.append(widget)

        def register_heading(self, tree, column: str, source: str) -> None:
            self.localizable_headings.append((tree, column, source))
            tree.heading(column, text=self.tr(source))

        def set_localized(self, variable: tk.StringVar, source: str, **values: object) -> None:
            self.localized_variables[str(variable)] = (variable, source, dict(values))
            variable.set(self.tr(source, **values))

        def set_raw(self, variable: tk.StringVar, value: str) -> None:
            self.localized_variables.pop(str(variable), None)
            variable.set(value)

        def set_widget_text(self, widget, source: str, **values: object) -> None:
            widget._i18n_source = source
            widget._i18n_values = dict(values)
            if widget not in self.localizable_widgets:
                self.localizable_widgets.append(widget)
            widget.configure(text=self.tr(source, **values))

        def change_language(self, _event=None) -> None:
            selected = language_code_from_label(self.language_var.get())
            if selected == self.language_code:
                return
            self.language_code = selected
            self.language_var.set(LANGUAGE_LABELS[selected])

            alive_widgets: list[object] = []
            for widget in self.localizable_widgets:
                try:
                    if not widget.winfo_exists():
                        continue
                    source = getattr(widget, "_i18n_source", "")
                    values = getattr(widget, "_i18n_values", {})
                    widget.configure(text=self.tr(source, **values))
                    alive_widgets.append(widget)
                except (tk.TclError, AttributeError):
                    continue
            self.localizable_widgets = alive_widgets

            alive_headings: list[tuple[object, str, str]] = []
            for tree, column, source in self.localizable_headings:
                try:
                    if not tree.winfo_exists():
                        continue
                    tree.heading(column, text=self.tr(source))
                    alive_headings.append((tree, column, source))
                except tk.TclError:
                    continue
            self.localizable_headings = alive_headings

            for variable, source, values in self.localized_variables.values():
                variable.set(self.tr(source, **values))

            self.update_theme_label()
            if hasattr(self, "theme_combo"):
                self.theme_combo.configure(values=self.localized_theme_labels())
            self.update_access_mode_label()
            if hasattr(self, "access_mode_combo"):
                self.access_mode_combo.configure(values=self.localized_access_mode_labels())
            if hasattr(self, "network_access_mode_combo"):
                self.network_access_mode_combo.configure(values=self.localized_access_mode_labels())
            self.update_network_transport_label()
            if hasattr(self, "network_transport_combo"):
                self.network_transport_combo.configure(values=self.localized_network_transport_labels())
            self.update_group_security_label()
            if hasattr(self, "network_group_security_combo"):
                self.network_group_security_combo.configure(values=self.localized_group_security_labels())
            self.save_current_config()

        def localized_theme_labels(self) -> tuple[str, ...]:
            return tuple(self.tr(label) for label in THEME_LABELS.values())

        def localized_access_mode_labels(self) -> tuple[str, ...]:
            return (self.tr("Generate every start"), self.tr("Keep my code"))

        def update_access_mode_label(self) -> None:
            label = "Keep my code" if self.access_mode == ACCESS_MODE_PERSISTENT else "Generate every start"
            self.access_mode_var.set(self.tr(label))

        def change_access_mode(self, _event=None) -> None:
            selected = self.access_mode_var.get().strip().casefold()
            persistent_labels = {
                "keep my code".casefold(),
                self.tr("Keep my code").casefold(),
                "keep on this network".casefold(),
                self.tr("Keep on this network").casefold(),
            }
            self.access_mode = ACCESS_MODE_PERSISTENT if selected in persistent_labels else "rotate"
            self.update_access_mode_label()
            self.save_current_config()

        def localized_network_transport_labels(self) -> tuple[str, ...]:
            return (self.tr("Local network (LAN)"), self.tr("SSH tunnel"))

        def localized_group_security_labels(self) -> tuple[str, ...]:
            return (self.tr("Open group"), self.tr("Code required"))

        def update_group_security_label(self) -> None:
            source = {
                "open": "Open group",
                "code": "Code required",
            }.get(self.network_group_security_var.get(), "Code required")
            self.network_group_security_label_var.set(self.tr(source))

        def change_group_security(self, _event=None) -> None:
            selected = self.network_group_security_label_var.get().strip().casefold()
            labels = {
                self.tr("Open group").casefold(): "open",
                self.tr("Code required").casefold(): "code",
            }
            self.network_group_security_var.set(labels.get(selected, "code"))
            self.apply_group_security()

        def update_network_transport_label(self) -> None:
            label = "SSH tunnel" if self.network_transport == "ssh" else "Local network (LAN)"
            self.network_transport_var.set(self.tr(label))

        def change_network_transport(self, _event=None) -> None:
            selected = self.network_transport_var.get().strip().casefold()
            ssh_labels = {"ssh tunnel".casefold(), self.tr("SSH tunnel").casefold()}
            self.network_transport = "ssh" if selected in ssh_labels else "lan"
            self.update_network_transport_label()
            self.save_current_config()
            self.refresh_openssh_status()

        def update_theme_label(self) -> None:
            self.theme_var.set(self.tr(THEME_LABELS[self.theme_code]))

        def selected_theme_code(self) -> str:
            selected = self.theme_var.get().strip().casefold()
            for code, label in THEME_LABELS.items():
                if selected in {label.casefold(), self.tr(label).casefold()}:
                    return code
            return self.theme_code

        def change_theme(self, _event=None) -> None:
            selected = self.selected_theme_code()
            if selected == self.theme_code:
                return
            self.theme_code = selected
            self.colors = build_palette(self.theme_code, self.custom_accent)
            self.update_theme_label()
            self.save_current_config()
            self.rebuild_interface()

        def choose_custom_accent(self) -> None:
            _rgb, selected = colorchooser.askcolor(
                color=self.custom_accent,
                title=self.tr("Choose interface colour"),
                parent=self.root,
            )
            if not selected:
                return
            self.custom_accent = normalize_color(selected, self.custom_accent)
            self.theme_code = "custom"
            self.colors = build_palette(self.theme_code, self.custom_accent)
            self.update_theme_label()
            self.save_current_config()
            self.rebuild_interface()

        def apply_motion_preference(self) -> None:
            self.save_current_config()
            self.rebuild_interface()

        def rebuild_interface(self) -> None:
            selected_tab = self.notebook.current_index if hasattr(self, "notebook") else 0
            log_contents = self.log_text.get("1.0", "end-1c") if hasattr(self, "log_text") else ""
            if self.status_trace_id:
                try:
                    self.status_var.trace_remove("write", self.status_trace_id)
                except tk.TclError:
                    pass
                self.status_trace_id = None
            for child in self.root.winfo_children():
                child.destroy()
            self.localizable_widgets.clear()
            self.localizable_headings.clear()
            self.glass_cards.clear()
            self.card_reveal_index = 0
            self.build_style(ttk)
            self.build_layout(tk, ttk, scrolledtext)
            if log_contents:
                self.log_text.insert("1.0", log_contents)
                self.log_text.see("end")
            if selected_tab:
                self.notebook.select(selected_tab)
            self.update_manual_controls()
            self.update_network_role_view()
            self.refresh_queue_tree()
            self.refresh_history_views()
            self.refresh_resume_status()

        def refresh_resume_status(self) -> None:
            if not self.resume_unfinished_var.get():
                self.set_localized(self.resume_status_var, "Startup recovery is off")
                return
            state = load_resume_state(RESUME_STATE_PATH)
            if state is None:
                self.set_localized(self.resume_status_var, "Ready · a Startup file will be created when rendering begins")
                return
            attempts = int(state.get("attempts") or 0)
            if attempts >= 3:
                self.set_localized(self.resume_status_var, "Automatic recovery paused after 3 attempts")
                return
            self.set_localized(
                self.resume_status_var,
                "Recovery armed · attempt {attempt}/3",
                attempt=attempts,
            )

        def on_resume_unfinished_toggle(self) -> None:
            if not self.resume_unfinished_var.get():
                clear_resume_artifacts(RESUME_STATE_PATH, resume_startup_file_path())
                self.refresh_resume_status()
                self.save_current_config()
                return
            if self.worker and self.worker.is_alive():
                mode = "queue" if self.queue_running else "single"
                self.arm_unfinished_resume(mode)
            else:
                self.refresh_resume_status()
            self.save_current_config()

        def arm_unfinished_resume(self, mode: str) -> None:
            if not self.resume_unfinished_var.get():
                return
            previous = load_resume_state(RESUME_STATE_PATH)
            attempts = 0
            if self.auto_resume_active and previous and str(previous.get("mode")) == mode:
                attempts = int(previous.get("attempts") or 0)
            try:
                arm_resume(
                    RESUME_STATE_PATH,
                    resume_startup_file_path(),
                    mode,
                    resume_launch_command(),
                    attempts=attempts,
                )
            except OSError as error:
                self.log(f"[WATCHDOG] Could not create Startup recovery file: {error}")
            self.refresh_resume_status()

        def clear_unfinished_resume(self) -> None:
            clear_resume_artifacts(RESUME_STATE_PATH, resume_startup_file_path())
            self.refresh_resume_status()

        def resume_unfinished_render(self) -> None:
            state = load_resume_state(RESUME_STATE_PATH)
            if state is None:
                return
            if not self.resume_unfinished_var.get():
                clear_resume_artifacts(RESUME_STATE_PATH, resume_startup_file_path())
                return
            attempts = int(state.get("attempts") or 0)
            if attempts >= 3:
                try:
                    resume_startup_file_path().unlink(missing_ok=True)
                except OSError:
                    pass
                self.refresh_resume_status()
                self.set_localized(self.status_var, "Recovery paused")
                self.set_localized(self.status_detail_var, "Open the app and start the render manually")
                return
            updated = mark_resume_attempt(RESUME_STATE_PATH)
            if updated is None:
                return
            self.auto_resume_active = True
            self.refresh_resume_status()
            self.log("[WATCHDOG] Windows Startup recovery is resuming an unfinished render.")
            if str(updated.get("mode")) == "queue":
                self.start_render_queue()
            else:
                self.start_watchdog()
            self.auto_resume_active = False

        def build_style(self, ttk_module) -> None:
            style = ttk_module.Style()
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass

            c = self.colors
            self.root.configure(bg=c["bg"])
            style.configure("App.TFrame", background=c["bg"])
            style.configure("Surface.TFrame", background=c["panel"])
            style.configure("GlassSurface.TFrame", background=c["panel"])
            style.configure("SurfaceAlt.TFrame", background=c["panel_alt"])
            style.configure("CardBorder.TFrame", background=c["line"])
            style.configure("Top.TFrame", background=c["bg"])
            style.configure("Hero.TLabel", background=c["bg"], foreground=c["text"], font=("Segoe UI Variable Display", 28, "bold"))
            style.configure("Subtle.TLabel", background=c["bg"], foreground=c["muted"], font=("Segoe UI", 10))
            style.configure("Chip.TLabel", background=c["panel_alt"], foreground=c["soft"], font=("Segoe UI", 9, "bold"), padding=(12, 6))
            style.configure("Mini.TLabel", background=c["panel"], foreground=c["muted"], font=("Segoe UI", 8, "bold"))
            style.configure("CardTitle.TLabel", background=c["panel"], foreground=c["text"], font=("Segoe UI Variable Text", 13, "bold"))
            style.configure("CardHint.TLabel", background=c["panel"], foreground=c["muted"], font=("Segoe UI", 9))
            style.configure("Field.TLabel", background=c["panel"], foreground=c["soft"], font=("Segoe UI", 9, "bold"))
            style.configure("Device.TLabel", background=c["panel_alt"], foreground=c["soft"], font=("Segoe UI", 9))
            style.configure("Status.TLabel", background=c["panel"], foreground=c["accent_green"], font=("Segoe UI Variable Text", 18, "bold"))
            style.configure("StatusDetail.TLabel", background=c["panel"], foreground=c["muted"], font=("Segoe UI", 9))
            style.configure("ProgressText.TLabel", background=c["panel"], foreground=c["accent"], font=("Segoe UI", 10, "bold"))
            style.configure("TEntry", fieldbackground=c["field"], foreground=c["text"], insertcolor=c["text"], bordercolor=c["field_border"], lightcolor=c["field_border"], darkcolor=c["field_border"], padding=11)
            style.map("TEntry", bordercolor=[("focus", c["accent"])] )
            style.configure("TButton", background=c["panel_alt"], foreground=c["text"], borderwidth=0, focusthickness=0, padding=(15, 10), font=("Segoe UI", 10, "bold"))
            style.map("TButton", background=[("active", c["line"]), ("disabled", c["field"])], foreground=[("disabled", c["muted"])])
            style.configure("Primary.TButton", background=c["accent"], foreground="#031014", padding=(24, 13), font=("Segoe UI", 11, "bold"))
            style.map("Primary.TButton", background=[("active", c["accent_hot"]), ("disabled", c["accent_dark"])], foreground=[("disabled", c["muted"])])
            style.configure("Danger.TButton", background="#3b1724", foreground="#ffd9e2", padding=(17, 12), font=("Segoe UI", 10, "bold"))
            style.map("Danger.TButton", background=[("active", "#5a2234"), ("disabled", "#151923")], foreground=[("disabled", "#65738a")])
            style.configure("Modern.TCheckbutton", background=c["panel_alt"], foreground=c["text"], font=("Segoe UI", 10, "bold"), padding=7)
            style.map("Modern.TCheckbutton", background=[("active", c["panel_alt"])], foreground=[("active", c["accent"]), ("selected", c["text"])])
            style.configure("Modern.Horizontal.TProgressbar", troughcolor=c["field"], background=c["accent"], bordercolor=c["panel"], lightcolor=c["accent"], darkcolor=c["accent"], thickness=16)
            style.configure("Modern.TNotebook", background=c["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
            style.configure("Modern.TNotebook.Tab", background=c["panel"], foreground=c["muted"], padding=(22, 12), font=("Segoe UI", 10, "bold"))
            style.map("Modern.TNotebook.Tab", background=[("selected", c["panel_alt"]), ("active", c["line"])], foreground=[("selected", c["accent"]), ("active", c["text"])])
            style.configure(
                "Glass.TCombobox",
                fieldbackground=c["field"],
                background=c["field"],
                foreground=c["text"],
                arrowcolor=c["muted"],
                borderwidth=0,
                padding=(2, 4),
            )
            style.map(
                "Glass.TCombobox",
                fieldbackground=[("readonly", c["field"])],
                foreground=[("readonly", c["text"])],
                selectbackground=[("readonly", c["field"])],
                selectforeground=[("readonly", c["text"])],
            )

            style.configure(
                "Queue.Treeview",
                background=c["field"],
                fieldbackground=c["field"],
                foreground=c["text"],
                borderwidth=0,
                rowheight=34,
                font=("Segoe UI", 9),
            )
            style.map("Queue.Treeview", background=[("selected", c["accent_dark"])])
            style.configure(
                "Queue.Treeview.Heading",
                background=c["panel_alt"],
                foreground=c["soft"],
                borderwidth=0,
                padding=(8, 8),
                font=("Segoe UI", 9, "bold"),
            )
        def build_layout(self, tk_module, ttk_module, scrolledtext_module) -> None:
            self.build_unified_layout(tk_module, ttk_module, scrolledtext_module)

        def build_legacy_layout(self, tk_module, ttk_module, scrolledtext_module) -> None:
            """Retained temporarily for compatibility while the unified UI settles."""
            self.tab_scroll_canvases.clear()
            self.tab_scroll_refreshers.clear()
            c = self.colors
            ttk_module = GlassWidgetFactory(
                ttk_module,
                c,
                translator=self.tr,
                register=self.register_localizable_widget,
            )
            outer = ttk_module.Frame(self.root, style="App.TFrame", padding=22)
            outer.pack(fill="both", expand=True)
            outer.columnconfigure(0, weight=1)
            outer.rowconfigure(1, weight=1)

            top = ttk_module.Frame(outer, style="Top.TFrame")
            top.grid(row=0, column=0, sticky="ew", pady=(0, 18))
            top.columnconfigure(0, weight=1)

            ttk_module.Label(top, text="Blender Render Watchdog", style="Hero.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(
                top,
                text="Smart recovery, distributed rendering, prediction and phone control.",
                style="Subtle.TLabel",
            ).grid(row=1, column=0, sticky="w", pady=(4, 0))

            chip_row = ttk_module.Frame(top, style="Top.TFrame")
            chip_row.grid(row=2, column=0, sticky="w", pady=(14, 0))
            ttk_module.Button(chip_row, text="Smart queue", style="Chip.TButton").grid(row=0, column=0, sticky="w")
            ttk_module.Button(chip_row, text="Network ×5", style="Chip.TButton").grid(row=0, column=1, sticky="w", padx=(8, 0))
            ttk_module.Button(chip_row, text="Mobile control", style="Chip.TButton").grid(row=0, column=2, sticky="w", padx=(8, 0))
            ttk_module.Button(chip_row, text="History + Auto Fix", style="Chip.TButton").grid(row=0, column=3, sticky="w", padx=(8, 0))

            status_card = self.make_card(top, ttk_module, row=0, column=1, rowspan=3, padx=(18, 0))
            self.status_card = status_card._glass_shell
            ttk_module.Label(status_card, text="CURRENT STATE", style="Mini.TLabel").pack(anchor="w")
            ttk_module.Label(status_card, textvariable=self.status_var, style="Status.TLabel").pack(anchor="w", pady=(4, 0))
            ttk_module.Label(status_card, textvariable=self.status_detail_var, style="StatusDetail.TLabel").pack(anchor="w", pady=(6, 0))
            self.status_trace_id = self.status_var.trace_add("write", lambda *_args: self.status_card.pulse())

            self.notebook = GlassTabView(
                outer,
                palette=c,
                translator=self.tr,
                register=self.register_localizable_widget,
                lightweight=self.lightweight_motion_var.get(),
            )
            self.notebook.grid(row=1, column=0, sticky="nsew")

            def scrollable_tab(text: str):
                shell = ttk_module.Frame(self.notebook.page_host, style="App.TFrame", padding=(0, 8, 0, 0))
                shell.columnconfigure(0, weight=1)
                shell.rowconfigure(0, weight=1)
                canvas = tk_module.Canvas(
                    shell,
                    background=c["bg"],
                    borderwidth=0,
                    highlightthickness=0,
                    yscrollincrement=24,
                )
                scrollbar = ttk_module.Scrollbar(shell, orient="vertical", command=canvas.yview)
                canvas.configure(yscrollcommand=scrollbar.set)
                canvas.grid(row=0, column=0, sticky="nsew")
                scrollbar.grid(row=0, column=1, sticky="ns", padx=(7, 0))
                content = ttk_module.Frame(canvas, style="App.TFrame")
                window_id = canvas.create_window((0, 0), window=content, anchor="nw")

                def sync_scroll_region(_event=None) -> None:
                    width = max(1, canvas.winfo_width())
                    height = max(content.winfo_reqheight(), canvas.winfo_height())
                    canvas.itemconfigure(window_id, width=width, height=height)
                    canvas.configure(scrollregion=canvas.bbox("all"))

                content.bind("<Configure>", sync_scroll_region, add="+")
                canvas.bind("<Configure>", sync_scroll_region, add="+")
                self.tab_scroll_canvases.append(canvas)
                self.tab_scroll_refreshers.append(sync_scroll_region)
                self.notebook.add(shell, text=text)
                return content

            render_tab = scrollable_tab("  Render  ")
            render_tab.columnconfigure(0, weight=1)
            render_tab.rowconfigure(1, weight=1)

            queue_tab = scrollable_tab("  Queue  ")
            queue_tab.columnconfigure(0, weight=1)
            queue_tab.rowconfigure(0, weight=1)

            network_tab = scrollable_tab("  Network  ")
            network_tab.columnconfigure(0, weight=1)
            network_tab.rowconfigure(0, weight=1)

            insights_tab = scrollable_tab("  Insights  ")
            insights_tab.columnconfigure(0, weight=1)
            insights_tab.rowconfigure(0, weight=1)

            sandbox_tab = scrollable_tab("  Sandbox  ")
            sandbox_tab.columnconfigure(0, weight=1)
            sandbox_tab.rowconfigure(0, weight=1)

            advanced_tab = scrollable_tab("  Advanced  ")
            advanced_tab.columnconfigure(0, weight=1)
            advanced_tab.rowconfigure(0, weight=1)

            settings_tab = scrollable_tab("  Settings  ")
            settings_tab.columnconfigure(0, weight=1)
            settings_tab.rowconfigure(0, weight=1)

            logs_tab = scrollable_tab("  Logs  ")
            logs_tab.columnconfigure(0, weight=1)
            logs_tab.rowconfigure(0, weight=1)
            if not self.mousewheel_bound:
                self.root.bind_all("<MouseWheel>", self.on_tab_mousewheel, add="+")
                self.mousewheel_bound = True

            setup_grid = ttk_module.Frame(render_tab, style="App.TFrame")
            setup_grid.grid(row=0, column=0, sticky="ew", pady=(0, 18))
            setup_grid.columnconfigure(0, weight=3)
            setup_grid.columnconfigure(1, weight=2)

            paths_card = self.make_card(setup_grid, ttk_module, row=0, column=0, sticky="nsew", padx=(0, 12))
            ttk_module.Label(paths_card, text="Project Setup", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
            ttk_module.Label(paths_card, text="Scene file, output frames folder, and detected Blender runtime.", style="CardHint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 14))
            paths_card.columnconfigure(1, weight=1)
            self.add_path_row(paths_card, ttk_module, 2, "Blender", self.blender_var, self.choose_blender)
            self.add_path_row(paths_card, ttk_module, 3, ".blend file", self.blend_var, self.choose_blend)
            self.frames_row_widgets = self.add_path_row(
                paths_card,
                ttk_module,
                4,
                "Frames folder",
                self.frames_var,
                self.choose_frames,
            )
            ttk_module.Checkbutton(
                paths_card,
                text="Use .blend output path",
                variable=self.use_scene_output_var,
                command=self.on_scene_output_toggle,
                style="Modern.TCheckbutton",
            ).grid(row=5, column=1, sticky="w", padx=(12, 0), pady=(8, 0))

            devices_card = self.make_card(setup_grid, ttk_module, row=0, column=1, sticky="nsew", padx=(12, 0))
            devices_card.columnconfigure(0, weight=1)
            ttk_module.Label(devices_card, text="Render Device", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(devices_card, text="Pick render devices. Cycles preferences are applied at launch.", style="CardHint.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 14))

            chip_frame = ttk_module.Frame(devices_card, style="SurfaceAlt.TFrame", padding=12)
            chip_frame.grid(row=2, column=0, sticky="ew")
            chip_frame.columnconfigure(0, weight=1)
            ttk_module.Label(chip_frame, text="CPU", style="Device.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(chip_frame, textvariable=self.cpu_info_var, style="Device.TLabel", wraplength=290).grid(row=1, column=0, sticky="w", pady=(3, 0))
            ttk_module.Label(chip_frame, text="GPU", style="Device.TLabel").grid(row=2, column=0, sticky="w", pady=(12, 0))
            ttk_module.Label(chip_frame, textvariable=self.gpu_info_var, style="Device.TLabel", wraplength=290).grid(row=3, column=0, sticky="w", pady=(3, 0))

            toggles = ttk_module.Frame(devices_card, style="SurfaceAlt.TFrame", padding=(12, 10))
            toggles.grid(row=3, column=0, sticky="ew", pady=(12, 0))
            toggles.columnconfigure(0, weight=1)
            toggles.columnconfigure(1, weight=1)
            ttk_module.Checkbutton(toggles, text="Use GPU", variable=self.use_gpu_var, command=self.save_current_config, style="Modern.TCheckbutton").grid(row=0, column=0, sticky="w")
            ttk_module.Checkbutton(toggles, text="Use CPU", variable=self.use_cpu_var, command=self.save_current_config, style="Modern.TCheckbutton").grid(row=0, column=1, sticky="w")

            work_area = ttk_module.Frame(render_tab, style="App.TFrame")
            work_area.grid(row=1, column=0, sticky="nsew")
            work_area.columnconfigure(0, weight=1)
            work_area.rowconfigure(0, weight=1)

            progress_card = self.make_card(work_area, ttk_module, row=0, column=0, sticky="nsew", pady=(0, 14))
            progress_card.columnconfigure(0, weight=1)
            progress_header = ttk_module.Frame(progress_card, style="Surface.TFrame")
            progress_header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
            progress_header.columnconfigure(0, weight=1)
            ttk_module.Label(progress_header, text="Render Timeline", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(progress_header, textvariable=self.progress_text_var, style="ProgressText.TLabel").grid(row=0, column=1, sticky="e")
            ttk_module.Label(
                progress_header,
                textvariable=self.remaining_time_var,
                style="CardHint.TLabel",
            ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
            self.progress_bar = ttk_module.Progressbar(progress_card, variable=self.progress_var, maximum=100, mode="determinate", style="Modern.Horizontal.TProgressbar")
            self.progress_bar.grid(row=1, column=0, sticky="ew")

            action_row = ttk_module.Frame(progress_card, style="Surface.TFrame")
            action_row.grid(row=2, column=0, sticky="ew", pady=(16, 0))
            action_row.columnconfigure(3, weight=1)
            self.start_button = ttk_module.Button(action_row, text="Start render", style="Primary.TButton", command=self.start_watchdog)
            self.start_button.grid(row=0, column=0, sticky="w")
            self.pause_button = ttk_module.Button(action_row, text="Pause after frame", command=self.pause_watchdog, state="disabled")
            self.pause_button.grid(row=0, column=1, sticky="w", padx=(10, 0))
            self.stop_button = ttk_module.Button(action_row, text="Stop now", style="Danger.TButton", command=self.stop_watchdog, state="disabled")
            self.stop_button.grid(row=0, column=2, sticky="w", padx=(10, 0))
            ttk_module.Label(action_row, text="Pause waits for the current frame, Stop terminates Blender now.", style="CardHint.TLabel").grid(row=0, column=3, sticky="e")

            console_card = self.make_card(logs_tab, ttk_module, row=0, column=0, sticky="nsew")
            console_card.rowconfigure(1, weight=1)
            console_card.columnconfigure(0, weight=1)
            console_head = ttk_module.Frame(console_card, style="Surface.TFrame")
            console_head.grid(row=0, column=0, sticky="ew", pady=(0, 10))
            console_head.columnconfigure(0, weight=1)
            ttk_module.Label(console_head, text="Live Console", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(console_head, text="Terminal stream, crashes, restarts and completed frames", style="CardHint.TLabel").grid(row=0, column=1, sticky="e")

            self.log_text = scrolledtext_module.ScrolledText(
                console_card,
                bg=c["field"],
                fg="#dfe7f3",
                insertbackground=c["text"],
                selectbackground="#28415f",
                relief="flat",
                borderwidth=0,
                font=("Cascadia Mono", 10),
                wrap="word",
            )
            self.log_text.grid(row=1, column=0, sticky="nsew")
            self.log_text.tag_configure("frame", foreground=c["accent"])
            self.log_text.tag_configure("error", foreground=c["danger"])
            self.log_text.tag_configure("watchdog", foreground=c["warning"])

            self.build_queue_tab(queue_tab, ttk_module)
            self.build_network_tab(network_tab, ttk_module)
            self.build_insights_tab(insights_tab, ttk_module)
            self.build_sandbox_tab(sandbox_tab, ttk_module)
            self.build_advanced_tab(advanced_tab, ttk_module)
            self.build_settings_tab(settings_tab, ttk_module)
            self.notebook.bind("<<NotebookTabChanged>>", self.animate_tab_change)
            self.root.after_idle(self.refresh_tab_scroll_regions)
            self.root.after(250, self.refresh_tab_scroll_regions)

        def build_unified_layout(self, tk_module, ttk_module, scrolledtext_module) -> None:
            """Build the compact workspace used by the redesigned desktop UI."""
            self.tab_scroll_canvases.clear()
            self.tab_scroll_refreshers.clear()
            c = self.colors
            ui = GlassWidgetFactory(
                ttk_module,
                c,
                translator=self.tr,
                register=self.register_localizable_widget,
            )
            outer = ui.Frame(self.root, style="App.TFrame", padding=(20, 16, 20, 18))
            outer.pack(fill="both", expand=True)
            outer.columnconfigure(0, weight=1)
            outer.rowconfigure(1, weight=1)

            header = ui.Frame(outer, style="Top.TFrame")
            header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
            header.columnconfigure(1, weight=1)
            ui.Label(header, text="Blender Render Watchdog", style="Hero.TLabel").grid(row=0, column=0, sticky="w")
            project_head = ui.Frame(header, style="Top.TFrame")
            project_head.grid(row=0, column=1, sticky="w", padx=(24, 0))
            ui.Label(project_head, text="ACTIVE PROJECT", style="Subtle.TLabel").grid(row=0, column=0, sticky="w")
            ui.Label(project_head, textvariable=self.active_project_var, style="Chip.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))
            header_actions = ui.Frame(header, style="Top.TFrame")
            header_actions.grid(row=0, column=2, sticky="e")
            ui.Button(header_actions, text="Settings", command=self.open_settings_window).grid(row=0, column=0, padx=(0, 8))
            self.status_card = GlassCard(
                header_actions,
                palette=c,
                padding=16,
                radius=18,
                backdrop=c["bg"],
                effects_enabled=False,
            )
            self.status_card.grid(row=0, column=1, sticky="e")
            status_content = self.status_card.content
            ui.Label(status_content, textvariable=self.status_var, style="Status.TLabel").pack(anchor="w")
            ui.Label(status_content, textvariable=self.status_detail_var, style="StatusDetail.TLabel").pack(anchor="w", pady=(2, 0))
            self.status_trace_id = self.status_var.trace_add("write", lambda *_args: self.status_card.pulse())

            self.notebook = GlassTabView(
                outer,
                palette=c,
                translator=self.tr,
                register=self.register_localizable_widget,
                lightweight=True,
            )
            self.notebook.grid(row=1, column=0, sticky="nsew")

            def scrollable_page(text: str):
                shell = ui.Frame(self.notebook.page_host, style="App.TFrame", padding=(0, 8, 0, 0))
                shell.columnconfigure(0, weight=1)
                shell.rowconfigure(0, weight=1)
                canvas = tk_module.Canvas(
                    shell,
                    background=c["bg"],
                    borderwidth=0,
                    highlightthickness=0,
                    yscrollincrement=28,
                )
                scrollbar = ui.Scrollbar(shell, orient="vertical", command=canvas.yview)
                canvas.configure(yscrollcommand=scrollbar.set)
                canvas.grid(row=0, column=0, sticky="nsew")
                scrollbar.grid(row=0, column=1, sticky="ns", padx=(7, 0))
                content = ui.Frame(canvas, style="App.TFrame")
                window_id = canvas.create_window((0, 0), window=content, anchor="nw")

                def sync_scroll_region(_event=None) -> None:
                    width = max(1, canvas.winfo_width())
                    height = max(content.winfo_reqheight(), canvas.winfo_height())
                    canvas.itemconfigure(window_id, width=width, height=height)
                    canvas.configure(scrollregion=canvas.bbox("all"))

                content.bind("<Configure>", sync_scroll_region, add="+")
                canvas.bind("<Configure>", sync_scroll_region, add="+")
                self.tab_scroll_canvases.append(canvas)
                self.tab_scroll_refreshers.append(sync_scroll_region)
                self.notebook.add(shell, text=text)
                return content

            workspace = scrollable_page("  Workspace  ")
            workspace.columnconfigure(0, weight=3, minsize=260)
            workspace.columnconfigure(1, weight=5, minsize=390)
            workspace.columnconfigure(2, weight=4, minsize=350)
            workspace.rowconfigure(0, weight=1)

            self.build_workspace_queue(workspace, ui)
            self.build_workspace_render(workspace, ui)
            self.build_workspace_network(workspace, ui)

            insights_page = scrollable_page("  Insights  ")
            insights_page.columnconfigure(0, weight=1)
            sandbox_page = scrollable_page("  Sandbox  ")
            sandbox_page.columnconfigure(0, weight=1)
            advanced_page = scrollable_page("  Advanced  ")
            advanced_page.columnconfigure(0, weight=1)
            logs_page = scrollable_page("  Logs  ")
            logs_page.columnconfigure(0, weight=1)
            logs_page.rowconfigure(0, weight=1)

            self.build_insights_tab(insights_page, ui)
            self.build_sandbox_tab(sandbox_page, ui)
            self.build_advanced_tab(advanced_page, ui)

            console_card = self.make_card(logs_page, ui, row=0, column=0, sticky="nsew")
            console_card.rowconfigure(1, weight=1)
            console_card.columnconfigure(0, weight=1)
            console_head = ui.Frame(console_card, style="Surface.TFrame")
            console_head.grid(row=0, column=0, sticky="ew", pady=(0, 10))
            console_head.columnconfigure(0, weight=1)
            ui.Label(console_head, text="Live Console", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ui.Label(console_head, text="Blender output and recovery events", style="CardHint.TLabel").grid(row=0, column=1, sticky="e")
            self.log_text = scrolledtext_module.ScrolledText(
                console_card,
                bg=c["field"],
                fg="#dfe7f3",
                insertbackground=c["text"],
                selectbackground="#28415f",
                relief="flat",
                borderwidth=0,
                font=("Cascadia Mono", 10),
                wrap="word",
                height=28,
            )
            self.log_text.grid(row=1, column=0, sticky="nsew")
            self.log_text.tag_configure("frame", foreground=c["accent"])
            self.log_text.tag_configure("error", foreground=c["danger"])
            self.log_text.tag_configure("watchdog", foreground=c["warning"])

            if not self.mousewheel_bound:
                self.root.bind_all("<MouseWheel>", self.on_tab_mousewheel, add="+")
                self.mousewheel_bound = True
            self.notebook.bind("<<NotebookTabChanged>>", self.animate_tab_change)
            self.update_network_role_view()
            self.update_network_range_mode_view()
            self.apply_render_device_mode()
            self.refresh_queue_tree()
            self.root.after_idle(self.refresh_tab_scroll_regions)

        def build_workspace_queue(self, parent, ui) -> None:
            card = self.make_card(parent, ui, row=0, column=0, sticky="nsew", padx=(0, 7))
            card.columnconfigure(0, weight=1)
            card.rowconfigure(2, weight=1)
            ui.Label(card, text="Projects", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ui.Label(card, text="One queue, one active project", style="CardHint.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 12))

            columns = ("index", "project", "range", "mode", "estimate", "output", "status")
            self.queue_tree = ui.Treeview(
                card,
                columns=columns,
                displaycolumns=("project", "status"),
                show="headings",
                selectmode="browse",
                style="Queue.Treeview",
                height=15,
            )
            self.register_heading(self.queue_tree, "project", "Project")
            self.register_heading(self.queue_tree, "status", "Status")
            self.queue_tree.column("project", width=170, minwidth=110, stretch=True)
            self.queue_tree.column("status", width=80, minwidth=70, anchor="center", stretch=False)
            self.queue_tree.grid(row=2, column=0, sticky="nsew")
            self.queue_tree.bind("<<TreeviewSelect>>", self.on_queue_project_selected)

            actions = ui.Frame(card, style="Surface.TFrame")
            actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
            actions.columnconfigure(0, weight=1)
            ui.Button(actions, text="Add files", command=self.add_files_to_queue).grid(row=0, column=0, sticky="w")
            ui.Button(actions, text="Add current", command=self.add_current_to_queue).grid(row=0, column=1, padx=(6, 0))
            ui.Button(actions, text="Remove", command=self.remove_queue_job).grid(row=0, column=2, padx=(6, 0))
            move_row = ui.Frame(card, style="Surface.TFrame")
            move_row.grid(row=4, column=0, sticky="ew", pady=(8, 0))
            move_row.columnconfigure(0, weight=1)
            self.queue_summary_var = tk.StringVar()
            self.set_localized(self.queue_summary_var, "Queue is empty")
            ui.Label(move_row, textvariable=self.queue_summary_var, style="CardHint.TLabel").grid(row=0, column=0, sticky="w")
            ui.Button(move_row, text="↑", width=3, command=lambda: self.move_queue_job(-1)).grid(row=0, column=1)
            ui.Button(move_row, text="↓", width=3, command=lambda: self.move_queue_job(1)).grid(row=0, column=2, padx=(4, 0))
            self.start_queue_button = ui.Button(card, text="Render queue", style="Primary.TButton", command=self.start_render_queue)
            self.start_queue_button.grid(row=5, column=0, sticky="ew", pady=(12, 0))

        def build_workspace_render(self, parent, ui) -> None:
            column = ui.Frame(parent, style="App.TFrame")
            column.grid(row=0, column=1, sticky="nsew", padx=7)
            column.columnconfigure(0, weight=1)

            setup = self.make_card(column, ui, row=0, column=0, sticky="ew")
            setup.columnconfigure(1, weight=1)
            ui.Label(setup, text="Active project", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
            ui.Label(setup, textvariable=self.active_project_var, style="CardHint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 10))
            self.add_path_row(setup, ui, 2, ".blend", self.blend_var, self.choose_blend)
            self.frames_row_widgets = self.add_path_row(setup, ui, 3, "Output", self.frames_var, self.choose_frames)
            ui.Checkbutton(
                setup,
                text="Project output",
                variable=self.use_scene_output_var,
                command=self.on_scene_output_toggle,
                style="Modern.TCheckbutton",
            ).grid(row=4, column=1, sticky="w", padx=(12, 0), pady=(6, 0))
            blender_row = ui.Frame(setup, style="Surface.TFrame")
            blender_row.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 0))
            blender_row.columnconfigure(1, weight=1)
            ui.Label(blender_row, text="Blender", style="Field.TLabel").grid(row=0, column=0, sticky="w")
            ui.Entry(blender_row, textvariable=self.blender_var).grid(row=0, column=1, sticky="ew", padx=(10, 8))
            ui.Button(blender_row, text="Browse", command=self.choose_blender).grid(row=0, column=2)

            distribution = self.make_card(column, ui, row=1, column=0, sticky="ew", pady=(12, 0))
            distribution.columnconfigure(1, weight=1)
            ui.Label(distribution, text="Render strategy", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
            ui.Label(
                distribution,
                text="Automatic hardware and frame chunks",
                style="CardHint.TLabel",
                wraplength=410,
                justify="left",
            ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 12))
            ui.Label(distribution, text="Device", style="Field.TLabel").grid(row=2, column=0, sticky="w", pady=4)
            device_combo = ui.Combobox(
                distribution,
                textvariable=self.render_device_mode_var,
                values=("AUTO", "GPU", "CPU_GPU", "CPU"),
                state="readonly",
            )
            device_combo.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=4)
            device_combo.bind("<<ComboboxSelected>>", self.apply_render_device_mode)
            ui.Label(distribution, text="Cycles", style="Field.TLabel").grid(row=3, column=0, sticky="w", pady=4)
            backend_values = tuple(dict.fromkeys(["AUTO", *self.local_capabilities.compute_backends]))
            ui.Combobox(
                distribution,
                textvariable=self.compute_backend_var,
                values=backend_values,
                state="readonly",
            ).grid(row=3, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=4)
            ui.Label(distribution, text="Assignment", style="Field.TLabel").grid(row=4, column=0, sticky="w", pady=4)
            chunk_mode = ui.Combobox(distribution, textvariable=self.chunk_mode_var, values=("adaptive", "fixed"), state="readonly", width=12)
            chunk_mode.grid(row=4, column=1, sticky="ew", padx=(12, 5), pady=4)
            ui.Combobox(distribution, textvariable=self.chunk_size_var, values=("1", "5", "10", "20", "50"), width=8).grid(row=4, column=2, sticky="ew", padx=(5, 0), pady=4)

            progress = self.make_card(column, ui, row=2, column=0, sticky="ew", pady=(12, 0))
            progress.columnconfigure(0, weight=1)
            progress_head = ui.Frame(progress, style="Surface.TFrame")
            progress_head.grid(row=0, column=0, sticky="ew")
            progress_head.columnconfigure(0, weight=1)
            ui.Label(progress_head, text="Render", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ui.Label(progress_head, textvariable=self.progress_text_var, style="ProgressText.TLabel").grid(row=0, column=1, sticky="e")
            ui.Label(progress, textvariable=self.remaining_time_var, style="CardHint.TLabel").grid(row=1, column=0, sticky="w", pady=(4, 10))
            self.progress_bar = ui.Progressbar(progress, variable=self.progress_var, maximum=100, mode="determinate", style="Modern.Horizontal.TProgressbar")
            self.progress_bar.grid(row=2, column=0, sticky="ew")
            controls = ui.Frame(progress, style="Surface.TFrame")
            controls.grid(row=3, column=0, sticky="ew", pady=(14, 0))
            controls.columnconfigure(0, weight=1)
            self.start_button = ui.Button(controls, text="Start render", style="Primary.TButton", command=self.start_watchdog)
            self.start_button.grid(row=0, column=0, sticky="ew")
            self.pause_button = ui.Button(controls, text="Pause", command=self.pause_watchdog, state="disabled")
            self.pause_button.grid(row=0, column=1, padx=(7, 0))
            self.stop_button = ui.Button(controls, text="Stop", style="Danger.TButton", command=self.stop_watchdog, state="disabled")
            self.stop_button.grid(row=0, column=2, padx=(7, 0))

        def build_workspace_network(self, parent, ui) -> None:
            column = ui.Frame(parent, style="App.TFrame")
            column.grid(row=0, column=2, sticky="nsew", padx=(7, 0))
            column.columnconfigure(0, weight=1)

            group_card = self.make_card(column, ui, row=0, column=0, sticky="ew")
            group_card.columnconfigure(0, weight=1)
            group_head = ui.Frame(group_card, style="Surface.TFrame")
            group_head.grid(row=0, column=0, sticky="ew")
            group_head.columnconfigure(1, weight=1)
            self.connection_status_icon = ConnectionStatusIcon(group_head, palette=self.colors, state="offline", backdrop=self.colors["panel"])
            self.connection_status_icon.grid(row=0, column=0, sticky="w", padx=(0, 9))
            ui.Label(group_head, text="Render group", style="CardTitle.TLabel").grid(row=0, column=1, sticky="w")
            ui.Button(group_head, text="New group", command=self.create_new_render_group).grid(row=0, column=2, sticky="e", padx=(0, 6))
            ui.Button(group_head, text="Connection", command=self.open_connection_settings).grid(row=0, column=3, sticky="e")
            role_row = ui.Frame(group_card, style="Surface.TFrame")
            self.network_group_selector = ui.Combobox(
                group_card,
                textvariable=self.network_group_selector_var,
                state="readonly",
            )
            self.network_group_selector.grid(row=1, column=0, sticky="ew", pady=(10, 0))
            self.network_group_selector.bind("<<ComboboxSelected>>", self.switch_saved_render_group)
            self.refresh_saved_group_selector()
            role_row.grid(row=2, column=0, sticky="ew", pady=(8, 0))
            role_row.columnconfigure(0, weight=1)
            role_row.columnconfigure(1, weight=1)
            ui.Button(role_row, text="Main PC", command=lambda: self.set_network_role("host")).grid(row=0, column=0, sticky="ew", padx=(0, 4))
            ui.Button(role_row, text="Join group", command=lambda: self.set_network_role("connect")).grid(row=0, column=1, sticky="ew", padx=(4, 0))

            host_controls = ui.Frame(group_card, style="Surface.TFrame")
            host_controls.grid(row=3, column=0, sticky="ew", pady=(12, 0))
            host_controls.columnconfigure(0, weight=1)
            self.network_controller_card = host_controls
            ui.Entry(host_controls, textvariable=self.network_group_name_var).grid(row=0, column=0, sticky="ew")
            self.network_group_security_combo = ui.Combobox(
                host_controls,
                textvariable=self.network_group_security_label_var,
                values=self.localized_group_security_labels(),
                state="readonly",
            )
            self.network_group_security_combo.grid(row=1, column=0, sticky="ew", pady=(7, 0))
            self.network_group_security_combo.bind("<<ComboboxSelected>>", self.change_group_security)
            options = ui.Frame(host_controls, style="Surface.TFrame")
            options.grid(row=2, column=0, sticky="ew", pady=(6, 0))
            ui.Checkbutton(options, text="Visible on LAN", variable=self.network_advertise_lan_var, command=self.apply_lan_visibility_settings, style="Modern.TCheckbutton").grid(row=0, column=0, sticky="w")
            ui.Checkbutton(options, text="Auto main", variable=self.network_allow_failover_var, command=self.save_current_config, style="Modern.TCheckbutton").grid(row=1, column=0, sticky="w", pady=(4, 0))
            host_actions = ui.Frame(host_controls, style="Surface.TFrame")
            host_actions.grid(row=3, column=0, sticky="ew", pady=(8, 0))
            host_actions.columnconfigure(0, weight=1)
            ui.Button(host_actions, text="Start group", style="Primary.TButton", command=self.start_network_controller).grid(row=0, column=0, sticky="ew")
            ui.Button(host_actions, text="Stop", command=self.stop_network_controller).grid(row=0, column=1, padx=(7, 0))
            ui.Entry(host_controls, textvariable=self.network_code_var, state="readonly").grid(row=4, column=0, sticky="ew", pady=(8, 0))
            pin_row = ui.Frame(host_controls, style="Surface.TFrame")
            pin_row.grid(row=5, column=0, sticky="ew", pady=(6, 0))
            pin_row.columnconfigure(0, weight=1)
            self.network_pairing_pin_label = ui.Label(pin_row, text="One-time code: {code}", style="CardHint.TLabel")
            self.network_pairing_pin_label.grid(row=0, column=0, sticky="w")
            ui.Button(pin_row, text="Copy invite", command=self.copy_network_code).grid(row=0, column=1, sticky="e")
            render_actions = ui.Frame(host_controls, style="Surface.TFrame")
            render_actions.grid(row=6, column=0, sticky="ew", pady=(8, 0))
            render_actions.columnconfigure(0, weight=1)
            ui.Button(render_actions, text="Render on group", style="Primary.TButton", command=self.start_network_render).grid(row=0, column=0, sticky="ew")
            ui.Button(render_actions, text="Stop render", style="Danger.TButton", command=self.stop_network_render).grid(row=0, column=1, padx=(7, 0))

            join_controls = ui.Frame(group_card, style="Surface.TFrame")
            join_controls.grid(row=3, column=0, sticky="ew", pady=(12, 0))
            join_controls.columnconfigure(0, weight=1)
            self.network_worker_card = join_controls
            self.lan_controller_combo = ui.Combobox(join_controls, textvariable=self.lan_controller_var, state="readonly")
            self.lan_controller_combo.grid(row=0, column=0, sticky="ew")
            ui.Button(join_controls, text="Refresh", command=self.refresh_lan_controllers).grid(row=0, column=1, padx=(7, 0))
            ui.Entry(join_controls, textvariable=self.network_pairing_input_var).grid(row=1, column=0, sticky="ew", pady=(7, 0))
            ui.Label(join_controls, text="Code only for protected groups", style="CardHint.TLabel").grid(row=2, column=0, sticky="w", pady=(3, 0))
            join_actions = ui.Frame(join_controls, style="Surface.TFrame")
            join_actions.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
            join_actions.columnconfigure(0, weight=1)
            ui.Button(join_actions, text="Connect", style="Primary.TButton", command=self.connect_selected_lan_controller).grid(row=0, column=0, sticky="ew")
            ui.Button(join_actions, text="Disconnect", command=self.stop_network_worker).grid(row=0, column=1, padx=(7, 0))

            devices = self.make_card(column, ui, row=1, column=0, sticky="nsew", pady=(12, 0))
            devices.columnconfigure(0, weight=1)
            devices.rowconfigure(4, weight=1)
            device_head = ui.Frame(devices, style="Surface.TFrame")
            device_head.grid(row=0, column=0, sticky="ew")
            device_head.columnconfigure(0, weight=1)
            ui.Label(device_head, text="Devices", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ui.Label(device_head, text="Select to configure", style="CardHint.TLabel").grid(row=0, column=1, sticky="e")
            ui.Progressbar(devices, variable=self.network_progress_var, maximum=100, mode="determinate", style="Modern.Horizontal.TProgressbar").grid(row=1, column=0, sticky="ew", pady=(10, 0))
            progress_text = ui.Frame(devices, style="Surface.TFrame")
            progress_text.grid(row=2, column=0, sticky="ew", pady=(4, 8))
            progress_text.columnconfigure(0, weight=1)
            ui.Label(progress_text, textvariable=self.network_progress_text_var, style="CardHint.TLabel").grid(row=0, column=0, sticky="w")
            ui.Label(progress_text, textvariable=self.network_eta_var, style="CardHint.TLabel").grid(row=0, column=1, sticky="e")
            columns = ("name", "state", "hardware", "current", "done", "average", "samples", "range")
            self.network_tree = ui.Treeview(
                devices,
                columns=columns,
                displaycolumns=("name", "state", "current", "average"),
                show="headings",
                style="Queue.Treeview",
                selectmode="browse",
                height=11,
            )
            for column_name, heading, width in (
                ("name", "Device", 130),
                ("state", "Status", 70),
                ("current", "Frames", 65),
                ("average", "Average", 70),
            ):
                self.register_heading(self.network_tree, column_name, heading)
                self.network_tree.column(column_name, width=width, minwidth=55, stretch=column_name == "name", anchor="w" if column_name == "name" else "center")
            self.network_tree.grid(row=4, column=0, sticky="nsew")
            self.network_tree.bind("<<TreeviewSelect>>", self.on_network_device_selected)
            ui.Label(devices, textvariable=self.network_status_var, style="CardHint.TLabel", wraplength=340).grid(row=5, column=0, sticky="w", pady=(8, 0))

        def apply_render_device_mode(self, _event=None) -> None:
            mode = self.render_device_mode_var.get().strip().upper()
            has_gpu = bool(self.local_capabilities.gpus) and not all(
                "no gpu" in gpu.casefold() for gpu in self.local_capabilities.gpus
            )
            if mode == "CPU":
                use_cpu, use_gpu = True, False
            elif mode == "GPU":
                use_cpu, use_gpu = False, True
            elif mode == "CPU_GPU":
                use_cpu, use_gpu = True, True
            else:
                mode = "AUTO"
                use_cpu, use_gpu = (False, True) if has_gpu else (True, False)
            self.render_device_mode_var.set(mode)
            self.use_cpu_var.set(use_cpu)
            self.use_gpu_var.set(use_gpu)
            self.schedule_config_save()

        def create_new_render_group(self) -> None:
            if self.network_controller is not None:
                messagebox.showinfo(self.tr("Render group"), self.tr("Stop the current group before creating another one."))
                return
            name = simpledialog.askstring(
                self.tr("New group"),
                self.tr("Group name"),
                parent=self.root,
            )
            if not name or not name.strip():
                return
            group = self.group_registry.create_group(name.strip(), self.network_group_security_var.get())
            group.register_device(
                self.group_registry.identity.device_id,
                platform.node() or "This PC",
                identity_fingerprint=self.group_registry.identity.fingerprint,
                capabilities=self.local_capabilities,
                role="coordinator",
            )
            self.network_controller_id = group.group_id
            self.network_group_name_var.set(group.name)
            self.network_group_security_var.set(group.security_mode)
            self.network_allow_failover_var.set(group.allow_failover)
            self.network_role_var.set("host")
            self.update_group_security_label()
            self.refresh_saved_group_selector()
            self.update_network_role_view()
            self.group_registry.save(GROUPS_PATH)
            self.save_current_config()
            self.set_localized(self.network_status_var, "Group created. Start it to accept devices.")

        def refresh_saved_group_selector(self) -> None:
            labels = {
                f"{group.name} · {group.group_id[:6]}": group
                for group in self.group_registry.groups.values()
            }
            self.saved_group_labels = labels
            if hasattr(self, "network_group_selector"):
                self.network_group_selector.configure(values=tuple(labels))
            active = self.group_registry.active
            selected = next((label for label, group in labels.items() if active and group.group_id == active.group_id), "")
            self.network_group_selector_var.set(selected)

        def switch_saved_render_group(self, _event=None) -> None:
            group = self.saved_group_labels.get(self.network_group_selector_var.get())
            if group is None or group.group_id == self.group_registry.active_group_id:
                return
            if self.network_controller is not None or self.network_worker is not None:
                messagebox.showinfo(self.tr("Render group"), self.tr("Disconnect before switching render groups."))
                self.refresh_saved_group_selector()
                return
            self.group_registry.active_group_id = group.group_id
            self.network_controller_id = group.group_id
            self.network_group_name_var.set(group.name)
            self.network_group_security_var.set(group.security_mode)
            self.network_allow_failover_var.set(group.allow_failover)
            self.network_advertise_lan_var.set(group.visible_on_lan)
            self.update_group_security_label()
            self.group_registry.save(GROUPS_PATH)
            self.save_current_config()
            self.set_localized(self.network_status_var, "Selected group: {group}", group=group.name)

        def apply_group_security(self, _event=None) -> None:
            mode = self.network_group_security_var.get().strip().lower()
            if mode not in {"open", "code"}:
                mode = "code"
                self.network_group_security_var.set(mode)
            self.network_require_pairing_var.set(mode != "open")
            self.apply_lan_visibility_settings()

        def open_settings_window(self) -> None:
            existing = getattr(self, "settings_window", None)
            if existing is not None:
                try:
                    existing.lift()
                    existing.focus_force()
                    return
                except tk.TclError:
                    self.settings_window = None
            dialog = tk.Toplevel(self.root)
            self.settings_window = dialog
            dialog.title(self.tr("Settings"))
            dialog.geometry("1080x760")
            dialog.minsize(900, 620)
            dialog.configure(background=self.colors["bg"])
            dialog.transient(self.root)
            ui = GlassWidgetFactory(ttk, self.colors, translator=self.tr, register=self.register_localizable_widget)
            shell = ui.Frame(dialog, style="App.TFrame", padding=18)
            shell.pack(fill="both", expand=True)
            shell.columnconfigure(0, weight=1)
            shell.rowconfigure(1, weight=1)
            title_row = ui.Frame(shell, style="Top.TFrame")
            title_row.grid(row=0, column=0, sticky="ew", pady=(0, 12))
            title_row.columnconfigure(0, weight=1)
            ui.Label(title_row, text="Settings", style="Hero.TLabel").grid(row=0, column=0, sticky="w")
            ui.Button(title_row, text="Close", command=dialog.destroy).grid(row=0, column=1, sticky="e")
            canvas = tk.Canvas(shell, background=self.colors["bg"], borderwidth=0, highlightthickness=0, yscrollincrement=26)
            scrollbar = ui.Scrollbar(shell, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=scrollbar.set)
            canvas.grid(row=1, column=0, sticky="nsew")
            scrollbar.grid(row=1, column=1, sticky="ns", padx=(7, 0))
            content = ui.Frame(canvas, style="App.TFrame")
            window_id = canvas.create_window((0, 0), window=content, anchor="nw")

            def sync(_event=None) -> None:
                canvas.itemconfigure(window_id, width=max(1, canvas.winfo_width()))
                canvas.configure(scrollregion=canvas.bbox("all"))

            content.bind("<Configure>", sync, add="+")
            canvas.bind("<Configure>", sync, add="+")
            canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-1 if event.delta > 0 else 1, "units"))
            self.build_settings_tab(content, ui)

            def close() -> None:
                self.settings_window = None
                dialog.destroy()

            dialog.protocol("WM_DELETE_WINDOW", close)

        def open_connection_settings(self) -> None:
            dialog = tk.Toplevel(self.root)
            dialog.title(self.tr("Connection settings"))
            dialog.geometry("720x790")
            dialog.resizable(False, False)
            dialog.configure(background=self.colors["bg"])
            dialog.transient(self.root)
            ui = GlassWidgetFactory(ttk, self.colors, translator=self.tr, register=self.register_localizable_widget)
            shell = GlassCard(
                dialog,
                palette=self.colors,
                padding=22,
                radius=26,
                backdrop=self.colors["bg"],
                effects_enabled=False,
            )
            shell.pack(fill="both", expand=True, padx=20, pady=20)
            card = shell.content
            card.columnconfigure(1, weight=1)
            ui.Label(card, text="Connection", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
            ui.Label(
                card,
                text="LAN is always available. SSH is used as the saved route between different networks.",
                style="CardHint.TLabel",
                wraplength=620,
                justify="left",
            ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 14))
            ui.Label(card, text="Transport", style="Field.TLabel").grid(row=2, column=0, sticky="w", pady=5)
            self.network_transport_combo = ui.Combobox(
                card,
                textvariable=self.network_transport_var,
                values=self.localized_network_transport_labels(),
                state="readonly",
            )
            self.network_transport_combo.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=5)
            self.network_transport_combo.bind("<<ComboboxSelected>>", self.change_network_transport)
            for row, (label, variable) in enumerate(
                (
                    ("SSH host", self.ssh_host_var),
                    ("SSH port", self.ssh_port_var),
                    ("SSH user", self.ssh_user_var),
                ),
                start=3,
            ):
                ui.Label(card, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="w", pady=5)
                ui.Entry(card, textvariable=variable).grid(row=row, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=5)
            ui.Label(card, text="SSH key", style="Field.TLabel").grid(row=6, column=0, sticky="w", pady=5)
            ui.Entry(card, textvariable=self.ssh_identity_var).grid(row=6, column=1, sticky="ew", padx=(12, 8), pady=5)
            ui.Button(card, text="Browse", command=self.browse_ssh_identity).grid(row=6, column=2, pady=5)
            ui.Label(card, textvariable=self.openssh_status_var, style="CardHint.TLabel").grid(row=7, column=0, columnspan=2, sticky="w", pady=(10, 0))
            ui.Button(card, text="Install OpenSSH", command=self.install_openssh).grid(row=7, column=2, sticky="e", pady=(10, 0))
            ui.Label(card, text="Invitation from this main PC", style="Field.TLabel").grid(row=8, column=0, columnspan=3, sticky="w", pady=(16, 5))
            ui.Entry(card, textvariable=self.ssh_share_invite_var).grid(row=9, column=0, columnspan=3, sticky="ew")
            ui.Label(
                card,
                text="Creates a dedicated SSH key and copies a trusted invitation link. Share it only with your render devices.",
                style="CardHint.TLabel",
                wraplength=620,
                justify="left",
            ).grid(row=10, column=0, columnspan=2, sticky="w", pady=(5, 0))
            ui.Button(card, text="Create and copy SSH invite", command=self.create_ssh_invitation).grid(row=10, column=2, sticky="e", pady=(5, 0))
            ui.Label(card, text="Invitation received on this PC", style="Field.TLabel").grid(row=11, column=0, sticky="w", pady=(16, 5))
            ui.Entry(card, textvariable=self.network_join_code_var).grid(row=12, column=0, columnspan=3, sticky="ew")
            buttons = ui.Frame(card, style="Surface.TFrame")
            buttons.grid(row=13, column=0, columnspan=3, sticky="ew", pady=(18, 0))
            buttons.columnconfigure(0, weight=1)
            ui.Button(buttons, text="Save", style="Primary.TButton", command=lambda: (self.change_network_transport(), self.save_current_config(), dialog.destroy())).grid(row=0, column=0, sticky="ew")
            ui.Button(buttons, text="Connect by invitation", command=lambda: (dialog.destroy(), self.start_network_worker())).grid(row=0, column=1, padx=(8, 0))
            ui.Button(buttons, text="Close", command=dialog.destroy).grid(row=0, column=2, padx=(8, 0))

        def build_queue_tab(self, parent, ttk_module) -> None:
            queue_card = self.make_card(parent, ttk_module, row=0, column=0, sticky="nsew")
            queue_card.columnconfigure(0, weight=1)
            queue_card.rowconfigure(1, weight=1)

            header = ttk_module.Frame(queue_card, style="Surface.TFrame")
            header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
            header.columnconfigure(0, weight=1)
            ttk_module.Label(header, text="Render Queue", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(
                header,
                text="Estimate projects, put shorter work first, retry failures and continue automatically.",
                style="CardHint.TLabel",
            ).grid(row=1, column=0, sticky="w", pady=(3, 0))

            queue_actions = ttk_module.Frame(header, style="Surface.TFrame")
            queue_actions.grid(row=0, column=1, rowspan=2, sticky="e")
            ttk_module.Button(queue_actions, text="Add current", command=self.add_current_to_queue).grid(row=0, column=0)
            ttk_module.Button(queue_actions, text="Add files", command=self.add_files_to_queue).grid(row=0, column=1, padx=(8, 0))
            ttk_module.Button(queue_actions, text="Remove", command=self.remove_queue_job).grid(row=0, column=2, padx=(8, 0))
            ttk_module.Button(queue_actions, text="↑", width=3, command=lambda: self.move_queue_job(-1)).grid(row=0, column=3, padx=(8, 0))
            ttk_module.Button(queue_actions, text="↓", width=3, command=lambda: self.move_queue_job(1)).grid(row=0, column=4, padx=(4, 0))
            ttk_module.Button(queue_actions, text="Estimate + sort", command=self.estimate_and_sort_queue).grid(row=0, column=5, padx=(8, 0))

            tree_frame = ttk_module.Frame(queue_card, style="Surface.TFrame")
            tree_frame.grid(row=1, column=0, sticky="nsew")
            tree_frame.columnconfigure(0, weight=1)
            tree_frame.rowconfigure(0, weight=1)
            columns = ("order", "project", "range", "mode", "estimate", "output", "status")
            self.queue_tree = ttk_module.Treeview(
                tree_frame,
                columns=columns,
                show="headings",
                selectmode="browse",
                style="Queue.Treeview",
            )
            headings = {
                "order": "#",
                "project": "Project",
                "range": "Frames",
                "mode": "Mode",
                "estimate": "Estimate",
                "output": "Output",
                "status": "Status",
            }
            widths = {"order": 40, "project": 220, "range": 105, "mode": 80, "estimate": 90, "output": 280, "status": 95}
            for column in columns:
                self.register_heading(self.queue_tree, column, headings[column])
                self.queue_tree.column(
                    column,
                    width=widths[column],
                    minwidth=40,
                    stretch=column in {"project", "output"},
                    anchor="w" if column not in {"order", "status"} else "center",
                )
            scrollbar = ttk_module.Scrollbar(tree_frame, orient="vertical", command=self.queue_tree.yview)
            self.queue_tree.configure(yscrollcommand=scrollbar.set)
            self.queue_tree.grid(row=0, column=0, sticky="nsew")
            scrollbar.grid(row=0, column=1, sticky="ns")

            footer = ttk_module.Frame(queue_card, style="Surface.TFrame")
            footer.grid(row=2, column=0, sticky="ew", pady=(14, 0))
            footer.columnconfigure(0, weight=1)
            self.queue_summary_var = tk.StringVar()
            self.set_localized(self.queue_summary_var, "Queue is empty")
            ttk_module.Label(footer, textvariable=self.queue_summary_var, style="CardHint.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Checkbutton(
                footer,
                text="Shortest projects first",
                variable=self.smart_queue_var,
                command=self.save_current_config,
                style="Modern.TCheckbutton",
            ).grid(row=0, column=1, sticky="e", padx=(10, 14))
            self.start_queue_button = ttk_module.Button(
                footer,
                text="Start queue",
                style="Primary.TButton",
                command=self.start_render_queue,
            )
            self.start_queue_button.grid(row=0, column=2, sticky="e")
            self.refresh_queue_tree()

        def build_network_tab(self, parent, ttk_module) -> None:
            parent.columnconfigure(0, weight=1)
            parent.columnconfigure(1, weight=2)

            controller_card = self.make_card(parent, ttk_module, row=0, column=0, sticky="nsew", padx=(0, 12))
            self.network_controller_card = controller_card._glass_shell
            controller_card.columnconfigure(0, weight=1)
            controller_header = ttk_module.Frame(controller_card, style="Surface.TFrame")
            controller_header.grid(row=0, column=0, sticky="ew")
            controller_header.columnconfigure(0, weight=1)
            ttk_module.Label(controller_header, text="Main computer", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Button(controller_header, text="Connect instead", command=lambda: self.set_network_role("connect")).grid(row=0, column=1, sticky="e")
            ttk_module.Label(controller_card, text="Visible main PCs can be selected on the LAN. New devices pair once and reconnect automatically.", style="CardHint.TLabel", wraplength=350).grid(row=1, column=0, sticky="w", pady=(3, 12))
            ttk_module.Label(controller_card, text="Main PC name", style="Field.TLabel").grid(row=2, column=0, sticky="w")
            ttk_module.Entry(controller_card, textvariable=self.controller_name_var).grid(row=3, column=0, sticky="ew", pady=(4, 10))
            host_transport = ttk_module.Frame(controller_card, style="Surface.TFrame")
            host_transport.grid(row=4, column=0, sticky="ew", pady=(0, 10))
            host_transport.columnconfigure(1, weight=1)
            ttk_module.Label(host_transport, text="Connection type", style="Field.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
            self.network_transport_combo = ttk_module.Combobox(
                host_transport,
                textvariable=self.network_transport_var,
                values=self.localized_network_transport_labels(),
                state="readonly",
            )
            self.network_transport_combo.grid(row=0, column=1, columnspan=2, sticky="ew")
            self.network_transport_combo.bind("<<ComboboxSelected>>", self.change_network_transport)
            ttk_module.Label(host_transport, textvariable=self.openssh_status_var, style="CardHint.TLabel", wraplength=350).grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
            ttk_module.Button(host_transport, text="Install OpenSSH", command=self.install_openssh).grid(row=2, column=0, sticky="ew", pady=(8, 0))
            ttk_module.Button(host_transport, text="Refresh", command=self.refresh_openssh_status).grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))
            ttk_module.Label(host_transport, text="SSH address", style="Field.TLabel").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
            ttk_module.Entry(host_transport, textvariable=self.ssh_host_var).grid(row=3, column=1, sticky="ew", pady=(8, 0))
            ttk_module.Entry(host_transport, textvariable=self.ssh_port_var, width=6).grid(row=3, column=2, padx=(8, 0), pady=(8, 0))
            ttk_module.Label(host_transport, text="SSH user", style="Field.TLabel").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
            ttk_module.Entry(host_transport, textvariable=self.ssh_user_var).grid(row=4, column=1, columnspan=2, sticky="ew", pady=(8, 0))
            ttk_module.Label(controller_card, text="Advanced connection code", style="Field.TLabel").grid(row=5, column=0, sticky="w")
            ttk_module.Entry(controller_card, textvariable=self.network_code_var, state="readonly").grid(row=6, column=0, sticky="ew", pady=(4, 0))
            controller_actions = ttk_module.Frame(controller_card, style="Surface.TFrame")
            controller_actions.grid(row=7, column=0, sticky="ew", pady=(12, 0))
            controller_actions.columnconfigure(0, weight=1)
            controller_actions.columnconfigure(1, weight=1)
            ttk_module.Button(controller_actions, text="Start controller", style="Primary.TButton", command=self.start_network_controller).grid(row=0, column=0, sticky="ew", padx=(0, 4))
            ttk_module.Button(controller_actions, text="Copy code", command=self.copy_network_code).grid(row=0, column=1, sticky="ew", padx=(4, 0))
            ttk_module.Button(controller_actions, text="Stop controller", command=self.stop_network_controller).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
            lan_settings = ttk_module.Frame(controller_card, style="Surface.TFrame")
            lan_settings.grid(row=8, column=0, sticky="ew", pady=(10, 0))
            lan_settings.columnconfigure(0, weight=1)
            ttk_module.Checkbutton(
                lan_settings,
                text="Show this main PC on the local network",
                variable=self.network_advertise_lan_var,
                command=self.apply_lan_visibility_settings,
                style="Modern.TCheckbutton",
            ).grid(row=0, column=0, columnspan=2, sticky="w")
            ttk_module.Checkbutton(
                lan_settings,
                text="Require a one-time code for new devices",
                variable=self.network_require_pairing_var,
                command=self.apply_lan_visibility_settings,
                style="Modern.TCheckbutton",
            ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
            self.network_pairing_pin_label = ttk_module.Label(lan_settings, text="One-time code: {code}", style="Status.TLabel")
            self.network_pairing_pin_label.grid(row=2, column=0, sticky="w", pady=(8, 0))
            ttk_module.Button(lan_settings, text="New code", command=self.rotate_network_pairing_pin).grid(row=2, column=1, sticky="e", padx=(8, 0), pady=(8, 0))
            ttk_module.Checkbutton(
                controller_card,
                text="Use this computer for rendering",
                variable=self.network_use_local_var,
                command=self.save_current_config,
                style="Modern.TCheckbutton",
            ).grid(row=9, column=0, sticky="w", pady=(10, 0))
            range_mode = ttk_module.Frame(controller_card, style="Surface.TFrame")
            range_mode.grid(row=10, column=0, sticky="ew", pady=(10, 0))
            range_mode.columnconfigure(0, weight=1)
            range_mode.columnconfigure(1, weight=1)
            ttk_module.Button(range_mode, text="Continue missing frames", command=lambda: self.set_network_range_mode("resume")).grid(row=0, column=0, sticky="ew", padx=(0, 4))
            ttk_module.Button(range_mode, text="Manual frame range", command=lambda: self.set_network_range_mode("manual")).grid(row=0, column=1, sticky="ew", padx=(4, 0))
            ttk_module.Label(range_mode, textvariable=self.network_range_mode_label_var, style="CardHint.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
            manual_range = ttk_module.Frame(controller_card, style="Surface.TFrame")
            manual_range.grid(row=11, column=0, sticky="ew", pady=(8, 0))
            ttk_module.Label(manual_range, text="from", style="Field.TLabel").grid(row=0, column=0)
            ttk_module.Entry(manual_range, textvariable=self.network_manual_start_var, width=9).grid(row=0, column=1, padx=(6, 10))
            ttk_module.Label(manual_range, text="to", style="Field.TLabel").grid(row=0, column=2)
            ttk_module.Entry(manual_range, textvariable=self.network_manual_end_var, width=9).grid(row=0, column=3, padx=(6, 0))
            self.network_manual_range_frame = manual_range
            render_actions = ttk_module.Frame(controller_card, style="Surface.TFrame")
            render_actions.grid(row=12, column=0, sticky="ew", pady=(10, 0))
            render_actions.columnconfigure(0, weight=1)
            render_actions.columnconfigure(1, weight=1)
            ttk_module.Button(render_actions, text="Start render", style="Primary.TButton", command=self.start_network_render).grid(row=0, column=0, sticky="ew", padx=(0, 4))
            ttk_module.Button(render_actions, text="Stop render", style="Danger.TButton", command=self.stop_network_render).grid(row=0, column=1, sticky="ew", padx=(4, 0))
            ttk_module.Label(controller_card, textvariable=self.network_status_var, style="CardHint.TLabel", wraplength=350).grid(row=13, column=0, sticky="w", pady=(10, 0))
            network_access = ttk_module.Frame(controller_card, style="Surface.TFrame")
            network_access.grid(row=14, column=0, sticky="ew", pady=(14, 0))
            network_access.columnconfigure(1, weight=1)
            ttk_module.Label(network_access, text="Code behaviour", style="Field.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
            self.network_access_mode_combo = ttk_module.Combobox(
                network_access,
                textvariable=self.access_mode_var,
                values=self.localized_access_mode_labels(),
                state="readonly",
                width=17,
            )
            self.network_access_mode_combo.grid(row=0, column=1, sticky="ew")
            self.network_access_mode_combo.bind("<<ComboboxSelected>>", self.change_access_mode)
            ttk_module.Button(network_access, text="Apply access", command=self.apply_access_settings).grid(row=0, column=2, padx=(8, 0))
            ttk_module.Label(network_access, text="Saved access key", style="Field.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
            ttk_module.Entry(network_access, textvariable=self.access_key_var).grid(row=1, column=1, sticky="ew", pady=(8, 0))
            ttk_module.Button(network_access, text="New key", command=self.create_new_access_key).grid(row=1, column=2, padx=(8, 0), pady=(8, 0))

            worker_card = self.make_card(parent, ttk_module, row=0, column=0, sticky="nsew", padx=(0, 12))
            self.network_worker_card = worker_card._glass_shell
            worker_card.columnconfigure(0, weight=1)
            worker_header = ttk_module.Frame(worker_card, style="Surface.TFrame")
            worker_header.grid(row=0, column=0, sticky="ew")
            worker_header.columnconfigure(0, weight=1)
            ttk_module.Label(worker_header, text="Connect to main computer", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Button(worker_header, text="Become main", command=lambda: self.set_network_role("host")).grid(row=0, column=1, sticky="e")
            ttk_module.Label(worker_card, text="Select a visible main PC. A saved trusted connection reconnects without asking for a code again.", style="CardHint.TLabel", wraplength=350).grid(row=1, column=0, sticky="w", pady=(3, 12))
            ttk_module.Label(worker_card, text="This PC name", style="Field.TLabel").grid(row=2, column=0, sticky="w")
            ttk_module.Entry(worker_card, textvariable=self.worker_name_var).grid(row=3, column=0, sticky="ew", pady=(4, 10))
            ttk_module.Label(worker_card, text="Main PCs on local network", style="Field.TLabel").grid(row=4, column=0, sticky="w")
            lan_choice = ttk_module.Frame(worker_card, style="Surface.TFrame")
            lan_choice.grid(row=5, column=0, sticky="ew", pady=(4, 8))
            lan_choice.columnconfigure(0, weight=1)
            self.lan_controller_combo = ttk_module.Combobox(lan_choice, textvariable=self.lan_controller_var, state="readonly")
            self.lan_controller_combo.grid(row=0, column=0, sticky="ew")
            ttk_module.Button(lan_choice, text="Refresh", command=self.refresh_lan_controllers).grid(row=0, column=1, padx=(8, 0))
            ttk_module.Label(worker_card, text="One-time code (only for the first connection)", style="Field.TLabel").grid(row=6, column=0, sticky="w")
            ttk_module.Entry(worker_card, textvariable=self.network_pairing_input_var).grid(row=7, column=0, sticky="ew", pady=(4, 8))
            ttk_module.Button(worker_card, text="Connect selected PC", style="Primary.TButton", command=self.connect_selected_lan_controller).grid(row=8, column=0, sticky="ew")
            ttk_module.Label(worker_card, text="Saved or SSH connection code", style="Field.TLabel").grid(row=9, column=0, sticky="w", pady=(12, 0))
            ttk_module.Entry(worker_card, textvariable=self.network_join_code_var).grid(row=10, column=0, sticky="ew", pady=(4, 10))
            worker_actions = ttk_module.Frame(worker_card, style="Surface.TFrame")
            worker_actions.grid(row=11, column=0, sticky="ew")
            ttk_module.Button(worker_actions, text="Connect", style="Primary.TButton", command=self.start_network_worker).grid(row=0, column=0)
            ttk_module.Button(worker_actions, text="Disconnect", command=self.stop_network_worker).grid(row=0, column=1, padx=(8, 0))
            worker_ssh = ttk_module.Frame(worker_card, style="Surface.TFrame")
            worker_ssh.grid(row=12, column=0, sticky="ew", pady=(12, 0))
            worker_ssh.columnconfigure(1, weight=1)
            ttk_module.Label(worker_ssh, textvariable=self.openssh_status_var, style="CardHint.TLabel", wraplength=350).grid(row=0, column=0, columnspan=3, sticky="w")
            ttk_module.Button(worker_ssh, text="Install OpenSSH", command=self.install_openssh).grid(row=1, column=0, sticky="ew", pady=(8, 0))
            ttk_module.Button(worker_ssh, text="Refresh", command=self.refresh_openssh_status).grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))
            ttk_module.Label(worker_ssh, text="SSH private key", style="Field.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(8, 0))
            ttk_module.Entry(worker_ssh, textvariable=self.ssh_identity_var).grid(row=2, column=1, sticky="ew", pady=(8, 0))
            ttk_module.Button(worker_ssh, text="Browse", command=self.browse_ssh_identity).grid(row=2, column=2, padx=(8, 0), pady=(8, 0))

            nodes_card = self.make_card(parent, ttk_module, row=0, column=1, sticky="nsew", padx=(12, 0))
            nodes_card.columnconfigure(0, weight=1)
            nodes_card.rowconfigure(2, weight=1)
            ttk_module.Label(nodes_card, text="Connected devices", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
            network_progress = ttk_module.Frame(nodes_card, style="Surface.TFrame")
            network_progress.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 12))
            network_progress.columnconfigure(0, weight=1)
            ttk_module.Progressbar(
                network_progress,
                variable=self.network_progress_var,
                maximum=100,
                mode="determinate",
                style="Modern.Horizontal.TProgressbar",
            ).grid(row=0, column=0, columnspan=2, sticky="ew")
            ttk_module.Label(network_progress, textvariable=self.network_progress_text_var, style="CardHint.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
            ttk_module.Label(network_progress, textvariable=self.network_eta_var, style="CardHint.TLabel").grid(row=1, column=1, sticky="e", padx=(12, 0), pady=(6, 0))
            columns = ("name", "state", "hardware", "current", "done", "average", "samples", "range")
            self.network_tree = ttk_module.Treeview(nodes_card, columns=columns, show="headings", style="Queue.Treeview", selectmode="browse")
            headings = {"name": "Device", "state": "Status", "hardware": "Hardware", "current": "Frame", "done": "Done", "average": "Avg", "samples": "Samples", "range": "Allocation"}
            widths = {"name": 140, "state": 70, "hardware": 180, "current": 55, "done": 55, "average": 70, "samples": 70, "range": 95}
            for column in columns:
                self.register_heading(self.network_tree, column, headings[column])
                self.network_tree.column(column, width=widths[column], anchor="center" if column in {"state", "current", "done", "average", "samples", "range"} else "w")
            tree_y = ttk_module.Scrollbar(nodes_card, orient="vertical", command=self.network_tree.yview)
            tree_x = ttk_module.Scrollbar(nodes_card, orient="horizontal", command=self.network_tree.xview)
            self.network_tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
            self.network_tree.grid(row=2, column=0, sticky="nsew")
            tree_y.grid(row=2, column=1, sticky="ns")
            tree_x.grid(row=3, column=0, sticky="ew", pady=(6, 0))
            self.network_tree.bind("<<TreeviewSelect>>", self.on_network_device_selected)
            ttk_module.Label(
                nodes_card,
                text="Select a device to open its render settings",
                style="CardHint.TLabel",
            ).grid(row=4, column=0, sticky="w", pady=(10, 0))
            self.root.after_idle(self.update_network_role_view)
            self.root.after_idle(self.update_network_range_mode_view)
            self.root.after_idle(self.update_pairing_pin_label)

        def build_insights_tab(self, parent, ttk_module) -> None:
            parent.columnconfigure(0, weight=1)
            parent.columnconfigure(1, weight=1)
            parent.rowconfigure(1, weight=1)
            prediction = self.make_card(parent, ttk_module, row=0, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))
            prediction.columnconfigure(0, weight=1)
            ttk_module.Label(prediction, text="Render prediction", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(prediction, text="Uses project history when available and a scene heuristic otherwise.", style="CardHint.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 12))
            ttk_module.Label(prediction, textvariable=self.prediction_var, style="Status.TLabel", wraplength=500).grid(row=2, column=0, sticky="w")
            ttk_module.Label(prediction, textvariable=self.memory_prediction_var, style="CardHint.TLabel").grid(row=3, column=0, sticky="w", pady=(6, 12))
            ttk_module.Button(prediction, text="Analyze scene", style="Primary.TButton", command=self.analyze_current_project).grid(row=4, column=0, sticky="w")

            autofix = self.make_card(parent, ttk_module, row=0, column=1, sticky="nsew", padx=(12, 0), pady=(0, 12))
            autofix.columnconfigure(0, weight=1)
            ttk_module.Label(autofix, text="Auto Fix", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(autofix, text="Checks output, frame range, GPU, disk space, external files and FFmpeg.", style="CardHint.TLabel", wraplength=500).grid(row=1, column=0, sticky="w", pady=(3, 12))
            ttk_module.Label(autofix, textvariable=self.autofix_var, style="CardHint.TLabel", wraplength=500, justify="left").grid(row=2, column=0, sticky="nw")
            fix_actions = ttk_module.Frame(autofix, style="Surface.TFrame")
            fix_actions.grid(row=3, column=0, sticky="w", pady=(12, 0))
            ttk_module.Button(fix_actions, text="Run preflight", command=self.analyze_current_project).grid(row=0, column=0)
            ttk_module.Button(fix_actions, text="Apply safe fixes", command=self.apply_current_fixes).grid(row=0, column=1, padx=(8, 0))

            history_card = self.make_card(parent, ttk_module, row=1, column=0, sticky="nsew", padx=(0, 12))
            history_card.columnconfigure(0, weight=1)
            history_card.rowconfigure(1, weight=1)
            ttk_module.Label(history_card, text="Render history", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
            columns = ("project", "status", "duration", "frames", "output")
            self.history_tree = ttk_module.Treeview(history_card, columns=columns, show="headings", style="Queue.Treeview")
            for column, title, width in (("project", "Project", 170), ("status", "Status", 80), ("duration", "Time", 80), ("frames", "Frames", 60), ("output", "Output", 230)):
                self.register_heading(self.history_tree, column, title)
                self.history_tree.column(column, width=width, anchor="w")
            self.history_tree.grid(row=1, column=0, sticky="nsew")

            hard_card = self.make_card(parent, ttk_module, row=1, column=1, sticky="nsew", padx=(12, 0))
            hard_card.columnconfigure(0, weight=1)
            hard_card.rowconfigure(1, weight=1)
            ttk_module.Label(hard_card, text="Hardest frames", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
            self.hardest_tree = ttk_module.Treeview(hard_card, columns=("frame", "duration"), show="headings", style="Queue.Treeview")
            self.register_heading(self.hardest_tree, "frame", "Frame")
            self.register_heading(self.hardest_tree, "duration", "Render time")
            self.hardest_tree.column("frame", width=100, anchor="center")
            self.hardest_tree.column("duration", width=160, anchor="center")
            self.hardest_tree.grid(row=1, column=0, sticky="nsew")
            self.refresh_history_views()

        def build_sandbox_tab(self, parent, ttk_module) -> None:
            parent.columnconfigure(0, weight=1)
            parent.columnconfigure(1, weight=2)
            parent.rowconfigure(0, weight=1)
            controls = self.make_card(parent, ttk_module, row=0, column=0, sticky="nsew", padx=(0, 12))
            controls.columnconfigure(0, weight=1)
            ttk_module.Label(controls, text="Render Sandbox", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(controls, text="Test one frame with Draft, Balanced and Quality settings, then compare speed and quality proxy.", style="CardHint.TLabel", wraplength=340).grid(row=1, column=0, sticky="w", pady=(3, 16))
            ttk_module.Label(controls, text="Test frame", style="Field.TLabel").grid(row=2, column=0, sticky="w")
            ttk_module.Entry(controls, textvariable=self.sandbox_frame_var, width=10).grid(row=3, column=0, sticky="w", pady=(4, 10))
            ttk_module.Checkbutton(controls, text="Run variants in parallel", variable=self.sandbox_parallel_var, style="Modern.TCheckbutton").grid(row=4, column=0, sticky="w")
            ttk_module.Label(controls, text="Parallel mode is faster but can exhaust GPU memory.", style="CardHint.TLabel", wraplength=340).grid(row=5, column=0, sticky="w", pady=(3, 14))
            ttk_module.Button(controls, text="Run comparison", style="Primary.TButton", command=self.start_sandbox).grid(row=6, column=0, sticky="w")
            ttk_module.Label(controls, textvariable=self.sandbox_status_var, style="CardHint.TLabel", wraplength=340).grid(row=7, column=0, sticky="w", pady=(14, 0))

            results = self.make_card(parent, ttk_module, row=0, column=1, sticky="nsew", padx=(12, 0))
            results.columnconfigure(0, weight=1)
            results.rowconfigure(1, weight=1)
            ttk_module.Label(results, text="Comparison results", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
            columns = ("variant", "samples", "resolution", "time", "quality", "output")
            self.sandbox_tree = ttk_module.Treeview(results, columns=columns, show="headings", style="Queue.Treeview")
            headings = {"variant": "Variant", "samples": "Samples", "resolution": "Resolution", "time": "Time", "quality": "Quality", "output": "Output"}
            widths = {"variant": 100, "samples": 80, "resolution": 90, "time": 80, "quality": 80, "output": 260}
            for column in columns:
                self.register_heading(self.sandbox_tree, column, headings[column])
                self.sandbox_tree.column(column, width=widths[column], anchor="w")
            self.sandbox_tree.grid(row=1, column=0, sticky="nsew")

        def build_advanced_tab(self, parent, ttk_module) -> None:
            parent.columnconfigure(0, weight=3)
            parent.columnconfigure(1, weight=1)
            parent.rowconfigure(1, weight=1)

            render_card = self.make_card(
                parent,
                ttk_module,
                row=0,
                column=0,
                sticky="ew",
                padx=(0, 12),
                pady=(0, 14),
            )
            render_card.columnconfigure(6, weight=1)
            ttk_module.Label(render_card, text="Render Settings", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=7, sticky="w")
            ttk_module.Label(render_card, text="Frame range", style="Field.TLabel").grid(row=1, column=0, sticky="w", pady=(12, 0))
            ttk_module.Checkbutton(
                render_card,
                text="Use .blend range",
                variable=self.use_scene_range_var,
                command=self.on_scene_range_toggle,
                style="Modern.TCheckbutton",
            ).grid(row=1, column=1, sticky="w", padx=(14, 10), pady=(12, 0))
            range_from_label = ttk_module.Label(render_card, text="from", style="CardHint.TLabel")
            range_from_label.grid(row=1, column=2, sticky="w", padx=(4, 6), pady=(12, 0))
            self.start_frame_entry = ttk_module.Entry(render_card, textvariable=self.start_frame_var, width=9)
            self.start_frame_entry.grid(row=1, column=3, sticky="w", pady=(12, 0))
            range_to_label = ttk_module.Label(render_card, text="to", style="CardHint.TLabel")
            range_to_label.grid(row=1, column=4, sticky="w", padx=(12, 6), pady=(12, 0))
            self.end_frame_entry = ttk_module.Entry(render_card, textvariable=self.end_frame_var, width=9)
            self.end_frame_entry.grid(row=1, column=5, sticky="w", pady=(12, 0))
            self.range_manual_widgets = (
                range_from_label,
                self.start_frame_entry,
                range_to_label,
                self.end_frame_entry,
            )

            ttk_module.Label(render_card, text="Resolution", style="Field.TLabel").grid(row=2, column=0, sticky="w", pady=(10, 0))
            ttk_module.Entry(render_card, textvariable=self.resolution_percent_var, width=9).grid(row=2, column=1, sticky="w", padx=(14, 0), pady=(10, 0))
            ttk_module.Label(render_card, text="%", style="CardHint.TLabel").grid(row=2, column=2, sticky="w", padx=(6, 0), pady=(10, 0))
            ttk_module.Label(render_card, text="Crash retries", style="Field.TLabel").grid(row=2, column=4, sticky="e", padx=(12, 6), pady=(10, 0))
            ttk_module.Entry(render_card, textvariable=self.max_restarts_var, width=7).grid(row=2, column=5, sticky="w", pady=(10, 0))

            ttk_module.Label(render_card, text="Output mode", style="Field.TLabel").grid(row=3, column=0, sticky="w", pady=(10, 0))
            mode_combo = ttk_module.Combobox(render_card, textvariable=self.render_mode_var, values=("frames", "video"), state="readonly", width=12)
            mode_combo.grid(row=3, column=1, sticky="w", padx=(14, 0), pady=(10, 0))
            mode_combo.bind("<<ComboboxSelected>>", self.on_render_mode_changed)
            compose_check = ttk_module.Checkbutton(
                render_card,
                text="Compose frames after render",
                variable=self.compose_video_var,
                command=self.on_compose_video_toggle,
                style="Modern.TCheckbutton",
            )
            compose_check.grid(row=3, column=2, columnspan=2, sticky="w", padx=(10, 0), pady=(10, 0))
            format_label = ttk_module.Label(render_card, text="Format", style="Field.TLabel")
            format_label.grid(row=3, column=4, sticky="e", padx=(10, 6), pady=(10, 0))
            format_combo = ttk_module.Combobox(render_card, textvariable=self.video_format_var, values=tuple(VIDEO_FORMATS), state="readonly", width=15)
            format_combo.grid(row=3, column=5, sticky="w", pady=(10, 0))
            fps_entry = ttk_module.Entry(render_card, textvariable=self.video_fps_var, width=6)
            fps_entry.grid(row=3, column=6, sticky="w", padx=(8, 0), pady=(10, 0))
            self.video_control_widgets = (format_label, format_combo, fps_entry)
            self.update_video_controls()

            options_card = self.make_card(parent, ttk_module, row=1, column=0, sticky="nsew", padx=(0, 12))
            options_card.columnconfigure(1, weight=1)
            ttk_module.Label(options_card, text="Optimization", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
            ttk_module.Label(
                options_card,
                text="Marked options are applied before render starts. Some trade quality for speed.",
                style="CardHint.TLabel",
            ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 16))

            ttk_module.Button(options_card, text="Auto optimize", style="Primary.TButton", command=self.apply_auto_optimization).grid(row=2, column=0, sticky="w", pady=(0, 14))
            ttk_module.Checkbutton(options_card, text="Apply optimization before render", variable=self.optimize_enabled_var, command=self.save_current_config, style="Modern.TCheckbutton").grid(row=2, column=1, sticky="w", padx=(14, 0), pady=(0, 14))

            option_grid = ttk_module.Frame(options_card, style="Surface.TFrame")
            option_grid.grid(row=3, column=0, columnspan=3, sticky="ew")
            option_grid.columnconfigure(0, weight=1)
            option_grid.columnconfigure(1, weight=1)
            option_values = (
                (self.adaptive_var, "Adaptive Sampling"),
                (self.denoise_var, "Denoise"),
                (self.persistent_data_var, "Persistent Data"),
                (self.fast_bounces_var, "Limit Light Bounces"),
                (self.simplify_var, "Simplify Geometry"),
            )
            for index, (variable, title) in enumerate(option_values):
                ttk_module.Checkbutton(
                    option_grid,
                    text=title,
                    variable=variable,
                    command=self.save_current_config,
                    style="Modern.TCheckbutton",
                ).grid(row=index // 2, column=index % 2, sticky="w", pady=5, padx=(0, 12))

            values_row = ttk_module.Frame(options_card, style="Surface.TFrame")
            values_row.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 0))
            ttk_module.Label(values_row, text="Samples", style="Field.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Entry(values_row, textvariable=self.samples_var, width=10).grid(row=0, column=1, sticky="w", padx=(8, 24))
            ttk_module.Label(values_row, text="Tile size", style="Field.TLabel").grid(row=0, column=2, sticky="w")
            ttk_module.Entry(values_row, textvariable=self.tile_size_var, width=10).grid(row=0, column=3, sticky="w", padx=(8, 0))

            info_card = self.make_card(parent, ttk_module, row=0, column=1, rowspan=2, sticky="nsew", padx=(12, 0))
            info_card.columnconfigure(0, weight=1)
            ttk_module.Label(info_card, text="Auto preset", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(
                info_card,
                text="Auto enables the safest speed wins: GPU, adaptive sampling, denoise, persistent data, limited bounces, samples 256, tile 256, full resolution. Simplify stays off because it can visibly change geometry.",
                style="CardHint.TLabel",
                wraplength=300,
                justify="left",
            ).grid(row=1, column=0, sticky="ew", pady=(8, 18))
            ttk_module.Label(info_card, text="Speed checklist", style="CardTitle.TLabel").grid(row=2, column=0, sticky="w")
            ttk_module.Label(
                info_card,
                text="⚡ Strong speedup: GPU, fewer samples, denoise, adaptive sampling.\n\n⚡ Animation speedup: persistent data.\n\n⚠ Quality tradeoff: bounces, simplify, resolution percent.",
                style="CardHint.TLabel",
                wraplength=300,
                justify="left",
            ).grid(row=3, column=0, sticky="ew", pady=(8, 0))

        def build_settings_tab(self, parent, ttk_module) -> None:
            parent.columnconfigure(0, weight=2)
            parent.columnconfigure(1, weight=1)
            parent.rowconfigure(0, weight=1)

            update_card = self.make_card(parent, ttk_module, row=0, column=0, sticky="nsew", padx=(0, 12))
            update_card.columnconfigure(1, weight=1)
            ttk_module.Label(update_card, text="GitHub Updates", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
            ttk_module.Label(
                update_card,
                text="No paste needed: default source is github:prostoodin1/BlenderRenderWatchdog. Advanced users can override it here.",
                style="CardHint.TLabel",
            ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 16))
            ttk_module.Label(update_card, text="Update source", style="Field.TLabel").grid(row=2, column=0, sticky="w", pady=6)
            ttk_module.Entry(update_card, textvariable=self.update_manifest_url_var).grid(row=2, column=1, columnspan=2, sticky="ew", padx=(12, 0), pady=6)
            ttk_module.Checkbutton(
                update_card,
                text="Check updates on start",
                variable=self.check_updates_on_start_var,
                command=self.save_current_config,
                style="Modern.TCheckbutton",
            ).grid(row=3, column=1, sticky="w", padx=(12, 0), pady=(8, 4))
            ttk_module.Checkbutton(
                update_card,
                text="Install updates automatically",
                variable=self.auto_install_updates_var,
                command=self.save_current_config,
                style="Modern.TCheckbutton",
            ).grid(row=4, column=1, sticky="w", padx=(12, 0), pady=(2, 4))
            action_row = ttk_module.Frame(update_card, style="Surface.TFrame")
            action_row.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(14, 0))
            action_row.columnconfigure(3, weight=1)
            self.check_update_button = ttk_module.Button(action_row, text="Check update", command=self.check_for_updates)
            self.check_update_button.grid(row=0, column=0, sticky="w")
            self.install_update_button = ttk_module.Button(action_row, text="Install update", command=self.install_latest_update, state="disabled")
            self.install_update_button.grid(row=0, column=1, sticky="w", padx=(10, 0))
            self.publish_github_button = ttk_module.Button(action_row, text="Publish to GitHub", command=self.publish_to_github)
            self.publish_github_button.grid(row=0, column=2, sticky="w", padx=(10, 0))
            ttk_module.Label(action_row, textvariable=self.update_status_var, style="CardHint.TLabel").grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))

            power_card = self.make_card(parent, ttk_module, row=0, column=1, sticky="nsew", padx=(12, 0))
            power_card.columnconfigure(0, weight=1)
            ttk_module.Label(power_card, text="Power", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(
                power_card,
                text="Optional actions after a successful render. Shutdown starts with a 60 second Windows countdown.",
                style="CardHint.TLabel",
                wraplength=300,
                justify="left",
            ).grid(row=1, column=0, sticky="ew", pady=(8, 16))
            power_options = ttk_module.Frame(power_card, style="Surface.TFrame")
            power_options.grid(row=2, column=0, sticky="ew")
            ttk_module.Checkbutton(
                power_options,
                text="Shutdown PC after successful render",
                variable=self.shutdown_after_render_var,
                command=self.save_current_config,
                style="Modern.TCheckbutton",
            ).grid(row=0, column=0, sticky="w")
            ttk_module.Checkbutton(
                power_options,
                text="Fast transitions",
                variable=self.lightweight_motion_var,
                command=self.apply_motion_preference,
                style="Modern.TCheckbutton",
            ).grid(row=0, column=1, sticky="w", padx=(12, 0))

            ttk_module.Label(power_card, text="Interface", style="CardTitle.TLabel").grid(row=3, column=0, sticky="w", pady=(18, 8))
            language_row = ttk_module.Frame(power_card, style="Surface.TFrame")
            language_row.grid(row=4, column=0, sticky="ew")
            language_row.columnconfigure(1, weight=1)
            ttk_module.Label(language_row, text="Language", style="Field.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
            self.language_combo = ttk_module.Combobox(
                language_row,
                textvariable=self.language_var,
                values=tuple(LANGUAGE_LABELS.values()),
                state="readonly",
                width=18,
            )
            self.language_combo.grid(row=0, column=1, sticky="ew")
            self.language_combo.bind("<<ComboboxSelected>>", self.change_language)

            theme_row = ttk_module.Frame(power_card, style="Surface.TFrame")
            theme_row.grid(row=5, column=0, sticky="ew", pady=(10, 0))
            theme_row.columnconfigure(1, weight=1)
            ttk_module.Label(theme_row, text="Colour theme", style="Field.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 12))
            self.theme_combo = ttk_module.Combobox(
                theme_row,
                textvariable=self.theme_var,
                values=self.localized_theme_labels(),
                state="readonly",
                width=13,
            )
            self.theme_combo.grid(row=0, column=1, sticky="ew")
            self.theme_combo.bind("<<ComboboxSelected>>", self.change_theme)
            ttk_module.Button(
                theme_row,
                text="Choose custom colour",
                command=self.choose_custom_accent,
            ).grid(row=0, column=2, sticky="e", padx=(8, 0))

            mobile_card = self.make_card(parent, ttk_module, row=1, column=0, sticky="ew", padx=(0, 12), pady=(14, 0))
            mobile_card.columnconfigure(1, weight=1)
            ttk_module.Label(mobile_card, text="Mobile dashboard", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
            ttk_module.Label(mobile_card, text="Open the private LAN link on a phone to view progress, previews and control the render.", style="CardHint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 10))
            ttk_module.Entry(mobile_card, textvariable=self.mobile_url_var, state="readonly").grid(row=2, column=0, columnspan=3, sticky="ew")
            ttk_module.Checkbutton(mobile_card, text="Start with the app", variable=self.mobile_enabled_var, command=self.save_current_config, style="Modern.TCheckbutton").grid(row=3, column=0, sticky="w", pady=(10, 0))
            ttk_module.Button(mobile_card, text="Start", command=self.start_mobile_dashboard).grid(row=3, column=1, sticky="e", pady=(10, 0))
            ttk_module.Button(mobile_card, text="Copy link", command=self.copy_mobile_url).grid(row=3, column=2, sticky="e", padx=(8, 0), pady=(10, 0))
            ttk_module.Label(mobile_card, text="Android sync code", style="Field.TLabel").grid(row=4, column=0, sticky="w", pady=(12, 6))
            ttk_module.Entry(mobile_card, textvariable=self.mobile_sync_code_var, state="readonly").grid(row=5, column=0, columnspan=2, sticky="ew")
            ttk_module.Button(mobile_card, text="Copy sync code", command=self.copy_mobile_sync_code).grid(row=5, column=2, sticky="e", padx=(8, 0))

            privacy_card = self.make_card(parent, ttk_module, row=1, column=1, sticky="nsew", padx=(12, 0), pady=(14, 0))
            privacy_card.columnconfigure(0, weight=1)
            ttk_module.Label(privacy_card, text="LAN security", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(
                privacy_card,
                text="Choose fresh codes for every start or keep your own key for stable LAN access.",
                style="CardHint.TLabel",
                wraplength=580,
                justify="left",
            ).grid(row=1, column=0, sticky="ew", pady=(8, 0))
            access_mode_row = ttk_module.Frame(privacy_card, style="Surface.TFrame")
            access_mode_row.grid(row=2, column=0, sticky="ew", pady=(12, 0))
            access_mode_row.columnconfigure(1, weight=1)
            ttk_module.Label(access_mode_row, text="Code behaviour", style="Field.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
            self.access_mode_combo = ttk_module.Combobox(
                access_mode_row,
                textvariable=self.access_mode_var,
                values=self.localized_access_mode_labels(),
                state="readonly",
                width=18,
            )
            self.access_mode_combo.grid(row=0, column=1, sticky="ew")
            self.access_mode_combo.bind("<<ComboboxSelected>>", self.change_access_mode)
            ttk_module.Button(access_mode_row, text="Apply access", command=self.apply_access_settings).grid(row=0, column=2, sticky="e", padx=(8, 0))
            access_key_row = ttk_module.Frame(privacy_card, style="Surface.TFrame")
            access_key_row.grid(row=3, column=0, sticky="ew", pady=(8, 0))
            access_key_row.columnconfigure(1, weight=1)
            ttk_module.Label(access_key_row, text="Your access key", style="Field.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
            ttk_module.Entry(access_key_row, textvariable=self.access_key_var).grid(row=0, column=1, sticky="ew")
            ttk_module.Button(access_key_row, text="New key", command=self.create_new_access_key).grid(row=0, column=2, sticky="e", padx=(8, 0))
            ttk_module.Checkbutton(
                privacy_card,
                text="Resume after restart",
                variable=self.resume_unfinished_var,
                command=self.on_resume_unfinished_toggle,
                style="Modern.TCheckbutton",
            ).grid(row=4, column=0, sticky="w", pady=(12, 0))
            ttk_module.Label(
                privacy_card,
                textvariable=self.resume_status_var,
                style="CardHint.TLabel",
                wraplength=580,
                justify="left",
            ).grid(row=5, column=0, sticky="ew", pady=(6, 0))
        def add_optimization_check(self, parent, ttk_module, row: int, variable: tk.BooleanVar, title: str, hint: str) -> None:
            ttk_module.Checkbutton(parent, text=title, variable=variable, command=self.save_current_config, style="Modern.TCheckbutton").grid(row=row, column=0, sticky="w", pady=6)
            ttk_module.Label(parent, text=hint, style="CardHint.TLabel").grid(row=row, column=1, columnspan=2, sticky="w", padx=(12, 0), pady=6)

        def on_tab_mousewheel(self, event) -> str | None:
            widget_class = str(getattr(event, "widget", self.root).winfo_class())
            if widget_class in {"Text", "Treeview", "Listbox"}:
                return None
            index = getattr(self.notebook, "current_index", -1)
            if not 0 <= index < len(self.tab_scroll_canvases):
                return None
            self.tab_scroll_refreshers[index]()
            canvas = self.tab_scroll_canvases[index]
            if int(canvas.bbox("all")[3] if canvas.bbox("all") else 0) <= canvas.winfo_height():
                return None
            direction = -1 if int(getattr(event, "delta", 0)) > 0 else 1
            canvas.yview_scroll(direction * 3, "units")
            return "break"

        def refresh_tab_scroll_regions(self) -> None:
            for refresh in tuple(self.tab_scroll_refreshers):
                try:
                    refresh()
                except tk.TclError:
                    continue

        def make_card(self, parent, ttk_module, row: int, column: int, sticky: str = "nsew", padx=0, pady=0, rowspan: int = 1):
            card = GlassCard(
                parent,
                palette=self.colors,
                padding=18,
                radius=24,
                backdrop=self.colors["bg"],
                effects_enabled=not self.lightweight_motion_var.get(),
            )
            card.grid(row=row, column=column, rowspan=rowspan, sticky=sticky, padx=padx, pady=pady)
            self.glass_cards.append(card)
            self.card_reveal_index += 1
            return card.content

        def add_path_row(self, parent, ttk_module, row: int, label: str, variable: tk.StringVar, command) -> tuple[object, object, object]:
            label_widget = ttk_module.Label(parent, text=label, style="Field.TLabel")
            entry_widget = ttk_module.Entry(parent, textvariable=variable)
            button_widget = ttk_module.Button(parent, text="Browse", command=command)
            label_widget.grid(row=row, column=0, sticky="w", pady=6)
            entry_widget.grid(row=row, column=1, sticky="ew", padx=(12, 8), pady=6)
            button_widget.grid(row=row, column=2, sticky="e", pady=6)
            return label_widget, entry_widget, button_widget

        def choose_blender(self) -> None:
            path = filedialog.askopenfilename(title="Choose blender.exe", filetypes=[("Blender executable", "blender.exe"), ("EXE", "*.exe")])
            if path:
                self.blender_var.set(path)
                self.save_current_config()

        def choose_blend(self) -> None:
            path = filedialog.askopenfilename(title="Choose .blend file", filetypes=[("Blender files", "*.blend"), ("All files", "*.*")])
            if path:
                self.blend_var.set(path)
                existing = self.render_queue.find_by_source(path)
                if existing is not None:
                    self.render_queue.set_active(existing.job_id)
                    self.sync_form_from_queue_job(existing, include_paths=False)
                    self.save_render_queue()
                    self.refresh_queue_tree(existing.job_id)
                self.save_current_config()

        def choose_frames(self) -> None:
            path = filedialog.askdirectory(title="Choose folder with rendered frames")
            if path:
                self.frames_var.set(path)
                self.save_current_config()

        def on_scene_output_toggle(self) -> None:
            self.update_manual_controls()
            self.save_current_config()

        def on_scene_range_toggle(self) -> None:
            self.update_manual_controls()
            self.save_current_config()

        def on_render_mode_changed(self, _event=None) -> None:
            if self.render_mode_var.get() == "video":
                self.compose_video_var.set(True)
            self.update_video_controls()
            self.save_current_config()

        def on_compose_video_toggle(self) -> None:
            self.update_video_controls()
            self.save_current_config()

        def update_video_controls(self) -> None:
            show = self.compose_video_var.get() or self.render_mode_var.get() == "video"
            for widget in getattr(self, "video_control_widgets", ()):
                if show:
                    widget.grid()
                else:
                    widget.grid_remove()

        def animate_window_in(self) -> None:
            try:
                self.root.attributes("-alpha", 0.0)
            except tk.TclError:
                return

            def step(value: int = 0) -> None:
                try:
                    progress = min(1.0, value / 18.0)
                    eased = 1 - (1 - progress) ** 3
                    self.root.attributes("-alpha", eased)
                except tk.TclError:
                    return
                if value < 18:
                    self.root.after(16, lambda: step(value + 1))

            step()

        def animate_tab_change(self, _event=None) -> None:
            self.root.after_idle(self.refresh_tab_scroll_regions)
            if self.lightweight_motion_var.get():
                return

            def reveal_visible_cards() -> None:
                for card in self.glass_cards:
                    if card.winfo_ismapped():
                        card.pulse()

            self.root.after(30, reveal_visible_cards)

        def animate_progress(self, target: float) -> None:
            self.progress_animation_target = max(0.0, min(100.0, target))
            if self.progress_animation_id is not None:
                return

            def step() -> None:
                current = float(self.progress_var.get())
                distance = self.progress_animation_target - current
                if abs(distance) < 0.35:
                    self.progress_var.set(self.progress_animation_target)
                    self.progress_animation_id = None
                    return
                self.progress_var.set(current + distance * 0.24)
                self.progress_animation_id = self.root.after(16, step)

            step()

        def update_manual_controls(self) -> None:
            show_frames = not self.use_scene_output_var.get()
            for widget in getattr(self, "frames_row_widgets", ()):
                if show_frames:
                    widget.grid()
                else:
                    widget.grid_remove()

            show_range = not self.use_scene_range_var.get()
            for widget in getattr(self, "range_manual_widgets", ()):
                if show_range:
                    widget.grid()
                else:
                    widget.grid_remove()

        def initialize_active_project(self) -> None:
            """Restore the queue's canonical project into every workspace control."""
            active = self.render_queue.active
            if active is None:
                blend_text = self.blend_var.get().strip().strip('"')
                blend = Path(blend_text) if blend_text else None
                if blend is not None and blend.exists():
                    start: int | None = None
                    end: int | None = None
                    if not self.use_scene_range_var.get():
                        try:
                            start = int(self.start_frame_var.get()) if self.start_frame_var.get().strip() else None
                            end = int(self.end_frame_var.get()) if self.end_frame_var.get().strip() else None
                        except ValueError:
                            start = end = None
                    try:
                        active = RenderJob(
                            blend_path=str(blend),
                            output_path=self.frames_var.get().strip().strip('"'),
                            use_scene_output=self.use_scene_output_var.get(),
                            use_scene_range=self.use_scene_range_var.get(),
                            start_frame=start,
                            end_frame=end,
                            resolution_percent=self.parse_positive_int(self.resolution_percent_var.get(), 100, 1, 100),
                            render_mode=self.render_mode_var.get(),
                            compose_video=self.compose_video_var.get(),
                            video_format=self.video_format_var.get(),
                            fps=self.parse_positive_float(self.video_fps_var.get(), 24.0, 1.0, 240.0),
                            chunk_mode=self.chunk_mode_var.get(),
                            chunk_size=self.parse_positive_int(self.chunk_size_var.get(), 10, 1, 1000),
                            render_device_mode=self.render_device_mode_var.get(),
                            compute_backend=self.compute_backend_var.get(),
                        )
                        active.refresh_revision()
                        self.render_queue.add(active, activate=True)
                        self.save_render_queue()
                    except ValueError:
                        active = None
            if active is not None:
                self.sync_form_from_queue_job(active)
                self.refresh_queue_tree(active.job_id)
            else:
                self.set_localized(self.active_project_var, "No active project")

        def save_render_queue(self) -> None:
            try:
                self.render_queue.save(QUEUE_PATH)
            except OSError as error:
                self.log_queue.put(f"[WATCHDOG] Could not save queue: {error}")

        def refresh_queue_tree(self, selected_job_id: str | None = None) -> None:
            if not hasattr(self, "queue_tree"):
                return
            for item in self.queue_tree.get_children():
                self.queue_tree.delete(item)
            status_labels = {
                "pending": "Pending",
                "running": "Running",
                "completed": "Complete",
                "failed": "Failed",
                "paused": "Paused",
            }
            for index, job in enumerate(self.render_queue.jobs, start=1):
                active_marker = "●" if job.job_id == self.render_queue.active_job_id else str(index)
                self.queue_tree.insert(
                    "",
                    "end",
                    iid=job.job_id,
                    values=(
                        active_marker,
                        job.project_name,
                        job.range_label,
                        "Video" if job.compose_video else "Frames",
                        format_duration(job.estimated_seconds) if job.estimated_seconds is not None else "—",
                        job.output_label,
                        self.tr(status_labels.get(job.status, job.status.title())),
                    ),
                )
            pending_count = len(self.render_queue.pending())
            total_count = len(self.render_queue.jobs)
            if total_count:
                self.set_localized(
                    self.queue_summary_var,
                    "{total} projects · {pending} waiting",
                    total=total_count,
                    pending=pending_count,
                )
            else:
                self.set_localized(self.queue_summary_var, "Queue is empty")
            active = self.render_queue.active
            if active is not None:
                self.set_raw(self.active_project_var, active.project_name)
            else:
                self.set_localized(self.active_project_var, "No active project")
            selected_job_id = selected_job_id or self.render_queue.active_job_id
            if selected_job_id and self.queue_tree.exists(selected_job_id):
                self.queue_tree.selection_set(selected_job_id)
                self.queue_tree.focus(selected_job_id)

        def sync_form_from_queue_job(self, job: RenderJob, include_paths: bool = True) -> None:
            """Make every workspace panel point at the queue's active project."""
            if include_paths:
                self.blend_var.set(job.blend_path)
                self.frames_var.set(job.output_path)
            self.use_scene_output_var.set(job.use_scene_output)
            self.use_scene_range_var.set(job.use_scene_range)
            self.start_frame_var.set("" if job.start_frame is None else str(job.start_frame))
            self.end_frame_var.set("" if job.end_frame is None else str(job.end_frame))
            self.resolution_percent_var.set(str(job.resolution_percent))
            self.render_mode_var.set(job.render_mode)
            self.compose_video_var.set(job.compose_video)
            self.video_format_var.set(job.video_format)
            self.video_fps_var.set(str(job.fps))
            self.chunk_mode_var.set(job.chunk_mode)
            self.chunk_size_var.set(str(job.chunk_size))
            self.render_device_mode_var.set(job.render_device_mode)
            self.compute_backend_var.set(job.compute_backend)
            self.set_raw(self.active_project_var, job.project_name)
            self.update_manual_controls()
            self.update_video_controls()

        def on_queue_project_selected(self, _event=None) -> None:
            job_id = self.selected_queue_job_id()
            if not job_id:
                return
            try:
                job = self.render_queue.set_active(job_id)
            except KeyError:
                return
            self.sync_form_from_queue_job(job)
            self.save_render_queue()
            self.save_current_config()

        def queue_job_from_current(self, blend_path: Path | None = None) -> RenderJob | None:
            blend_text = str(blend_path or self.blend_var.get().strip().strip('"'))
            if not blend_text or not Path(blend_text).exists():
                messagebox.showerror(self.tr("Blend file not found"), self.tr("Choose an existing .blend file first."))
                return None

            frame_range = self.frame_range_values()
            if frame_range is None:
                return None
            start_frame, end_frame = frame_range
            output_path = self.frames_var.get().strip().strip('"')
            if not self.use_scene_output_var.get() and not output_path:
                messagebox.showerror(self.tr("Frames folder missing"), self.tr("Choose an output folder or enable the .blend output path."))
                return None
            fps = self.parse_positive_float(self.video_fps_var.get(), 24.0, 1.0, 240.0)
            job = RenderJob(
                blend_path=blend_text,
                output_path=output_path,
                use_scene_output=self.use_scene_output_var.get(),
                use_scene_range=self.use_scene_range_var.get(),
                start_frame=start_frame,
                end_frame=end_frame,
                resolution_percent=self.parse_positive_int(self.resolution_percent_var.get(), 100, 1, 100),
                render_mode=self.render_mode_var.get(),
                compose_video=self.compose_video_var.get(),
                video_format=self.video_format_var.get(),
                fps=fps,
                chunk_mode=self.chunk_mode_var.get(),
                chunk_size=self.parse_positive_int(self.chunk_size_var.get(), 10, 1, 1000),
                render_device_mode=self.render_device_mode_var.get(),
                compute_backend=self.compute_backend_var.get(),
            )
            job.refresh_revision()
            estimate_start = start_frame if start_frame is not None else 1
            estimate_end = end_frame if end_frame is not None else 250
            workers = 1 + len(self.network_controller.workers) if self.network_controller else 1
            job.estimated_seconds = estimate_render(
                Path(blend_text), estimate_start, estimate_end, {}, self.render_history, workers=workers
            ).total_seconds
            return job

        def add_current_to_queue(self) -> None:
            if self.queue_running:
                return
            job = self.queue_job_from_current()
            if job is None:
                return
            job = self.render_queue.add_or_update(job, activate=True)
            self.save_render_queue()
            self.refresh_queue_tree(job.job_id)
            self.log(f"[WATCHDOG] Added to queue: {job.project_name}")

        def add_files_to_queue(self) -> None:
            if self.queue_running:
                return
            paths = filedialog.askopenfilenames(
                title="Add .blend files to queue",
                filetypes=[("Blender files", "*.blend"), ("All files", "*.*")],
            )
            last_job: RenderJob | None = None
            for path in paths:
                job = self.queue_job_from_current(Path(path))
                if job is None:
                    break
                last_job = self.render_queue.add_or_update(job, activate=True)
            if last_job:
                self.save_render_queue()
                self.refresh_queue_tree(last_job.job_id)
                self.log(f"[WATCHDOG] Added {len(paths)} project(s) to queue.")

        def selected_queue_job_id(self) -> str | None:
            selection = self.queue_tree.selection()
            return str(selection[0]) if selection else None

        def remove_queue_job(self) -> None:
            if self.queue_running:
                return
            job_id = self.selected_queue_job_id()
            if not job_id or job_id == self.active_queue_job_id:
                return
            if self.render_queue.remove(job_id):
                self.save_render_queue()
                self.refresh_queue_tree()

        def move_queue_job(self, offset: int) -> None:
            job_id = self.selected_queue_job_id()
            if not job_id or self.queue_running:
                return
            if self.render_queue.move(job_id, offset):
                self.save_render_queue()
                self.refresh_queue_tree(job_id)

        def estimate_and_sort_queue(self) -> None:
            if self.queue_running or not self.render_queue.pending():
                return
            blender = Path(self.blender_var.get().strip().strip('"'))
            self.set_localized(self.queue_summary_var, "Analyzing queued projects…")
            threading.Thread(
                target=self.estimate_queue_worker,
                args=(blender, self.smart_queue_var.get()),
                daemon=True,
            ).start()

        def estimate_queue_worker(self, blender: Path, smart_sort: bool) -> None:
            workers = 1 + len(self.network_controller.workers) if self.network_controller else 1
            for job in self.render_queue.pending():
                blend = Path(job.blend_path)
                settings = query_scene_settings(blender, blend, log=lambda message: self.log_queue.put(message)) if blender.exists() and blend.exists() else {}
                settings = settings or {}
                start = job.start_frame if job.start_frame is not None else int(settings.get("frame_start") or 1)
                end = job.end_frame if job.end_frame is not None else int(settings.get("frame_end") or start)
                job.estimated_seconds = estimate_render(blend, start, end, settings, self.render_history, workers=workers).total_seconds
            if smart_sort:
                self.render_queue.smart_sort(shortest_first=True)
            self.save_render_queue()
            self.log_queue.put(("__QUEUE_REFRESH__", 0, ""))

        def bind_config_autosave(self) -> None:
            variables = [
                self.blender_var,
                self.blend_var,
                self.frames_var,
                self.start_frame_var,
                self.end_frame_var,
                self.use_scene_range_var,
                self.use_scene_output_var,
                self.use_cpu_var,
                self.use_gpu_var,
                self.optimize_enabled_var,
                self.auto_optimize_var,
                self.adaptive_var,
                self.denoise_var,
                self.persistent_data_var,
                self.fast_bounces_var,
                self.simplify_var,
                self.tile_size_var,
                self.samples_var,
                self.resolution_percent_var,
                self.max_restarts_var,
                self.render_mode_var,
                self.compose_video_var,
                self.video_format_var,
                self.video_fps_var,
                self.smart_queue_var,
                self.chunk_mode_var,
                self.chunk_size_var,
                self.render_device_mode_var,
                self.compute_backend_var,
                self.update_manifest_url_var,
                self.check_updates_on_start_var,
                self.auto_install_updates_var,
                self.shutdown_after_render_var,
                self.mobile_enabled_var,
                self.language_var,
                self.theme_var,
                self.lightweight_motion_var,
                self.resume_unfinished_var,
                self.network_role_var,
                self.network_transport_var,
                self.network_use_local_var,
                self.network_advertise_lan_var,
                self.network_require_pairing_var,
                self.network_group_name_var,
                self.network_group_security_var,
                self.network_allow_failover_var,
                self.controller_name_var,
                self.worker_name_var,
                self.network_join_code_var,
                self.ssh_host_var,
                self.ssh_port_var,
                self.ssh_user_var,
                self.ssh_identity_var,
                self.network_range_mode_var,
                self.network_manual_start_var,
                self.network_manual_end_var,
                self.access_key_var,
            ]
            for variable in variables:
                variable.trace_add("write", lambda *_: self.schedule_config_save())

        def schedule_config_save(self) -> None:
            if self.config_save_after_id:
                self.root.after_cancel(self.config_save_after_id)
            self.config_save_after_id = self.root.after(350, self.save_current_config)


        def save_current_config(self) -> None:
            self.config_save_after_id = None
            stored_join_code = self.network_join_code_var.get().strip()
            try:
                stored_connection = PairingCode.decode(stored_join_code)
                if stored_connection.ssh_private_key:
                    stored_join_code = stored_connection.without_private_key().invitation_link
            except ValueError:
                pass
            active_group = self.group_registry.active
            if active_group is not None:
                active_group.name = self.network_group_name_var.get().strip()[:80] or active_group.name
                security_mode = self.network_group_security_var.get().strip().lower()
                active_group.security_mode = security_mode if security_mode in {"open", "approval", "code"} else "approval"
                active_group.visible_on_lan = self.network_advertise_lan_var.get()
                active_group.allow_failover = self.network_allow_failover_var.get()
                active_group.ssh_endpoint = self.ssh_host_var.get().strip()
                active_group.updated_at = time.time()
                try:
                    self.group_registry.save(GROUPS_PATH)
                except OSError as error:
                    self.log_queue.put(f"[NETWORK] Could not save render groups: {error}")
            save_config(
                {
                    "blender": self.blender_var.get().strip(),
                    "blend": self.blend_var.get().strip(),
                    "frames": self.frames_var.get().strip(),
                    "start_frame": self.start_frame_var.get().strip(),
                    "end_frame": self.end_frame_var.get().strip(),
                    "use_scene_range": "1" if self.use_scene_range_var.get() else "0",
                    "use_scene_output": "1" if self.use_scene_output_var.get() else "0",
                    "use_cpu": "1" if self.use_cpu_var.get() else "0",
                    "use_gpu": "1" if self.use_gpu_var.get() else "0",
                    "optimize_enabled": "1" if self.optimize_enabled_var.get() else "0",
                    "auto_optimize": "1" if self.auto_optimize_var.get() else "0",
                    "adaptive_sampling": "1" if self.adaptive_var.get() else "0",
                    "denoise": "1" if self.denoise_var.get() else "0",
                    "persistent_data": "1" if self.persistent_data_var.get() else "0",
                    "fast_bounces": "1" if self.fast_bounces_var.get() else "0",
                    "simplify": "1" if self.simplify_var.get() else "0",
                    "samples": self.samples_var.get().strip(),
                    "tile_size": self.tile_size_var.get().strip(),
                    "resolution_percent": self.resolution_percent_var.get().strip(),
                    "max_restarts": self.max_restarts_var.get().strip(),
                    "render_mode": self.render_mode_var.get().strip(),
                    "compose_video": "1" if self.compose_video_var.get() else "0",
                    "video_format": self.video_format_var.get().strip(),
                    "video_fps": self.video_fps_var.get().strip(),
                    "smart_queue": "1" if self.smart_queue_var.get() else "0",
                    "chunk_mode": self.chunk_mode_var.get().strip(),
                    "chunk_size": self.chunk_size_var.get().strip(),
                    "render_device_mode": self.render_device_mode_var.get().strip(),
                    "compute_backend": self.compute_backend_var.get().strip(),
                    "update_manifest_url": self.update_manifest_url_var.get().strip(),
                    "check_updates_on_start": "1" if self.check_updates_on_start_var.get() else "0",
                    "auto_install_updates": "1" if self.auto_install_updates_var.get() else "0",
                    "shutdown_after_render": "1" if self.shutdown_after_render_var.get() else "0",
                    "mobile_enabled": "1" if self.mobile_enabled_var.get() else "0",
                    "language": self.language_code,
                    "theme": self.theme_code,
                    "custom_accent": self.custom_accent,
                    "lightweight_motion": "1" if self.lightweight_motion_var.get() else "0",
                    "resume_unfinished_enabled": "1" if self.resume_unfinished_var.get() else "0",
                    "network_role": self.network_role_var.get(),
                    "network_transport": self.network_transport,
                    "network_use_local": "1" if self.network_use_local_var.get() else "0",
                    "network_advertise_lan": "1" if self.network_advertise_lan_var.get() else "0",
                    "network_require_pairing": "1" if self.network_require_pairing_var.get() else "0",
                    "network_controller_id": self.network_controller_id,
                    "network_group_name": self.network_group_name_var.get().strip(),
                    "network_trusted_devices": encode_trusted_network_devices(self.trusted_network_devices),
                    "network_saved_connections": json.dumps(self.saved_lan_connections, ensure_ascii=False, separators=(",", ":")),
                    "network_controller_name": self.controller_name_var.get().strip(),
                    "network_worker_name": self.worker_name_var.get().strip(),
                    "network_join_code": stored_join_code,
                    "network_range_mode": self.network_range_mode_var.get(),
                    "network_manual_start": self.network_manual_start_var.get().strip(),
                    "network_manual_end": self.network_manual_end_var.get().strip(),
                    "ssh_host": self.ssh_host_var.get().strip(),
                    "ssh_port": self.ssh_port_var.get().strip(),
                    "ssh_user": self.ssh_user_var.get().strip(),
                    "ssh_identity": self.ssh_identity_var.get().strip(),
                    "access_mode": self.access_mode,
                    "access_key": self.access_key_var.get().strip(),
                }
            )

        def publish_to_github(self) -> None:
            candidates = [
                app_target_path().parent / "Publish To GitHub.cmd",
                app_target_path().parent.parent / "Publish To GitHub.cmd",
                Path.cwd() / "Publish To GitHub.cmd",
            ]
            script_path = next((path for path in candidates if path.exists()), None)
            if not script_path:
                messagebox.showerror(self.tr("Publisher missing"), self.tr("Publish To GitHub.cmd was not found near the app."))
                return
            try:
                subprocess.Popen(["cmd.exe", "/c", "start", "", str(script_path)], cwd=str(script_path.parent))
                self.log(f"[WATCHDOG] GitHub publisher started: {script_path}")
            except Exception as error:
                messagebox.showerror(self.tr("Publisher failed"), str(error))


        def check_for_updates(self) -> None:
            raw_update_source = self.update_manifest_url_var.get()
            update_source = normalize_update_source(raw_update_source)
            if raw_update_source.strip() != update_source:
                self.update_manifest_url_var.set(update_source)
            self.save_current_config()
            self.set_localized(self.update_status_var, "Checking for updates...")
            if hasattr(self, "check_update_button"):
                self.check_update_button.configure(state="disabled")
            if hasattr(self, "install_update_button"):
                self.install_update_button.configure(state="disabled")
            threading.Thread(target=self.update_check_worker, args=(update_source,), daemon=True).start()

        def update_check_worker(self, update_source: str) -> None:
            try:
                manifest = fetch_update_manifest(update_source)
                version = str(manifest.get("version") or "").strip()
                if not version:
                    raise ValueError("Manifest does not contain version.")
                if is_newer_version(version):
                    self.log_queue.put(("__UPDATE__", "available", f"Update available: {version}", manifest))
                else:
                    self.log_queue.put(("__UPDATE__", "current", f"Already latest: {APP_VERSION}", manifest))
            except Exception as error:
                self.log_queue.put(("__UPDATE__", "error", f"Update check failed: {error}", None))

        def install_latest_update(self, ask: bool = True) -> None:
            if not self.latest_update_manifest:
                if ask:
                    messagebox.showerror(self.tr("No update"), self.tr("Check updates first."))
                return
            if ask:
                should_install = messagebox.askyesno(
                    self.tr("Install update"),
                    self.tr("The app will close, download the new version and restart. Install update?"),
                )
                if not should_install:
                    return
            try:
                self.save_current_config()
                install_update_from_manifest(self.latest_update_manifest)
                self.root.destroy()
            except Exception as error:
                if ask:
                    messagebox.showerror(self.tr("Update failed"), str(error))
                else:
                    self.log(f"[WATCHDOG] Auto update failed: {error}")

        def apply_auto_optimization(self) -> None:
            self.optimize_enabled_var.set(True)
            self.auto_optimize_var.set(True)
            self.use_gpu_var.set(True)
            self.use_cpu_var.set(False)
            self.adaptive_var.set(True)
            self.denoise_var.set(True)
            self.persistent_data_var.set(True)
            self.fast_bounces_var.set(True)
            self.simplify_var.set(False)
            self.samples_var.set("256")
            self.tile_size_var.set("256")
            self.resolution_percent_var.set("100")
            self.save_current_config()
            self.log("[WATCHDOG] Auto optimization preset selected.")

        def parse_positive_int(self, value: str, fallback: int, minimum: int = 1, maximum: int = 100000) -> int:
            try:
                parsed = int(value.strip())
            except ValueError:
                return fallback
            return max(minimum, min(maximum, parsed))

        def parse_positive_float(self, value: str, fallback: float, minimum: float = 0.1, maximum: float = 100000.0) -> float:
            try:
                parsed = float(value.strip())
            except ValueError:
                return fallback
            return max(minimum, min(maximum, parsed))
        def parse_optional_frame(self, value: str, field_name: str) -> int | None:
            value = value.strip()
            if not value:
                return None
            try:
                frame = int(value)
            except ValueError:
                messagebox.showerror(
                    self.tr("Invalid frame range"),
                    self.tr("{field} must be a number.", field=self.tr(field_name)),
                )
                return None
            if frame < 0:
                messagebox.showerror(
                    self.tr("Invalid frame range"),
                    self.tr("{field} cannot be less than 0.", field=self.tr(field_name)),
                )
                return None
            return frame

        def frame_range_values(self) -> tuple[int | None, int | None] | None:
            if self.use_scene_range_var.get():
                return None, None

            start_frame = self.parse_optional_frame(self.start_frame_var.get(), "Start frame")
            if start_frame is None and self.start_frame_var.get().strip():
                return None
            end_frame = self.parse_optional_frame(self.end_frame_var.get(), "End frame")
            if end_frame is None and self.end_frame_var.get().strip():
                return None
            if start_frame is not None and end_frame is not None and start_frame > end_frame:
                messagebox.showerror(
                    self.tr("Invalid frame range"),
                    self.tr("Start frame cannot be greater than End frame."),
                )
                return None
            return start_frame, end_frame

        def output_folder_values(self, blender: Path, blend: Path, manual_frames: Path) -> tuple[Path, bool] | None:
            if not self.use_scene_output_var.get():
                return manual_frames, True

            settings = query_scene_settings(blender, blend, log=lambda message: self.log(message))
            if not settings:
                messagebox.showerror(
                    self.tr("Scene output not found"),
                    self.tr("Could not read the output path from the .blend file."),
                )
                return None

            output_path = str(settings.get("output_path") or "")
            output_folder = output_folder_from_scene_path(output_path, blend)
            self.log(f"[WATCHDOG] Using .blend output path: {output_path}")
            self.log(f"[WATCHDOG] Watching output folder: {output_folder}")
            return output_folder, False
        def optimization_options(self) -> dict[str, object]:
            samples = self.parse_positive_int(self.samples_var.get(), 256, 1, 100000)
            tile_size = self.parse_positive_int(self.tile_size_var.get(), 256, 16, 4096)
            resolution = self.parse_positive_int(self.resolution_percent_var.get(), 100, 1, 100)
            return {
                "enabled": self.optimize_enabled_var.get(),
                "adaptive_sampling": self.adaptive_var.get(),
                "adaptive_threshold": 0.02,
                "denoise": self.denoise_var.get(),
                "denoiser": "OPENIMAGEDENOISE",
                "persistent_data": self.persistent_data_var.get(),
                "fast_bounces": self.fast_bounces_var.get(),
                "max_bounces": 6,
                "diffuse_bounces": 2,
                "glossy_bounces": 3,
                "transmission_bounces": 4,
                "transparent_bounces": 4,
                "simplify": self.simplify_var.get(),
                "simplify_subdivision": 1,
                "simplify_particles": 0.5,
                "simplify_volumes": 0.5,
                "samples": samples,
                "tile_size": tile_size,
                "resolution_percent": resolution,
                "compute_backend": self.compute_backend_var.get().strip().upper() or "AUTO",
            }

        def validate_paths(self) -> tuple[Path, Path, Path] | None:
            blender_text = self.blender_var.get().strip().strip('"')
            blend_text = self.blend_var.get().strip().strip('"')
            frames_text = self.frames_var.get().strip().strip('"')

            if not frames_text and not self.use_scene_output_var.get():
                messagebox.showerror(
                    self.tr("Frames folder missing"),
                    self.tr("Choose a frames folder or enable Use .blend output path."),
                )
                return None
            if not self.use_cpu_var.get() and not self.use_gpu_var.get():
                messagebox.showerror(
                    self.tr("Render device missing"),
                    self.tr("Choose at least CPU or GPU for rendering."),
                )
                return None

            blender = Path(blender_text)
            blend = Path(blend_text)
            frames = Path(frames_text) if frames_text else blend.parent

            if not blender.exists():
                messagebox.showerror(self.tr("Blender not found"), self.tr("blender.exe was not found. Choose it manually."))
                return None
            if not blend.exists():
                messagebox.showerror(self.tr("Blend file not found"), self.tr("The .blend file was not found."))
                return None
            return blender, blend, frames

        def analyze_current_project(self) -> None:
            blender = Path(self.blender_var.get().strip().strip('"'))
            blend = Path(self.blend_var.get().strip().strip('"'))
            manual_output = Path(self.frames_var.get().strip().strip('"') or blend.parent)
            if not blend.exists() or not blender.exists():
                messagebox.showerror(self.tr("Project missing"), self.tr("Choose an existing Blender executable and .blend project first."))
                return
            frame_range = self.frame_range_values()
            if frame_range is None:
                return
            self.set_localized(self.prediction_var, "Analyzing scene…")
            self.set_localized(self.autofix_var, "Running preflight…")
            threading.Thread(
                target=self.analysis_worker,
                args=(
                    blender,
                    blend,
                    manual_output,
                    frame_range,
                    self.use_scene_output_var.get(),
                    self.use_gpu_var.get(),
                    self.compose_video_var.get() or self.render_mode_var.get() == "video",
                ),
                daemon=True,
            ).start()

        def analysis_worker(
            self,
            blender: Path,
            blend: Path,
            manual_output: Path,
            frame_range: tuple[int | None, int | None],
            use_scene_output: bool,
            use_gpu: bool,
            compose_after: bool,
        ) -> None:
            settings = query_scene_settings(blender, blend, log=lambda message: self.log_queue.put(message)) or {}
            start = frame_range[0] if frame_range[0] is not None else int(settings.get("frame_start") or 1)
            end = frame_range[1] if frame_range[1] is not None else int(settings.get("frame_end") or start)
            output = (
                output_folder_from_scene_path(str(settings.get("output_path") or ""), blend)
                if use_scene_output
                else manual_output
            )
            workers = 1 + len(self.network_controller.workers) if self.network_controller else 1
            prediction = estimate_render(blend, start, end, settings, self.render_history, workers=workers)
            issues = inspect_render_setup(
                blender,
                blend,
                output,
                start,
                end,
                use_gpu,
                compose_after,
                settings,
            )
            self.log_queue.put(("__ANALYSIS__", prediction, issues, output))

        def apply_current_fixes(self) -> None:
            if not self.current_analysis_issues or self.current_analysis_output is None:
                self.analyze_current_project()
                return
            changes = apply_safe_fixes(self.current_analysis_issues, self.current_analysis_output)
            if changes.get("enable_gpu"):
                self.use_gpu_var.set(True)
            fixed = sum(issue.fixed for issue in self.current_analysis_issues)
            self.save_current_config()
            self.set_localized(
                self.autofix_var,
                "Applied {count} safe fix(es). Re-run preflight to verify.",
                count=fixed,
            )
            self.log(f"[AUTO FIX] Applied {fixed} safe fix(es).")

        def save_render_history(self) -> None:
            try:
                self.render_history.save(HISTORY_PATH)
            except OSError as error:
                self.log_queue.put(f"[WATCHDOG] Could not save render history: {error}")

        def refresh_history_views(self) -> None:
            if not hasattr(self, "history_tree"):
                return
            for item in self.history_tree.get_children():
                self.history_tree.delete(item)
            for record in self.render_history.recent(50):
                self.history_tree.insert(
                    "",
                    "end",
                    iid=record.record_id,
                    values=(record.project_name, record.status.title(), format_duration(record.duration_seconds), record.rendered_frames, record.output_path),
                )
            for item in self.hardest_tree.get_children():
                self.hardest_tree.delete(item)
            project = self.blend_var.get().strip().strip('"') or None
            for metric in self.render_history.hardest_frames(project, limit=20):
                self.hardest_tree.insert("", "end", values=(metric.frame, format_duration(metric.duration_seconds)))

        def start_sandbox(self) -> None:
            paths = self.validate_paths()
            if paths is None:
                return
            blender, blend, output = paths
            try:
                frame = int(self.sandbox_frame_var.get().strip())
            except ValueError:
                messagebox.showerror(self.tr("Invalid frame"), self.tr("Sandbox frame must be a number."))
                return
            samples = self.parse_positive_int(self.samples_var.get(), 256, 1, 100000)
            resolution = self.parse_positive_int(self.resolution_percent_var.get(), 100, 1, 100)
            variants = [
                SandboxVariant("Draft", min(32, samples), min(50, resolution)),
                SandboxVariant("Balanced", min(128, samples), min(75, resolution)),
                SandboxVariant("Quality", samples, resolution),
            ]
            self.set_localized(self.sandbox_status_var, "Running sandbox variants…")
            for item in self.sandbox_tree.get_children():
                self.sandbox_tree.delete(item)
            sandbox_output = output / "watchdog_sandbox"
            threading.Thread(
                target=self.sandbox_worker,
                args=(blender, blend, sandbox_output, frame, variants, self.sandbox_parallel_var.get()),
                daemon=True,
            ).start()

        def sandbox_worker(self, blender: Path, blend: Path, output: Path, frame: int, variants: list[SandboxVariant], parallel: bool) -> None:
            try:
                results = run_sandbox(blender, blend, output, frame, variants, parallel=parallel)
                self.log_queue.put(("__SANDBOX__", results, recommend_variant(results), ""))
            except Exception as error:
                self.log_queue.put(("__SANDBOX__", [], None, str(error)))

        def _apply_openssh_state(self, state: OpenSshState) -> None:
            self.openssh_state = state
            self.openssh_status_running = False
            if state.server_running:
                self.set_localized(self.openssh_status_var, "OpenSSH client and server are ready")
            elif state.server_installed:
                self.set_localized(self.openssh_status_var, "OpenSSH server is installed but stopped")
            elif state.client_installed:
                self.set_localized(self.openssh_status_var, "OpenSSH client is ready")
            else:
                self.set_localized(self.openssh_status_var, "OpenSSH is not installed")

        def refresh_openssh_status(self) -> None:
            if self.openssh_status_running:
                return
            self.openssh_status_running = True
            self.set_localized(self.openssh_status_var, "OpenSSH: checking…")

            def check() -> None:
                state = query_openssh_state()
                try:
                    self.root.after(0, lambda: self._apply_openssh_state(state))
                except tk.TclError:
                    pass

            threading.Thread(target=check, name="openssh-status", daemon=True).start()

        def install_openssh(self) -> None:
            install_server = self.network_role_var.get() == "host"
            prompt = "Install the Windows OpenSSH client and server?" if install_server else "Install the Windows OpenSSH client?"
            if not messagebox.askyesno(self.tr("Install OpenSSH"), self.tr(prompt)):
                return
            try:
                launch_openssh_install(install_server)
                self.set_localized(self.openssh_status_var, "OpenSSH installer started with administrator rights")
                self.root.after(5000, self.refresh_openssh_status)
                self.root.after(15000, self.refresh_openssh_status)
            except Exception as error:
                messagebox.showerror(self.tr("OpenSSH installation"), str(error))

        def browse_ssh_identity(self) -> None:
            path = filedialog.askopenfilename(title=self.tr("Choose SSH private key"), filetypes=[("SSH key", "*"), ("All files", "*.*")])
            if path:
                self.ssh_identity_var.set(path)

        def _ssh_share_connection(self, private_key: str = "") -> PairingCode:
            ssh_host = self.ssh_host_var.get().strip()
            ssh_user = self.ssh_user_var.get().strip()
            ssh_port = int(self.ssh_port_var.get().strip() or "22")
            if not ssh_host or not ssh_user or not 1 <= ssh_port <= 65535:
                raise ValueError(self.tr("Enter a valid SSH address, port, and user."))
            if self.network_controller is not None:
                controller_port = self.network_controller.port
                controller_token = self.network_controller.token
            else:
                access = resolve_service_access(self.access_mode, self.access_key_var.get(), "network")
                controller_port = access.port
                controller_token = access.token
            return PairingCode(
                "127.0.0.1",
                controller_port,
                controller_token,
                "ssh",
                ssh_host,
                ssh_port,
                ssh_user,
                private_key,
            )

        def _share_link_for_controller(self, fallback: str) -> str:
            if self.network_transport != "ssh" or not self.ssh_invite_private_key:
                return fallback
            try:
                return self._ssh_share_connection(self.ssh_invite_private_key).invitation_link
            except (TypeError, ValueError):
                return fallback

        def create_ssh_invitation(self) -> None:
            if self.ssh_setup_running:
                return
            if self.network_role_var.get() != "host":
                messagebox.showerror(self.tr("SSH invitation"), self.tr("Switch this device to Main PC before creating an invitation."))
                return
            try:
                connection = self._ssh_share_connection()
            except (TypeError, ValueError) as error:
                messagebox.showerror(self.tr("SSH invitation"), str(error))
                return
            self.ssh_setup_running = True
            self.network_transport = "ssh"
            self.network_transport_var.set(self.tr("SSH tunnel"))
            self.set_localized(self.openssh_status_var, "Creating SSH key and configuring the main PC…")
            group_id = self.network_controller_id or self.group_registry.identity.device_id
            ssh_user = connection.ssh_user

            def setup() -> None:
                try:
                    invite_key = ensure_ssh_invite_key(app_config_dir() / "ssh", group_id)
                    configure_openssh_host(invite_key.public_key, ssh_user)
                    link = PairingCode(
                        connection.host,
                        connection.port,
                        connection.token,
                        connection.transport,
                        connection.ssh_host,
                        connection.ssh_port,
                        connection.ssh_user,
                        invite_key.private_key,
                    ).invitation_link
                    self.root.after(0, lambda: self._finish_ssh_invitation(invite_key.private_key, link))
                except Exception as error:
                    try:
                        self.root.after(0, lambda message=str(error): self._fail_ssh_invitation(message))
                    except tk.TclError:
                        pass

            threading.Thread(target=setup, name="ssh-invitation-setup", daemon=True).start()

        def _finish_ssh_invitation(self, private_key: str, link: str) -> None:
            self.ssh_setup_running = False
            self.ssh_invite_private_key = private_key
            self.ssh_share_invite_var.set(link)
            self.network_code_var.set(link)
            self.root.clipboard_clear()
            self.root.clipboard_append(link)
            self.save_current_config()
            self.set_localized(self.openssh_status_var, "SSH invitation is ready and copied")
            self.set_localized(self.network_status_var, "SSH invitation copied; start the group and share it with trusted devices")
            messagebox.showinfo(
                self.tr("SSH invitation"),
                self.tr("The SSH invitation was copied. It contains a private access key, so share it only with devices you trust."),
            )

        def _fail_ssh_invitation(self, message: str) -> None:
            self.ssh_setup_running = False
            self.set_localized(self.openssh_status_var, "SSH invitation could not be created")
            messagebox.showerror(self.tr("SSH invitation"), message)

        def save_trusted_network_device(self, token: str, name: str) -> None:
            self.trusted_network_devices[token] = name
            try:
                self.root.after(0, self.save_current_config)
            except tk.TclError:
                pass

        def update_pairing_pin_label(self) -> None:
            code = self.network_pairing_pin_var.get() if self.network_require_pairing_var.get() else self.tr("Not required")
            if hasattr(self, "network_pairing_pin_label"):
                self.network_pairing_pin_label.configure(text=self.tr("One-time code: {code}", code=code))

        def rotate_network_pairing_pin(self) -> None:
            if self.network_controller:
                self.network_pairing_pin_var.set(self.network_controller.rotate_pairing_pin())
            self.update_pairing_pin_label()

        def _stop_lan_advertiser(self) -> None:
            if self.lan_advertiser:
                self.lan_advertiser.stop()
            self.lan_advertiser = None

        def current_lan_announcement(self) -> DiscoveredController | None:
            controller = self.network_controller
            if controller is None:
                return None
            active_group = self.group_registry.active
            group_name = active_group.name if active_group else controller.controller_name
            security_mode = active_group.security_mode if active_group else ("code" if controller.require_pairing_code else "open")
            return DiscoveredController(
                self.network_controller_id,
                controller.controller_name,
                controller.advertised_host,
                controller.port,
                controller.require_pairing_code,
                APP_VERSION,
                group_id=active_group.group_id if active_group else self.network_controller_id,
                group_name=group_name,
                coordinator_device_id=self.group_registry.identity.device_id,
                device_count=1 + len([worker for worker in controller.workers.values() if not worker.disabled]),
                security_mode=security_mode,
                joinable=True,
                coordinator_online=True,
            )

        def _start_lan_advertiser(self) -> None:
            self._stop_lan_advertiser()
            controller = self.network_controller
            if not controller or not self.network_advertise_lan_var.get():
                return
            announcement = self.current_lan_announcement()
            if announcement is None:
                return
            try:
                self.lan_advertiser = LanDiscoveryAdvertiser(announcement)
                self.lan_advertiser.start()
            except OSError as error:
                self.lan_advertiser = None
                self.log_queue.put(f"[NETWORK] LAN discovery unavailable: {error}")

        def apply_lan_visibility_settings(self) -> None:
            if self.network_controller:
                self.network_controller.require_pairing_code = self.network_require_pairing_var.get()
                if self.network_controller.require_pairing_code and time.time() > self.network_controller.pairing_pin_expires_at:
                    self.network_controller.rotate_pairing_pin()
                self._start_lan_advertiser()
            self.update_pairing_pin_label()
            self.save_current_config()

        def refresh_lan_controllers(self) -> None:
            if self.lan_discovery_running:
                return
            self.lan_discovery_running = True
            if hasattr(self, "connection_status_icon") and not self.network_controller and not self.network_worker:
                self.connection_status_icon.set_state("searching")
            self.last_lan_discovery_at = time.monotonic()

            def scan() -> None:
                controllers = discover_controllers(timeout=0.8)
                try:
                    self.root.after(0, lambda: self._apply_lan_controllers(controllers))
                except tk.TclError:
                    pass

            threading.Thread(target=scan, name="lan-controller-scan", daemon=True).start()

        def _apply_lan_controllers(self, controllers: list[DiscoveredController]) -> None:
            self.lan_discovery_running = False
            if hasattr(self, "connection_status_icon") and not self.network_controller and not self.network_worker:
                self.connection_status_icon.set_state("searching" if controllers else "offline")
            self.discovered_controllers = {item.effective_group_id: item for item in controllers}
            labels: dict[str, DiscoveredController] = {}
            for item in controllers:
                remembered = item.effective_group_id in self.saved_lan_connections
                lock = self.tr("Code required") if item.requires_code and not remembered else self.tr("Ready")
                online = lock if item.coordinator_online else self.tr("Offline")
                labels[f"{item.effective_group_name} · {item.device_count} devices · {online}"] = item
            self.discovered_controller_labels = labels
            if hasattr(self, "lan_controller_combo"):
                self.lan_controller_combo.configure(values=tuple(labels))
            current = self.lan_controller_var.get()
            if current not in labels:
                self.lan_controller_var.set(next(iter(labels), ""))

        def connect_selected_lan_controller(self) -> None:
            controller = self.discovered_controller_labels.get(self.lan_controller_var.get())
            if controller is None:
                messagebox.showinfo(self.tr("Local network"), self.tr("No main PC was found. Refresh the list or use an advanced connection code."))
                return
            if not controller.joinable or not controller.coordinator_online:
                messagebox.showinfo(self.tr("Local network"), self.tr("This group is remembered, but its coordinator is currently offline."))
                return
            group_id = controller.effective_group_id
            saved = self.saved_lan_connections.get(group_id, "")
            if saved:
                try:
                    old = PairingCode.decode(saved)
                    code = PairingCode(controller.host, controller.port, old.token, "lan").encode()
                    self.network_join_code_var.set(code)
                    self.start_network_worker()
                    return
                except ValueError:
                    self.saved_lan_connections.pop(group_id, None)
            pin = self.network_pairing_input_var.get().strip()
            if controller.requires_code and not pin:
                messagebox.showinfo(self.tr("One-time code"), self.tr("Enter the one-time code shown on the main PC."))
                return

            def pair() -> None:
                try:
                    code = request_pairing(controller.host, controller.port, self.worker_name_var.get().strip() or platform.node(), pin)
                    self.root.after(0, lambda: self._finish_lan_pairing(controller, code))
                except Exception as error:
                    self.root.after(0, lambda message=str(error): messagebox.showerror(self.tr("Worker connection"), message))

            threading.Thread(target=pair, name="lan-pairing", daemon=True).start()

        def _finish_lan_pairing(self, controller: DiscoveredController, code: str) -> None:
            group_id = controller.effective_group_id
            self.saved_lan_connections[group_id] = code
            remembered = self.group_registry.groups.get(group_id)
            if remembered is None:
                remembered = RenderGroup(
                    name=controller.effective_group_name,
                    owner_device_id=controller.coordinator_device_id or group_id,
                    group_id=group_id,
                    security_mode=controller.security_mode,
                    coordinator_device_id=controller.coordinator_device_id,
                )
            else:
                remembered.name = controller.effective_group_name
                remembered.security_mode = controller.security_mode or remembered.security_mode
                remembered.coordinator_device_id = controller.coordinator_device_id or remembered.coordinator_device_id
            remembered.register_device(
                self.group_registry.identity.device_id,
                self.worker_name_var.get().strip() or platform.node(),
                identity_fingerprint=self.group_registry.identity.fingerprint,
                capabilities=self.local_capabilities,
                address=controller.host,
                role="worker",
            )
            self.group_registry.remember_group(remembered)
            self.group_registry.active_group_id = group_id
            self.network_controller_id = group_id
            self.network_group_name_var.set(remembered.name)
            self.refresh_saved_group_selector()
            self.network_join_code_var.set(code)
            self.network_pairing_input_var.set("")
            self.save_current_config()
            self.start_network_worker()

        def set_network_role(self, role: str) -> None:
            self.network_role_var.set("host" if role == "host" else "connect")
            self.update_network_role_view()
            self.save_current_config()

        def update_network_role_view(self) -> None:
            if not hasattr(self, "network_controller_card") or not hasattr(self, "network_worker_card"):
                return
            if self.network_role_var.get() == "host":
                self.network_worker_card.grid_remove()
                self.network_controller_card.grid()
            else:
                self.network_controller_card.grid_remove()
                self.network_worker_card.grid()
            self.root.after_idle(self.refresh_tab_scroll_regions)

        def set_network_range_mode(self, mode: str) -> None:
            self.network_range_mode_var.set("manual" if mode == "manual" else "resume")
            self.update_network_range_mode_view()
            self.save_current_config()

        def update_network_range_mode_view(self) -> None:
            if self.network_range_mode_var.get() == "manual":
                self.set_localized(self.network_range_mode_label_var, "Manual frame range selected")
                if hasattr(self, "network_manual_range_frame"):
                    self.network_manual_range_frame.grid()
            else:
                self.set_localized(self.network_range_mode_label_var, "Continue: skip frames already in the output folder")
                if hasattr(self, "network_manual_range_frame"):
                    self.network_manual_range_frame.grid_remove()
            self.root.after_idle(self.refresh_tab_scroll_regions)

        def start_network_controller(self) -> None:
            if self.network_controller is not None:
                link = self._share_link_for_controller(self.network_controller.invitation_link)
                self.network_code_var.set(link)
                if self.network_transport == "ssh":
                    self.ssh_share_invite_var.set(link)
                return
            try:
                advertised_host = None
                bind_host = "0.0.0.0"
                ssh_host = ""
                ssh_port = 22
                ssh_user = ""
                if self.network_transport == "ssh":
                    state = query_openssh_state()
                    self._apply_openssh_state(state)
                    if os.name == "nt" and not state.server_running:
                        raise RuntimeError(self.tr("Install and start OpenSSH Server on the main PC first."))
                    ssh_host = self.ssh_host_var.get().strip()
                    ssh_user = self.ssh_user_var.get().strip()
                    ssh_port = int(self.ssh_port_var.get().strip() or "22")
                    if not ssh_host or not ssh_user or not 1 <= ssh_port <= 65535:
                        raise ValueError(self.tr("Enter a valid SSH address, port, and user."))
                    bind_host = "0.0.0.0"
                    advertised_host = None
                access = resolve_service_access(
                    self.access_mode,
                    self.access_key_var.get(),
                    "network",
                )
                active_group = self.group_registry.active
                if active_group is not None:
                    active_group.register_device(
                        self.group_registry.identity.device_id,
                        self.controller_name_var.get().strip() or platform.node(),
                        identity_fingerprint=self.group_registry.identity.fingerprint,
                        capabilities=self.local_capabilities,
                        role="coordinator",
                    )
                    active_group.coordinator_device_id = self.group_registry.identity.device_id
                    self.group_registry.save(GROUPS_PATH)
                self.network_controller = RenderCoordinator(
                    bind_host=bind_host,
                    port=access.port,
                    advertised_host=advertised_host,
                    transport=self.network_transport,
                    token=access.token,
                    ssh_host=ssh_host,
                    ssh_port=ssh_port,
                    ssh_user=ssh_user,
                    require_pairing_code=self.network_require_pairing_var.get(),
                    trusted_tokens=set(self.trusted_network_devices),
                    on_trusted_token=self.save_trusted_network_device,
                    controller_name=self.controller_name_var.get().strip() or platform.node(),
                    controller_hardware=f"{self.cpu_name}; {'; '.join(self.gpu_names)}",
                    on_event=lambda message: self.log_queue.put(message),
                    on_frame=self.on_network_frame,
                    group_id=self.network_controller_id,
                    controller_device_id=self.group_registry.identity.device_id,
                )
                code = self.network_controller.start()
                link = self._share_link_for_controller(self.network_controller.invitation_link)
                self.network_code_var.set(link)
                if self.network_transport == "ssh":
                    self.ssh_share_invite_var.set(link)
                self.network_join_code_var.set(code)
                self.network_pairing_pin_var.set(self.network_controller.pairing_pin)
                self.update_pairing_pin_label()
                self._start_lan_advertiser()
                self.set_localized(
                    self.network_status_var,
                    "Controller ready via {connection} · 0/{maximum} devices",
                    connection="SSH" if self.network_transport == "ssh" else "LAN",
                    maximum=MAX_WORKERS,
                )
            except Exception as error:
                self.network_controller = None
                messagebox.showerror(self.tr("Network controller"), str(error))

        def stop_network_controller(self) -> None:
            self._stop_lan_advertiser()
            if self.network_controller:
                if self.network_controller.plan:
                    self.network_controller.plan.stop()
                self.network_controller.stop()
            self.network_controller = None
            active_group = self.group_registry.active
            if active_group is not None:
                local_member = active_group.members.get(self.group_registry.identity.device_id)
                if local_member is not None:
                    local_member.last_seen = 0.0
                active_group.elect_coordinator()
                try:
                    self.group_registry.save(GROUPS_PATH)
                except OSError:
                    pass
            self.network_code_var.set("")
            self.network_pairing_pin_var.set("—")
            self.update_pairing_pin_label()
            self.set_localized(self.network_status_var, "Controller is stopped")

        def stop_network_render(self) -> None:
            if not self.network_controller or not self.network_controller.plan:
                self.set_localized(self.network_status_var, "No network render is running")
                return
            self.network_controller.plan.stop()
            self.set_localized(self.network_status_var, "Network render stopped; active frames may finish")
            self.set_localized(self.status_var, "Stopped")
            self.set_localized(self.status_detail_var, "No new network frames will be assigned")

        def copy_network_code(self) -> None:
            if self.network_role_var.get() == "host" and self.network_transport == "ssh" and not self.ssh_invite_private_key:
                self.create_ssh_invitation()
                return
            code = self.network_code_var.get().strip()
            if code:
                self.root.clipboard_clear()
                self.root.clipboard_append(code)
                self.set_localized(self.network_status_var, "Connection code copied")

        def start_network_render(self) -> None:
            paths = self.validate_paths()
            if paths is None:
                return
            if self.network_controller is None:
                self.start_network_controller()
            if self.network_controller is None:
                return
            if self.network_use_local_var.get() and self.network_worker is None:
                self.network_join_code_var.set(
                    PairingCode("127.0.0.1", self.network_controller.port, self.network_controller.token, "lan").encode()
                )
                self.start_network_worker(confirm=False, name_override=self.controller_name_var.get().strip())
            blender, blend, manual_output = paths
            active_job = self.queue_job_from_current(blend)
            if active_job is None:
                return
            active_job = self.render_queue.add_or_update(active_job, activate=True)
            active_job.status = "running"
            self.active_queue_job_id = active_job.job_id
            self.save_render_queue()
            self.refresh_queue_tree(active_job.job_id)
            settings = query_scene_settings(blender, blend, log=lambda message: self.log(message)) or {}
            output = (
                output_folder_from_scene_path(str(settings.get("output_path") or ""), blend)
                if self.use_scene_output_var.get()
                else manual_output
            )
            scene_start = int(settings.get("frame_start") or 1)
            scene_end = int(settings.get("frame_end") or scene_start)
            completed_frames: set[int] = set()
            if self.network_range_mode_var.get() == "manual":
                start = self.parse_optional_frame(self.network_manual_start_var.get(), "Start frame")
                end = self.parse_optional_frame(self.network_manual_end_var.get(), "End frame")
                if start is None or end is None:
                    messagebox.showerror(self.tr("Invalid frame range"), self.tr("Enter both Start frame and End frame for manual network rendering."))
                    return
                if start > end:
                    messagebox.showerror(self.tr("Invalid frame range"), self.tr("Start frame cannot be greater than End frame."))
                    return
            else:
                start, end = scene_start, scene_end
                completed_frames = {
                    frame
                    for path in rendered_frame_files(output).values()
                    if (frame := frame_number_from_path(path)) is not None and start <= frame <= end
                }
            self.set_localized(self.status_var, "Network setup")
            self.set_localized(self.status_detail_var, "Sharing the selected project with workers")
            self.set_localized(self.network_status_var, "Preparing the original project…")
            controller = self.network_controller
            threading.Thread(
                target=self.prepare_network_plan_worker,
                args=(controller, blend, output, start, end, settings, completed_frames, active_job),
                daemon=True,
            ).start()

        def prepare_network_plan_worker(
            self,
            controller: RenderCoordinator,
            blend: Path,
            output: Path,
            start: int,
            end: int,
            settings: dict[str, object],
            completed_frames: set[int],
            active_job: RenderJob,
        ) -> None:
            try:
                legacy_cache = app_config_dir() / "network_projects"
                shutil.rmtree(legacy_cache, ignore_errors=True)
                if not blend.exists():
                    raise FileNotFoundError(f"Blend file not found: {blend}")
                if self.network_controller is not controller:
                    return
                controller.start_plan(
                    blend,
                    output,
                    start,
                    end,
                    completed_frames,
                    chunk_mode=active_job.chunk_mode,
                    chunk_size=active_job.chunk_size,
                    project_id=active_job.project_id,
                    source_fingerprint=active_job.source_fingerprint,
                )
                self.network_session = RenderSession(str(blend), str(output), start, end, mode="network", settings=settings)
                self.network_history_saved = False
                self.log_queue.put(("__NETWORK_STARTED__", start, end, len(completed_frames)))
            except Exception as error:
                self.log_queue.put(("__NETWORK_ERROR__", str(error), ""))

        def on_network_frame(self, frame: int, path: Path, _duration: float) -> None:
            self.latest_frame_path = path
            if self.network_session:
                self.network_session.mark_frame(frame)
            self.log_queue.put(("__NETWORK_FRAME__", frame, str(path), float(_duration)))

        def start_network_worker(self, confirm: bool = True, name_override: str | None = None) -> None:
            if self.network_worker is not None or self.network_connect_attempt:
                return
            code = self.network_join_code_var.get().strip()
            blender = Path(self.blender_var.get().strip().strip('"'))
            if not code or not blender.exists():
                messagebox.showerror(self.tr("Worker setup"), self.tr("Enter a connection code and choose blender.exe."))
                return
            try:
                connection = PairingCode.decode(code)
            except ValueError as error:
                messagebox.showerror(self.tr("Worker connection"), str(error))
                return
            if confirm and not messagebox.askyesno(
                self.tr("Join render network"),
                self.tr("This computer will download the project and render assigned frames. Ready to connect?"),
            ):
                return
            worker_name = name_override or self.worker_name_var.get().strip() or platform.node()
            if connection.transport != "ssh":
                self._finish_network_worker_connection(code, blender, worker_name)
                return
            state = query_openssh_state()
            self._apply_openssh_state(state)
            if not state.client_installed:
                messagebox.showerror(self.tr("Worker connection"), self.tr("Install the OpenSSH client on this PC first."))
                return
            if connection.ssh_private_key:
                try:
                    embedded_identity = store_invitation_private_key(connection.ssh_private_key, app_config_dir() / "ssh")
                except (OSError, ValueError) as error:
                    messagebox.showerror(self.tr("Worker connection"), str(error))
                    return
                self.ssh_identity_var.set(str(embedded_identity))
                self.network_join_code_var.set(connection.without_private_key().invitation_link)
                self.save_current_config()
            identity_text = self.ssh_identity_var.get().strip().strip('"')
            identity = Path(identity_text) if identity_text else None
            if identity is not None and not identity.is_file():
                messagebox.showerror(self.tr("Worker connection"), self.tr("SSH private key was not found."))
                return
            attempt = uuid.uuid4().hex
            self.network_connect_attempt = attempt
            self.set_localized(self.openssh_status_var, "Connecting SSH tunnel…")

            def connect_ssh() -> None:
                tunnel = SshTunnel(connection.ssh_host, connection.ssh_port, connection.ssh_user, connection.port, identity)
                try:
                    local_port = tunnel.start()
                    worker_code = PairingCode("127.0.0.1", local_port, connection.token, "lan").encode()
                    self.root.after(0, lambda: self._finish_ssh_worker_connection(attempt, tunnel, worker_code, blender, worker_name))
                except Exception as error:
                    tunnel.stop()
                    try:
                        self.root.after(0, lambda message=str(error): self._fail_ssh_worker_connection(attempt, message))
                    except tk.TclError:
                        pass

            threading.Thread(target=connect_ssh, name="ssh-tunnel-connect", daemon=True).start()

        def _finish_ssh_worker_connection(self, attempt: str, tunnel: SshTunnel, code: str, blender: Path, worker_name: str) -> None:
            if self.network_connect_attempt != attempt:
                tunnel.stop()
                return
            self.network_connect_attempt = ""
            self.ssh_tunnel = tunnel
            self.set_localized(self.openssh_status_var, "SSH tunnel connected")
            self._finish_network_worker_connection(code, blender, worker_name)

        def _fail_ssh_worker_connection(self, attempt: str, message: str) -> None:
            if self.network_connect_attempt != attempt:
                return
            self.network_connect_attempt = ""
            messagebox.showerror(self.tr("Worker connection"), message)

        def _finish_network_worker_connection(self, code: str, blender: Path, worker_name: str) -> None:
            try:
                self.network_worker = NetworkWorker(
                    code,
                    blender,
                    name=worker_name,
                    hardware=f"{self.cpu_name}; {'; '.join(self.gpu_names)}",
                    cache_folder=app_config_dir() / "network_worker",
                    on_event=lambda message: self.log_queue.put(message),
                    use_cpu=self.use_cpu_var.get(),
                    use_gpu=self.use_gpu_var.get(),
                    device_id=self.group_registry.identity.device_id,
                    identity_fingerprint=self.group_registry.identity.fingerprint,
                    capabilities=self.local_capabilities,
                    compute_backend=self.compute_backend_var.get(),
                )
                worker = self.network_worker
                threading.Thread(target=self.run_network_worker, args=(worker,), daemon=True).start()
            except Exception as error:
                if self.ssh_tunnel:
                    self.ssh_tunnel.stop()
                self.ssh_tunnel = None
                self.network_worker = None
                messagebox.showerror(self.tr("Worker connection"), str(error))

        def run_network_worker(self, worker: NetworkWorker) -> None:
            try:
                worker.run(stay_connected=True)
            except Exception as error:
                self.log_queue.put(f"[NETWORK] Worker stopped: {error}")
            finally:
                worker.cleanup_cache()
                if self.network_worker is worker:
                    self.network_worker = None
                if self.ssh_tunnel:
                    self.ssh_tunnel.stop()
                    self.ssh_tunnel = None

        def start_local_network_worker(self) -> None:
            if self.network_controller is None:
                self.start_network_controller()
            if self.network_controller:
                self.network_join_code_var.set(
                    PairingCode("127.0.0.1", self.network_controller.port, self.network_controller.token, "lan").encode()
                )
                self.start_network_worker(name_override=self.controller_name_var.get().strip())

        def stop_network_worker(self) -> None:
            self.network_connect_attempt = ""
            if self.network_worker:
                self.network_worker.stop()
            self.network_worker = None
            if self.ssh_tunnel:
                self.ssh_tunnel.stop()
            self.ssh_tunnel = None

        def apply_network_allocation(self) -> None:
            if not self.network_controller:
                return
            selection = self.network_tree.selection()
            if not selection:
                return
            try:
                start = int(self.worker_range_start_var.get()) if self.worker_range_start_var.get().strip() else None
                end = int(self.worker_range_end_var.get()) if self.worker_range_end_var.get().strip() else None
                samples = int(self.worker_samples_var.get()) if self.worker_samples_var.get().strip() else None
                backend = self.worker_backend_var.get().strip() or "AUTO"
                chunk_size = int(self.worker_chunk_size_var.get()) if self.worker_chunk_size_var.get().strip() else None
                worker_id = self.network_device_worker_ids.get(str(selection[0]), "")
                if not worker_id or not self.network_controller.set_worker_settings(
                    worker_id,
                    start,
                    end,
                    samples,
                    compute_backend=backend,
                    chunk_size=chunk_size,
                ):
                    messagebox.showerror(self.tr("Allocation"), self.tr("This device is not available for render settings."))
                    return
                self.set_localized(self.network_status_var, "Device render settings applied")
            except ValueError as error:
                messagebox.showerror(self.tr("Allocation"), str(error))

        def set_selected_worker_auto(self) -> None:
            self.worker_range_start_var.set("")
            self.worker_range_end_var.set("")
            self.apply_network_allocation()

        def on_network_device_selected(self, _event=None) -> None:
            selection = self.network_tree.selection()
            if not selection:
                return
            device = self.network_device_rows.get(str(selection[0]), {})
            self.worker_range_start_var.set("" if device.get("frame_start") is None else str(device["frame_start"]))
            self.worker_range_end_var.set("" if device.get("frame_end") is None else str(device["frame_end"]))
            self.worker_samples_var.set("" if device.get("samples") is None else str(device["samples"]))
            self.worker_backend_var.set(str(device.get("compute_backend") or "AUTO"))
            self.worker_chunk_size_var.set("" if device.get("chunk_size") is None else str(device["chunk_size"]))
            item_id = str(selection[0])
            if self.network_controller and self.network_device_worker_ids.get(item_id):
                self.root.after_idle(lambda selected_item=item_id: self.show_network_device_settings(selected_item))

        def show_network_device_settings(self, item_id: str) -> None:
            device = self.network_device_rows.get(item_id, {})
            if not device:
                return
            if self.network_device_dialog is not None:
                try:
                    self.network_device_dialog.destroy()
                except tk.TclError:
                    pass

            dialog = tk.Toplevel(self.root)
            self.network_device_dialog = dialog
            dialog.title(self.tr("Device settings"))
            dialog.geometry("650x700")
            dialog.resizable(False, False)
            dialog.configure(background=self.colors["bg"])
            dialog.transient(self.root)
            dialog.grab_set()

            def close_dialog() -> None:
                if self.network_device_dialog is dialog:
                    self.network_device_dialog = None
                dialog.destroy()

            ui = GlassWidgetFactory(
                ttk,
                self.colors,
                translator=self.tr,
                register=self.register_localizable_widget,
            )
            shell = GlassCard(
                dialog,
                palette=self.colors,
                padding=22,
                radius=28,
                backdrop=self.colors["bg"],
                effects_enabled=not self.lightweight_motion_var.get(),
            )
            shell.pack(fill="both", expand=True, padx=20, pady=20)
            card = shell.content
            card.columnconfigure(1, weight=1)

            name = str(device.get("name") or self.tr("Device"))
            ui.Label(card, text=name, style="CardTitle.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
            ui.Label(
                card,
                text="Connected device render controls",
                style="CardHint.TLabel",
            ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 16))

            details = (
                ("Status", self.tr("Online") if device.get("online", True) else self.tr("Offline")),
                ("Render hardware", str(device.get("hardware") or "—")),
                ("Current frame", str(device.get("current_frame") or "—")),
                ("Completed frames", str(int(device.get("completed_frames") or 0))),
                ("Average frame time", format_duration(float(device.get("average_seconds") or 0.0))),
            )
            row = 2
            for label, value in details:
                ui.Label(card, text=label, style="Field.TLabel").grid(row=row, column=0, sticky="nw", pady=5)
                ui.Label(card, text=value, style="CardHint.TLabel", wraplength=390, justify="left").grid(row=row, column=1, sticky="w", padx=(14, 0), pady=5)
                row += 1

            mode_var = tk.StringVar(value=str(device.get("render_device") or "GPU + CPU"))
            start_var = tk.StringVar(value="" if device.get("frame_start") is None else str(device["frame_start"]))
            end_var = tk.StringVar(value="" if device.get("frame_end") is None else str(device["frame_end"]))
            samples_var = tk.StringVar(value="" if device.get("samples") is None else str(device["samples"]))
            backend_var = tk.StringVar(value=str(device.get("compute_backend") or "AUTO"))
            chunk_size_var = tk.StringVar(value="" if device.get("chunk_size") is None else str(device["chunk_size"]))

            ui.Label(card, text="Render device", style="Field.TLabel").grid(row=row, column=0, sticky="w", pady=(14, 5))
            ui.Combobox(card, textvariable=mode_var, values=("GPU + CPU", "GPU", "CPU"), state="readonly").grid(row=row, column=1, sticky="ew", padx=(14, 0), pady=(14, 5))
            row += 1

            capabilities = device.get("capabilities") if isinstance(device.get("capabilities"), dict) else {}
            supported_backends = ["AUTO", *[str(value).upper() for value in capabilities.get("compute_backends", [])]]
            ui.Label(card, text="Cycles backend", style="Field.TLabel").grid(row=row, column=0, sticky="w", pady=5)
            ui.Combobox(
                card,
                textvariable=backend_var,
                values=tuple(dict.fromkeys(supported_backends)),
                state="readonly",
            ).grid(row=row, column=1, sticky="ew", padx=(14, 0), pady=5)
            row += 1

            ui.Label(card, text="Frames per assignment", style="Field.TLabel").grid(row=row, column=0, sticky="w", pady=5)
            ui.Combobox(
                card,
                textvariable=chunk_size_var,
                values=("", "1", "5", "10", "20", "50"),
            ).grid(row=row, column=1, sticky="ew", padx=(14, 0), pady=5)
            row += 1

            allocation = ui.Frame(card, style="Surface.TFrame")
            allocation.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0))
            ui.Label(allocation, text="Frame allocation", style="Field.TLabel").grid(row=0, column=0, sticky="w")
            ui.Entry(allocation, textvariable=start_var, width=8).grid(row=0, column=1, padx=(12, 5))
            ui.Label(allocation, text="to", style="CardHint.TLabel").grid(row=0, column=2)
            ui.Entry(allocation, textvariable=end_var, width=8).grid(row=0, column=3, padx=(5, 16))
            ui.Label(allocation, text="Samples", style="Field.TLabel").grid(row=0, column=4)
            ui.Entry(allocation, textvariable=samples_var, width=8).grid(row=0, column=5, padx=(8, 0))
            row += 1

            def apply_dialog_settings(automatic: bool = False) -> None:
                try:
                    start = None if automatic or not start_var.get().strip() else int(start_var.get())
                    end = None if automatic or not end_var.get().strip() else int(end_var.get())
                    samples = int(samples_var.get()) if samples_var.get().strip() else None
                    chunk_size = int(chunk_size_var.get()) if chunk_size_var.get().strip() else None
                    mode = mode_var.get()
                    use_cpu = mode in {"CPU", "GPU + CPU"}
                    use_gpu = mode in {"GPU", "GPU + CPU"}
                    worker_id = self.network_device_worker_ids.get(item_id, "")
                    if not self.network_controller or not worker_id:
                        raise ValueError(self.tr("This device is not available for render settings."))
                    if not self.network_controller.set_worker_settings(
                        worker_id,
                        start,
                        end,
                        samples,
                        use_cpu,
                        use_gpu,
                        compute_backend=backend_var.get(),
                        chunk_size=chunk_size,
                    ):
                        raise ValueError(self.tr("This device is not available for render settings."))
                    self.set_localized(self.network_status_var, "Device render settings applied")
                    self.refresh_network_state()
                    close_dialog()
                except ValueError as error:
                    messagebox.showerror(self.tr("Device settings"), str(error), parent=dialog)

            buttons = ui.Frame(card, style="Surface.TFrame")
            buttons.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(18, 0))
            ui.Button(buttons, text="Apply settings", style="Primary.TButton", command=apply_dialog_settings).grid(row=0, column=0, sticky="w")
            ui.Button(buttons, text="Automatic balancing", command=lambda: apply_dialog_settings(True)).grid(row=0, column=1, sticky="w", padx=(8, 0))
            disconnect_button = ui.Button(
                buttons,
                text="Disconnect from main PC",
                style="Danger.TButton",
                command=lambda: (close_dialog(), self.disconnect_selected_network_device()),
                state="disabled" if device.get("is_controller") else "normal",
            )
            disconnect_button.grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))

            dialog.protocol("WM_DELETE_WINDOW", close_dialog)

        def disconnect_selected_network_device(self) -> None:
            controller = self.network_controller
            if controller is None:
                messagebox.showerror(self.tr("Disconnect device"), self.tr("Start the main computer controller first."))
                return
            selection = self.network_tree.selection()
            if not selection:
                messagebox.showinfo(self.tr("Disconnect device"), self.tr("Select a connected device first."))
                return
            item_id = str(selection[0])
            device = self.network_device_rows.get(item_id, {})
            if device.get("is_controller"):
                messagebox.showinfo(self.tr("Disconnect device"), self.tr("The main computer cannot disconnect itself."))
                return
            worker_id = self.network_device_worker_ids.get(item_id, "")
            name = str(device.get("name") or self.tr("Device"))
            if not worker_id:
                messagebox.showerror(self.tr("Disconnect device"), self.tr("This device is no longer connected."))
                self.refresh_network_state()
                return
            should_disconnect = messagebox.askyesno(
                self.tr("Disconnect device"),
                self.tr(
                    "Disconnect {device} from this render network? Its active frame will return to the queue.",
                    device=name,
                ),
            )
            if not should_disconnect:
                return
            if controller.disconnect_worker(worker_id):
                self.set_localized(self.network_status_var, "Disconnected {device}", device=name)
            else:
                self.set_localized(self.network_status_var, "Device was already disconnected")
            self.refresh_network_state()

        def update_network_progress(self, plan: dict[str, object] | None) -> None:
            if not plan:
                self.network_progress_var.set(0.0)
                self.set_localized(self.network_progress_text_var, "Waiting for network render")
                self.set_localized(self.network_eta_var, "No ETA yet")
                return
            progress = max(0.0, min(100.0, float(plan.get("progress") or 0.0)))
            completed = int(plan.get("completed") or 0)
            total = int(plan.get("total") or 0)
            self.network_progress_var.set(progress)
            self.set_localized(
                self.network_progress_text_var,
                "Network frames {completed}/{total} · {progress}%",
                completed=completed,
                total=total,
                progress=f"{progress:.0f}",
            )
            eta_seconds = float(plan.get("eta_seconds") or 0.0)
            if bool(plan.get("finished")):
                self.set_localized(self.network_eta_var, "Render complete")
            elif eta_seconds > 0:
                self.set_localized(self.network_eta_var, "About {time} left", time=format_duration(eta_seconds))
            else:
                self.set_localized(self.network_eta_var, "Measuring speed…")

        def refresh_network_state(self) -> None:
            controller = self.network_controller
            if hasattr(self, "connection_status_icon"):
                if controller:
                    self.connection_status_icon.set_state("host")
                elif self.network_worker:
                    self.connection_status_icon.set_state("connected")
                elif self.lan_discovery_running:
                    self.connection_status_icon.set_state("searching")
                else:
                    self.connection_status_icon.set_state("offline")
            if controller:
                if controller.require_pairing_code and time.time() > controller.pairing_pin_expires_at:
                    controller.rotate_pairing_pin()
                self.network_pairing_pin_var.set(controller.pairing_pin)
                if self.lan_advertiser:
                    announcement = self.current_lan_announcement()
                    if announcement is not None:
                        self.lan_advertiser.update(announcement)
            self.update_pairing_pin_label()
            if self.network_role_var.get() == "connect" and time.monotonic() - self.last_lan_discovery_at >= 5:
                self.refresh_lan_controllers()
            if hasattr(self, "network_tree"):
                self.network_device_worker_ids: dict[str, str] = {}
                self.network_device_rows: dict[str, dict[str, object]] = {}
                for item in self.network_tree.get_children():
                    self.network_tree.delete(item)
                worker = self.network_worker
                if controller:
                    controller.controller_name = self.controller_name_var.get().strip() or platform.node()
                snapshot = controller.status() if controller else (dict(worker.status_snapshot) if worker else {})
                plan_snapshot = snapshot.get("plan") if isinstance(snapshot, dict) else None
                self.update_network_progress(plan_snapshot if isinstance(plan_snapshot, dict) else None)
                devices = snapshot.get("devices") or snapshot.get("workers") or []
                visible_device_ids: set[str] = set()
                active_group = self.group_registry.active
                if isinstance(devices, list):
                    for device in devices:
                        if not isinstance(device, dict):
                            continue
                        start = device.get("frame_start")
                        end = device.get("frame_end")
                        allocation = self.tr("Auto") if start is None and end is None else f"{start or '…'}-{end or '…'}"
                        name = str(device.get("name") or self.tr("Device"))
                        if device.get("is_controller"):
                            name = f"{name} · {self.tr('Main')}"
                        device_id = str(device.get("device_id") or device.get("worker_id") or name)
                        item_id = device_id
                        visible_device_ids.add(device_id)
                        self.network_device_worker_ids[item_id] = str(device.get("settings_worker_id") or device.get("worker_id") or "")
                        self.network_device_rows[item_id] = device
                        if active_group is not None:
                            raw_capabilities = device.get("capabilities") if isinstance(device.get("capabilities"), dict) else {}
                            active_group.register_device(
                                device_id,
                                str(device.get("name") or name),
                                identity_fingerprint=str(device.get("identity_fingerprint") or ""),
                                capabilities=DeviceCapabilities.from_dict(raw_capabilities),
                                address=str((snapshot.get("controller") or {}).get("host") or "") if isinstance(snapshot.get("controller"), dict) else "",
                                role="coordinator" if device.get("is_controller") else "worker",
                                seen_at=float(device.get("last_seen") or time.time()) if device.get("online", True) else float(device.get("last_seen") or 0.0),
                            )
                        self.network_tree.insert(
                            "",
                            "end",
                            iid=item_id,
                            values=(
                                name,
                                self.tr("Online") if device.get("online", True) else self.tr("Offline"),
                                str(device.get("hardware") or "—"),
                                (
                                    f"{device['current_frames'][0]}–{device['current_frames'][-1]}"
                                    if isinstance(device.get("current_frames"), list) and len(device["current_frames"]) > 1
                                    else device.get("current_frame") or "—"
                                ),
                                int(device.get("completed_frames") or 0),
                                format_duration(float(device.get("average_seconds") or 0.0)),
                                device.get("samples") or self.tr("Scene"),
                                allocation,
                            ),
                        )
                if active_group is not None:
                    for device_id, member in active_group.members.items():
                        if device_id in visible_device_ids:
                            continue
                        remembered = {
                            "device_id": device_id,
                            "name": member.name,
                            "hardware": "; ".join(filter(None, [member.capabilities.cpu, *member.capabilities.gpus])),
                            "online": False,
                            "capabilities": member.capabilities.to_dict(),
                            "compute_backend": "AUTO",
                        }
                        self.network_device_rows[device_id] = remembered
                        self.network_device_worker_ids[device_id] = ""
                        self.network_tree.insert(
                            "",
                            "end",
                            iid=device_id,
                            values=(member.name, self.tr("Offline"), remembered["hardware"] or "—", "—", 0, "—", self.tr("Scene"), self.tr("Auto")),
                        )
                    if time.monotonic() - self.last_group_save_at >= 10:
                        try:
                            self.group_registry.save(GROUPS_PATH)
                            self.last_group_save_at = time.monotonic()
                        except OSError:
                            pass
                if controller:
                    online = sum(
                        not worker.disabled and time.time() - worker.last_seen < WORKER_OFFLINE_SECONDS
                        for worker in controller.workers.values()
                    )
                    self.set_localized(
                        self.network_status_var,
                        "Controller active · {online}/{maximum} devices",
                        online=online,
                        maximum=MAX_WORKERS,
                    )
                    if controller.plan:
                        summary = controller.plan.summary(controller.workers)
                        corrupt_frames = summary.get("corrupt_frames") or []
                        if corrupt_frames and not summary["finished"]:
                            self.set_localized(
                                self.network_status_var,
                                "Integrity check requeued frames: {frames}",
                                frames=", ".join(str(frame) for frame in corrupt_frames),
                            )
                        self.animate_progress(float(summary["progress"]))
                        self.set_localized(
                            self.progress_text_var,
                            "Frames {start}-{end} · {completed}/{total} · {per_minute}/min · {per_hour}/hour",
                            start=summary["start_frame"],
                            end=summary["end_frame"],
                            completed=summary["completed"],
                            total=summary["total"],
                            per_minute=f"{float(summary.get('frames_per_minute') or 0.0):.2f}",
                            per_hour=f"{float(summary.get('frames_per_hour') or 0.0):.1f}",
                        )
                        eta_seconds = float(summary.get("eta_seconds") or 0.0)
                        if eta_seconds > 0:
                            self.set_localized(self.remaining_time_var, "Approx. remaining: {time}", time=format_duration(eta_seconds))
                        elif not summary["finished"]:
                            self.set_localized(self.remaining_time_var, "Measuring network render speed…")
                        if summary["finished"] and not self.network_history_saved and self.network_session:
                            self.set_localized(self.remaining_time_var, "Render complete")
                            status = "completed" if int(summary["failed"]) == 0 else "failed"
                            self.render_history.add(self.network_session.finish(status))
                            self.save_render_history()
                            self.refresh_history_views()
                            self.network_history_saved = True
                            active_job = self.render_queue.active
                            if active_job is not None and active_job.project_id == str(summary.get("project_id") or ""):
                                active_job.status = "completed" if int(summary["failed"]) == 0 else "failed"
                                active_job.error = "" if active_job.status == "completed" else "One or more network frames failed"
                                self.active_queue_job_id = None
                                self.save_render_queue()
                                self.refresh_queue_tree(active_job.job_id)
                            self.set_localized(self.status_var, "Network complete")
                            detail = (
                                "{completed} complete · {failed} failed · integrity verified"
                                if summary.get("integrity_audited")
                                else "{completed} complete · {failed} failed"
                            )
                            self.set_localized(
                                self.status_detail_var,
                                detail,
                                completed=summary["completed"],
                                failed=summary["failed"],
                            )
                            send_notification("Blender Render Watchdog", "Distributed render finished.")
                elif worker and snapshot:
                    controller_info = snapshot.get("controller") or {}
                    controller_name = str(controller_info.get("name") or worker.connection.host) if isinstance(controller_info, dict) else worker.connection.host
                    online = sum(bool(device.get("online", True)) for device in devices if isinstance(device, dict)) if isinstance(devices, list) else 0
                    self.set_localized(
                        self.network_status_var,
                        "Connected to {controller} · {online} devices",
                        controller=controller_name,
                        online=online,
                    )
                    plan = snapshot.get("plan")
                    if isinstance(plan, dict):
                        self.animate_progress(float(plan.get("progress") or 0.0))
                        self.set_localized(
                            self.progress_text_var,
                            "Frames {start}-{end} · {completed}/{total} · {per_minute}/min · {per_hour}/hour",
                            start=plan.get("start_frame") or "—",
                            end=plan.get("end_frame") or "—",
                            completed=plan.get("completed") or 0,
                            total=plan.get("total") or 0,
                            per_minute=f"{float(plan.get('frames_per_minute') or 0.0):.2f}",
                            per_hour=f"{float(plan.get('frames_per_hour') or 0.0):.1f}",
                        )
                        eta_seconds = float(plan.get("eta_seconds") or 0.0)
                        if eta_seconds > 0:
                            self.set_localized(self.remaining_time_var, "Approx. remaining: {time}", time=format_duration(eta_seconds))
                        elif not plan.get("finished"):
                            self.set_localized(self.remaining_time_var, "Measuring network render speed…")
            self.refresh_mobile_state_cache()
            try:
                self.root.after(1000, self.refresh_network_state)
            except tk.TclError:
                pass

        def mobile_state_provider(self) -> dict[str, object]:
            return dict(self.mobile_state_cache)

        def mobile_action_handler(self, action: str) -> tuple[bool, str]:
            if action not in {"pause", "stop", "shutdown"}:
                return False, "Unknown action"
            self.log_queue.put(("__REMOTE_ACTION__", action, ""))
            return True, "Command queued"

        def start_mobile_dashboard(self) -> None:
            if self.mobile_dashboard:
                self.set_raw(self.mobile_url_var, self.mobile_dashboard.public_url)
                return
            try:
                access = resolve_service_access(
                    self.access_mode,
                    self.access_key_var.get(),
                    "mobile",
                )
                self.mobile_dashboard = MobileDashboardServer(
                    state_provider=self.mobile_state_provider,
                    action_handler=self.mobile_action_handler,
                    preview_provider=lambda: self.latest_frame_path,
                    history_provider=lambda: [record.to_dict() for record in self.render_history.recent(50)],
                    port=access.port,
                    token=access.token,
                )
                self.set_raw(self.mobile_url_var, self.mobile_dashboard.start())
                self.set_raw(
                    self.mobile_sync_code_var,
                    MobileSyncCode(
                        host=self.mobile_dashboard.advertised_host,
                        port=self.mobile_dashboard.port,
                        token=self.mobile_dashboard.token,
                        name=self.controller_name_var.get().strip() or platform.node() or "Blender PC",
                        version=APP_VERSION,
                    ).encode(),
                )
                self.log(f"[MOBILE] Dashboard: {self.mobile_url_var.get()}")
            except Exception as error:
                self.mobile_dashboard = None
                self.set_localized(self.mobile_url_var, "Could not start: {error}", error=error)
                self.set_localized(self.mobile_sync_code_var, "Start the mobile service to create a sync code")

        def copy_mobile_url(self) -> None:
            value = self.mobile_url_var.get()
            if value.startswith("http"):
                self.root.clipboard_clear()
                self.root.clipboard_append(value)

        def copy_mobile_sync_code(self) -> None:
            value = self.mobile_sync_code_var.get().strip()
            if value.startswith("BRWM1-"):
                self.root.clipboard_clear()
                self.root.clipboard_append(value)

        def create_new_access_key(self) -> None:
            self.access_key_var.set(generate_access_key())
            self.apply_access_settings()

        def apply_access_settings(self) -> None:
            self.change_access_mode()
            if self.access_mode == ACCESS_MODE_PERSISTENT:
                try:
                    validate_access_key(self.access_key_var.get())
                except ValueError as error:
                    messagebox.showerror(self.tr("LAN security"), str(error))
                    return
            self.save_current_config()
            if self.mobile_dashboard:
                self.mobile_dashboard.stop()
                self.mobile_dashboard = None
                self.start_mobile_dashboard()
            if self.network_controller and self.network_controller.plan is None:
                self.stop_network_controller()
                self.start_network_controller()
            elif self.network_controller:
                self.log("[NETWORK] Access changes will apply after the active network render stops.")

        def refresh_mobile_state_cache(self) -> None:
            queue_text = ", ".join(f"{job.project_name}: {job.status}" for job in self.render_queue.jobs[:8])
            workers = 0
            if self.network_controller:
                workers = sum(
                    not worker.disabled and time.time() - worker.last_seen < WORKER_OFFLINE_SECONDS
                    for worker in self.network_controller.workers.values()
                )
            current_frame = self.current_render_frame
            remaining_frames = 0
            average_seconds = self.render_average_seconds
            if self.network_controller and self.network_controller.plan:
                summary = self.network_controller.plan.summary(self.network_controller.workers)
                remaining_frames = int(summary.get("remaining_frames") or 0)
                running = [task.frame for task in self.network_controller.plan.tasks.values() if task.status == "running"]
                current_frame = min(running) if running else current_frame
                completed_durations = [task.duration_seconds for task in self.network_controller.plan.tasks.values() if task.status == "completed" and task.duration_seconds > 0]
                if completed_durations:
                    average_seconds = sum(completed_durations) / len(completed_durations)
            else:
                try:
                    end_frame = int(self.end_frame_var.get().strip())
                    remaining_frames = max(0, end_frame - int(current_frame or 0)) if current_frame is not None else 0
                except ValueError:
                    remaining_frames = 0
            preview_version = 0
            if self.latest_frame_path and self.latest_frame_path.exists():
                try:
                    preview_version = self.latest_frame_path.stat().st_mtime_ns
                except OSError:
                    preview_version = 0
            self.mobile_state_cache = {
                "device_name": self.controller_name_var.get().strip() or platform.node() or "Blender PC",
                "version": APP_VERSION,
                "project": self.render_queue.active.project_name if self.render_queue.active else "Waiting for render",
                "project_id": self.render_queue.active.project_id if self.render_queue.active else "",
                "status": self.status_var.get(),
                "detail": self.status_detail_var.get(),
                "progress": float(self.progress_var.get()),
                "workers": workers,
                "queue": queue_text or "No queued projects",
                "preview": bool(self.latest_frame_path and self.latest_frame_path.exists()),
                "preview_version": preview_version,
                "current_frame": current_frame,
                "remaining_frames": remaining_frames,
                "average_seconds": average_seconds,
            }

        def handle_remote_action(self, action: str) -> None:
            if action == "pause":
                if self.network_controller and self.network_controller.plan:
                    plan = self.network_controller.plan
                    plan.paused = not plan.paused
                    self.log(f"[MOBILE] Network render {'paused' if plan.paused else 'resumed'}.")
                elif self.is_paused:
                    if self.paused_queue:
                        self.start_render_queue()
                    else:
                        self.start_watchdog()
                else:
                    self.pause_watchdog()
            elif action == "stop":
                if self.network_controller and self.network_controller.plan:
                    self.network_controller.plan.stop()
                self.stop_watchdog()
            elif action == "shutdown":
                self.shutdown_after_render_var.set(True)
                self.save_current_config()
                self.log("[MOBILE] Shutdown after successful render enabled.")

        def schedule_hardware_poll(self) -> None:
            if not self.hardware_poll_running:
                self.hardware_poll_running = True
                threading.Thread(target=self.hardware_poll_worker, daemon=True).start()
            try:
                self.root.after(15000, self.schedule_hardware_poll)
            except tk.TclError:
                pass

        def hardware_poll_worker(self) -> None:
            try:
                cpu, gpus = detect_hardware()
                self.log_queue.put(("__HARDWARE__", cpu, gpus))
            finally:
                self.hardware_poll_running = False

        def start_watchdog(self) -> None:
            paths = self.validate_paths()
            if paths is None:
                return

            frame_range = self.frame_range_values()
            if frame_range is None:
                return

            blender, blend, manual_frames = paths
            start_frame, end_frame = frame_range
            output_values = self.output_folder_values(blender, blend, manual_frames)
            if output_values is None:
                return
            frames, output_override = output_values
            active_job = self.queue_job_from_current(blend)
            if active_job is None:
                return
            active_job = self.render_queue.add_or_update(active_job, activate=True)
            active_job.status = "running"
            active_job.attempts += 1
            active_job.error = ""
            self.active_queue_job_id = active_job.job_id
            self.save_render_queue()
            self.refresh_queue_tree(active_job.job_id)
            self.save_current_config()
            self.stop_event = threading.Event()
            self.pause_event = threading.Event()
            self.is_paused = False
            self.paused_queue = False
            self.progress_var.set(0.0)
            self.set_localized(self.progress_text_var, "Starting")
            self.set_localized(self.remaining_time_var, "Approx. remaining time appears after the first frame")
            self.current_render_frame = None
            self.render_frame_count = 0
            self.render_average_seconds = 0.0
            self.last_frame_observed_at = time.monotonic()
            self.set_localized(self.status_var, "Running")
            self.set_localized(self.status_detail_var, "Blender process is active")
            self.start_button.configure(state="disabled")
            self.pause_button.configure(state="normal")
            self.stop_button.configure(state="normal")
            self.start_queue_button.configure(state="disabled")
            self.log("")
            self.log("Starting watchdog...")
            self.arm_unfinished_resume("single")

            worker_options = self.optimization_options()
            max_restarts = self.parse_positive_int(self.max_restarts_var.get(), 3, 0, 100)
            compose_after = self.compose_video_var.get() or self.render_mode_var.get() == "video"
            video_format = self.video_format_var.get()
            video_fps = self.parse_positive_float(self.video_fps_var.get(), 24.0, 1.0, 240.0)
            self.worker = threading.Thread(
                target=self.watchdog_worker,
                args=(
                    blender,
                    blend,
                    frames,
                    start_frame,
                    end_frame,
                    output_override,
                    self.use_cpu_var.get(),
                    self.use_gpu_var.get(),
                    worker_options,
                    max_restarts,
                    compose_after,
                    video_format,
                    video_fps,
                    active_job.job_id,
                ),
                daemon=True,
            )
            self.worker.start()

        def watchdog_worker(
            self,
            blender: Path,
            blend: Path,
            frames: Path,
            start_frame: int | None,
            end_frame: int | None,
            output_override: bool,
            use_cpu: bool,
            use_gpu: bool,
            optimize_options: dict[str, object],
            max_restarts: int,
            compose_after: bool,
            video_format: str,
            video_fps: float,
            active_job_id: str,
        ) -> None:
            session = RenderSession(
                str(blend),
                str(frames),
                start_frame,
                end_frame,
                mode="video" if compose_after else "frames",
                settings=optimize_options,
            )
            history_recorded = False

            def observe_frame(frame: int, path: Path) -> None:
                session.mark_frame(frame)
                self.latest_frame_path = path
                self.log_queue.put(("__FRAME_METRIC__", frame, str(path)))

            try:
                code = run_watchdog(
                    blender=blender,
                    blend=blend,
                    frames_folder=frames,
                    sleep_seconds=args.sleep,
                    padding=args.padding,
                    start=start_frame,
                    end=end_frame,
                    extra_args=args.extra,
                    stop_event=self.stop_event,
                    pause_event=self.pause_event,
                    log=lambda message: self.log_queue.put(message),
                    progress=lambda percent, text: self.log_queue.put(("__PROGRESS__", percent, text)),
                    use_cpu=use_cpu,
                    use_gpu=use_gpu,
                    optimize_options=optimize_options,
                    output_override=output_override or compose_after,
                    max_restarts=max_restarts,
                    frame_observer=observe_frame,
                )
                if code == 0 and compose_after:
                    destination = video_output_path(frames, blend.stem, video_format)
                    self.log_queue.put(f"[VIDEO] Composing {destination.name}…")
                    video = compose_video(
                        frames,
                        destination,
                        video_fps,
                        video_format,
                        padding=args.padding,
                        start_number=start_frame,
                    )
                    if not video.succeeded:
                        code = video.return_code or 1
                        self.log_queue.put(f"[VIDEO] FFmpeg failed: {video.output[-1200:]}")
                    else:
                        self.log_queue.put(f"[VIDEO] Saved: {destination}")
                status = "completed" if code == 0 else "paused" if code == 131 else "stopped" if code == 130 else "failed"
                active_job = self.render_queue.get(active_job_id)
                if active_job is not None:
                    active_job.status = "pending" if status == "stopped" else status
                    active_job.error = "" if code == 0 else f"Blender exited with code {code}"
                    self.save_render_queue()
                    self.log_queue.put(("__QUEUE_ITEM__", active_job.job_id, active_job.status, active_job.error))
                self.render_history.add(session.finish(status))
                self.save_render_history()
                history_recorded = True
                self.log_queue.put(("__HISTORY_REFRESH__", 0, ""))
                self.log_queue.put(("__FINISHED__", code, "Paused" if code == 131 else "Finished"))
                self.log_queue.put(f"Process finished with code {code}.")
            except Exception as error:
                active_job = self.render_queue.get(active_job_id)
                if active_job is not None:
                    active_job.status = "failed"
                    active_job.error = str(error)
                    self.save_render_queue()
                    self.log_queue.put(("__QUEUE_ITEM__", active_job.job_id, active_job.status, active_job.error))
                self.log_queue.put(f"Error: {error}")
                self.log_queue.put(("__FINISHED__", 1, "Error"))
            finally:
                if self.active_queue_job_id == active_job_id:
                    self.active_queue_job_id = None
                if not history_recorded:
                    self.render_history.add(session.finish("failed"))
                    self.save_render_history()
                    self.log_queue.put(("__HISTORY_REFRESH__", 0, ""))
                self.log_queue.put("__WATCHDOG_DONE__")

        def start_render_queue(self) -> None:
            blender_text = self.blender_var.get().strip().strip('"')
            blender = Path(blender_text)
            if not blender.exists():
                messagebox.showerror(self.tr("Blender not found"), self.tr("Choose blender.exe on the Render tab first."))
                return
            if not self.use_cpu_var.get() and not self.use_gpu_var.get():
                messagebox.showerror(self.tr("Render device missing"), self.tr("Choose at least CPU or GPU for rendering."))
                return

            self.render_queue.reset_unfinished()
            if not self.render_queue.pending():
                messagebox.showinfo(self.tr("Queue complete"), self.tr("There are no waiting projects in the queue."))
                return

            if self.smart_queue_var.get():
                self.render_queue.smart_sort(shortest_first=True)

            self.save_current_config()
            self.save_render_queue()
            self.stop_event = threading.Event()
            self.pause_event = threading.Event()
            self.is_paused = False
            self.paused_queue = False
            self.queue_running = True
            self.progress_var.set(0.0)
            self.set_localized(self.progress_text_var, "Queue starting")
            self.set_localized(self.remaining_time_var, "Approx. remaining time appears after the first frame")
            self.current_render_frame = None
            self.render_frame_count = 0
            self.render_average_seconds = 0.0
            self.last_frame_observed_at = time.monotonic()
            self.set_localized(self.status_var, "Queue")
            self.set_localized(self.status_detail_var, "Preparing the first project")
            self.start_button.configure(state="disabled")
            self.start_queue_button.configure(state="disabled")
            self.pause_button.configure(state="normal")
            self.stop_button.configure(state="normal")
            self.arm_unfinished_resume("queue")

            worker_options = self.optimization_options()
            max_restarts = self.parse_positive_int(self.max_restarts_var.get(), 3, 0, 100)
            self.worker = threading.Thread(
                target=self.render_queue_worker,
                args=(
                    blender,
                    self.use_cpu_var.get(),
                    self.use_gpu_var.get(),
                    worker_options,
                    max_restarts,
                ),
                daemon=True,
            )
            self.worker.start()

        def render_queue_worker(
            self,
            blender: Path,
            use_cpu: bool,
            use_gpu: bool,
            optimize_options: dict[str, object],
            max_restarts: int,
        ) -> None:
            completed = 0
            failed = 0
            active_session: RenderSession | None = None
            try:
                for job in self.render_queue.jobs:
                    if job.status != "pending":
                        continue
                    if self.stop_event and self.stop_event.is_set():
                        break

                    self.active_queue_job_id = job.job_id
                    self.render_queue.set_active(job.job_id)
                    job.status = "running"
                    job.attempts += 1
                    job.error = ""
                    self.save_render_queue()
                    self.log_queue.put(("__QUEUE_ITEM__", job.job_id, job.status, job.error))
                    self.log_queue.put(f"[WATCHDOG] Queue started: {job.project_name}")

                    blend = Path(job.blend_path)
                    if not blend.exists():
                        job.status = "failed"
                        job.error = "Blend file not found"
                        failed += 1
                        self.save_render_queue()
                        self.log_queue.put(("__QUEUE_ITEM__", job.job_id, job.status, job.error))
                        continue

                    if job.use_scene_output:
                        settings = query_scene_settings(
                            blender,
                            blend,
                            log=lambda message: self.log_queue.put(message),
                        )
                        if not settings:
                            job.status = "failed"
                            job.error = "Could not read .blend output path"
                            failed += 1
                            self.save_render_queue()
                            self.log_queue.put(("__QUEUE_ITEM__", job.job_id, job.status, job.error))
                            continue
                        frames = output_folder_from_scene_path(str(settings.get("output_path") or ""), blend)
                        output_override = False
                    else:
                        frames = Path(job.output_path) if job.output_path else blend.parent
                        output_override = True

                    job_options = dict(optimize_options)
                    job_options["resolution_percent"] = job.resolution_percent
                    job_options["compute_backend"] = job.compute_backend
                    if job.render_device_mode == "CPU":
                        job_use_cpu, job_use_gpu = True, False
                    elif job.render_device_mode == "GPU":
                        job_use_cpu, job_use_gpu = False, True
                    elif job.render_device_mode == "CPU_GPU":
                        job_use_cpu, job_use_gpu = True, True
                    else:
                        job_use_cpu, job_use_gpu = use_cpu, use_gpu
                    session = RenderSession(
                        str(blend),
                        str(frames),
                        job.start_frame,
                        job.end_frame,
                        mode="video" if job.compose_video else "frames",
                        settings=job_options,
                    )
                    active_session = session

                    def observe_frame(frame: int, path: Path) -> None:
                        session.mark_frame(frame)
                        self.latest_frame_path = path
                        self.log_queue.put(("__FRAME_METRIC__", frame, str(path)))

                    code = run_watchdog(
                        blender=blender,
                        blend=blend,
                        frames_folder=frames,
                        sleep_seconds=args.sleep,
                        padding=args.padding,
                        start=job.start_frame,
                        end=job.end_frame,
                        extra_args=args.extra,
                        stop_event=self.stop_event,
                        pause_event=self.pause_event,
                        log=lambda message: self.log_queue.put(message),
                        progress=lambda percent, text: self.log_queue.put(("__PROGRESS__", percent, text)),
                        use_cpu=job_use_cpu,
                        use_gpu=job_use_gpu,
                        optimize_options=job_options,
                        output_override=output_override or job.compose_video,
                        max_restarts=max_restarts,
                        frame_observer=observe_frame,
                    )

                    if code == 0 and job.compose_video:
                        destination = video_output_path(frames, blend.stem, job.video_format)
                        self.log_queue.put(f"[VIDEO] Composing {destination.name}…")
                        video = compose_video(
                            frames,
                            destination,
                            job.fps,
                            job.video_format,
                            padding=args.padding,
                            start_number=job.start_frame,
                        )
                        if not video.succeeded:
                            code = video.return_code or 1
                            job.error = video.output[-1200:]
                        else:
                            self.log_queue.put(f"[VIDEO] Saved: {destination}")

                    if code == 0:
                        job.status = "completed"
                        completed += 1
                    elif code == 131:
                        job.status = "paused"
                    elif code == 130:
                        job.status = "pending"
                    else:
                        job.status = "failed"
                        job.error = job.error or f"Blender exited with code {code}"
                        failed += 1
                    self.render_history.add(session.finish(job.status))
                    self.save_render_history()
                    active_session = None
                    self.log_queue.put(("__HISTORY_REFRESH__", 0, ""))
                    self.save_render_queue()
                    self.log_queue.put(("__QUEUE_ITEM__", job.job_id, job.status, job.error))
                    if code in {130, 131}:
                        break
            except Exception as error:
                failed += 1
                self.log_queue.put(f"Error: {error}")
                if active_session is not None:
                    self.render_history.add(active_session.finish("failed"))
                    self.save_render_history()
                    self.log_queue.put(("__HISTORY_REFRESH__", 0, ""))
                    active_session = None
                if self.active_queue_job_id:
                    job = self.render_queue.get(self.active_queue_job_id)
                    if job:
                        job.status = "failed"
                        job.error = str(error)
                        self.save_render_queue()
                        self.log_queue.put(("__QUEUE_ITEM__", job.job_id, job.status, job.error))
            finally:
                self.active_queue_job_id = None
                completed = sum(job.status == "completed" for job in self.render_queue.jobs)
                failed = sum(job.status == "failed" for job in self.render_queue.jobs)
                self.log_queue.put(("__QUEUE_DONE__", completed, failed))
                self.log_queue.put("__WATCHDOG_DONE__")

        def pause_watchdog(self) -> None:
            if self.pause_event:
                self.pause_event.set()
                self.set_localized(self.status_var, "Pausing")
                self.set_localized(self.status_detail_var, "Waiting for current frame to finish")
                self.pause_button.configure(state="disabled")
                self.log("[WATCHDOG] Pause requested. Waiting for current frame to finish...")

        def stop_watchdog(self) -> None:
            if self.stop_event:
                self.stop_event.set()
                self.clear_unfinished_resume()
                self.set_localized(self.status_var, "Stopping")
                self.set_localized(self.status_detail_var, "Terminating Blender safely")
                self.pause_button.configure(state="disabled")
                self.stop_button.configure(state="disabled")
                self.log("Stopping watchdog...")

        def drain_log_queue(self) -> None:
            while True:
                try:
                    message = self.log_queue.get_nowait()
                except queue.Empty:
                    break

                if message == "__WATCHDOG_DONE__":
                    if not self.is_paused:
                        self.set_widget_text(self.start_button, "Start render")
                        self.start_button.configure(state="normal")
                    self.start_queue_button.configure(state="normal")
                    self.pause_button.configure(state="disabled")
                    self.stop_button.configure(state="disabled")
                    continue

                if isinstance(message, tuple) and len(message) == 4 and message[0] == "__ANALYSIS__":
                    prediction = message[1]
                    issues = list(message[2])
                    output = Path(str(message[3]))
                    self.current_analysis_issues = issues
                    self.current_analysis_output = output
                    self.set_localized(
                        self.prediction_var,
                        "≈ {total} total · {per_frame}/frame",
                        total=format_duration(prediction.total_seconds),
                        per_frame=format_duration(prediction.seconds_per_frame),
                    )
                    self.set_localized(
                        self.memory_prediction_var,
                        "Memory ≈ {memory:.1f} GB · {confidence} confidence · {source} · difficult frames: {frames}",
                        memory=prediction.memory_mb / 1024,
                        confidence=prediction.confidence,
                        source=prediction.source,
                        frames=", ".join(map(str, prediction.difficult_frames)),
                    )
                    if issues:
                        self.set_raw(self.autofix_var, "\n".join(f"[{issue.severity.upper()}] {issue.message}" for issue in issues[:7]))
                    else:
                        self.set_localized(self.autofix_var, "No common problems found. Ready to render.")
                    continue

                if isinstance(message, tuple) and len(message) == 4 and message[0] == "__SANDBOX__":
                    results = list(message[1])
                    recommendation = message[2]
                    error = str(message[3])
                    for item in self.sandbox_tree.get_children():
                        self.sandbox_tree.delete(item)
                    for result in results:
                        self.sandbox_tree.insert(
                            "",
                            "end",
                            values=(
                                result.variant.name,
                                result.variant.samples,
                                f"{result.variant.resolution_percent}%",
                                format_duration(result.duration_seconds),
                                f"{result.quality_score:.0f}%",
                                result.output_file or f"Failed ({result.return_code})",
                            ),
                        )
                    if error:
                        self.set_localized(self.sandbox_status_var, "Sandbox failed: {error}", error=error)
                    elif recommendation:
                        self.set_localized(
                            self.sandbox_status_var,
                            "Recommended: {variant} · {duration} · quality {quality}%",
                            variant=recommendation.variant.name,
                            duration=format_duration(recommendation.duration_seconds),
                            quality=f"{recommendation.quality_score:.0f}",
                        )
                    else:
                        self.set_localized(self.sandbox_status_var, "No sandbox variant completed successfully")
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__QUEUE_REFRESH__":
                    self.refresh_queue_tree()
                    continue

                if isinstance(message, tuple) and len(message) >= 3 and message[0] in {"__FRAME_METRIC__", "__NETWORK_FRAME__"}:
                    now = time.monotonic()
                    duration = float(message[3]) if len(message) > 3 else max(0.0, now - (self.last_frame_observed_at or now))
                    self.last_frame_observed_at = now
                    self.current_render_frame = int(message[1])
                    if duration > 0:
                        self.render_frame_count += 1
                        count = self.render_frame_count
                        self.render_average_seconds = ((self.render_average_seconds * (count - 1)) + duration) / count
                    self.latest_frame_path = Path(str(message[2]))
                    continue

                if isinstance(message, tuple) and len(message) >= 3 and message[0] == "__NETWORK_STARTED__":
                    start = int(message[1])
                    end = int(message[2])
                    skipped = int(message[3]) if len(message) > 3 else 0
                    self.current_render_frame = None
                    self.render_frame_count = 0
                    self.render_average_seconds = 0.0
                    self.last_frame_observed_at = time.monotonic()
                    self.set_localized(self.status_var, "Network render")
                    if skipped:
                        self.set_localized(self.status_detail_var, "Frames {start}-{end} · {skipped} existing skipped", start=start, end=end, skipped=skipped)
                    else:
                        self.set_localized(self.status_detail_var, "Distributing frames {start}-{end}", start=start, end=end)
                    self.set_localized(self.network_status_var, "Distributed render is running")
                    if self.network_controller and not self.network_controller.workers:
                        self.log("[NETWORK] No workers yet. Connect a device or choose Use this PC.")
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__NETWORK_ERROR__":
                    self.set_localized(self.status_var, "Network error")
                    self.set_raw(self.status_detail_var, str(message[1]))
                    self.set_localized(self.network_status_var, "Could not start: {error}", error=message[1])
                    active_job = self.render_queue.active
                    if active_job is not None and active_job.job_id == self.active_queue_job_id:
                        active_job.status = "failed"
                        active_job.error = str(message[1])
                        self.active_queue_job_id = None
                        self.save_render_queue()
                        self.refresh_queue_tree(active_job.job_id)
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__HISTORY_REFRESH__":
                    self.refresh_history_views()
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__REMOTE_ACTION__":
                    self.handle_remote_action(str(message[1]))
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__HARDWARE__":
                    cpu, gpus = stable_hardware_snapshot(
                        self.cpu_name,
                        self.gpu_names,
                        str(message[1]),
                        [str(item) for item in message[2]],
                    )
                    previous = set(self.gpu_names)
                    self.cpu_name = cpu
                    self.gpu_names = gpus
                    self.local_capabilities = DeviceCapabilities(
                        cpu=cpu,
                        gpus=gpus,
                        compute_backends=infer_compute_backends(gpus, platform.system()),
                        platform=f"{platform.system()} {platform.release()}",
                    )
                    active_group = self.group_registry.active
                    if active_group is not None:
                        active_group.register_device(
                            self.group_registry.identity.device_id,
                            platform.node() or "This PC",
                            identity_fingerprint=self.group_registry.identity.fingerprint,
                            capabilities=self.local_capabilities,
                        )
                        try:
                            self.group_registry.save(GROUPS_PATH)
                        except OSError:
                            pass
                    self.cpu_info_var.set(cpu)
                    self.gpu_info_var.set("; ".join(gpus))
                    new_devices = [gpu for gpu in gpus if gpu not in previous]
                    if new_devices:
                        self.log(f"[HOT-PLUG] New render device detected: {'; '.join(new_devices)}")
                        send_notification("Blender Render Watchdog", f"New device detected: {new_devices[0]}")
                    continue

                if isinstance(message, tuple) and len(message) == 4 and message[0] == "__QUEUE_ITEM__":
                    job_id = str(message[1])
                    status = str(message[2])
                    error = str(message[3])
                    self.refresh_queue_tree(job_id)
                    job = self.render_queue.get(job_id)
                    if status == "running" and job:
                        self.set_localized(self.status_var, "Queue")
                        self.set_localized(self.status_detail_var, "Rendering {project}", project=job.project_name)
                    elif status == "failed" and job:
                        self.log(f"[WATCHDOG] Queue failed: {job.project_name} — {error}")
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__QUEUE_DONE__":
                    completed = int(message[1])
                    failed = int(message[2])
                    self.queue_running = False
                    paused = any(job.status == "paused" for job in self.render_queue.jobs)
                    self.is_paused = paused
                    self.paused_queue = paused
                    self.refresh_queue_tree()
                    if paused:
                        self.set_localized(self.status_var, "Paused")
                        self.set_localized(self.status_detail_var, "Queue can continue from the next frame")
                        self.set_widget_text(self.start_queue_button, "Continue queue")
                        self.start_queue_button.configure(state="normal")
                        send_notification("Blender Render Watchdog", "Render queue paused after the current frame.")
                    elif self.stop_event and self.stop_event.is_set():
                        self.clear_unfinished_resume()
                        self.set_localized(self.status_var, "Stopped")
                        self.set_localized(self.status_detail_var, "Render queue stopped")
                        self.set_widget_text(self.start_queue_button, "Continue queue")
                        self.start_queue_button.configure(state="normal")
                    else:
                        if failed == 0:
                            self.clear_unfinished_resume()
                        self.set_localized(self.status_var, "Queue complete")
                        self.set_localized(
                            self.status_detail_var,
                            "{completed} complete · {failed} failed",
                            completed=completed,
                            failed=failed,
                        )
                        self.set_widget_text(self.start_queue_button, "Start queue")
                        self.start_queue_button.configure(state="normal")
                        send_notification(
                            "Blender Render Watchdog",
                            f"Render queue finished: {completed} complete, {failed} failed.",
                        )
                        if failed == 0 and self.shutdown_after_render_var.get():
                            self.log("[WATCHDOG] Queue complete. Windows will shut down in 60 seconds.")
                            schedule_system_shutdown(60)
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__FINISHED__":
                    code = int(message[1])
                    if code == 0:
                        self.clear_unfinished_resume()
                        self.is_paused = False
                        self.paused_queue = False
                        self.set_localized(self.status_var, "Complete")
                        self.set_localized(self.status_detail_var, "Render finished normally")
                        send_notification("Blender Render Watchdog", "Render finished successfully.")
                        if self.shutdown_after_render_var.get():
                            self.log("[WATCHDOG] Shutdown enabled. Windows will shut down in 60 seconds.")
                            send_notification("Blender Render Watchdog", "Render finished. Shutdown starts in 60 seconds.")
                            schedule_system_shutdown(60)
                    elif code == 131:
                        self.is_paused = True
                        self.paused_queue = False
                        self.set_localized(self.status_var, "Paused")
                        self.set_localized(self.status_detail_var, "Ready to resume from the next frame")
                        self.set_widget_text(self.start_button, "Resume Render")
                        self.start_button.configure(state="normal")
                        send_notification("Blender Render Watchdog", "Render paused after current frame.")
                    elif code == 130:
                        self.clear_unfinished_resume()
                        self.is_paused = False
                        self.paused_queue = False
                        self.set_localized(self.status_var, "Stopped")
                        self.set_localized(self.status_detail_var, "Render stopped by user")
                        send_notification("Blender Render Watchdog", "Render stopped.")
                    else:
                        self.is_paused = False
                        self.paused_queue = False
                        self.set_localized(self.status_var, "Error")
                        self.set_localized(self.status_detail_var, "Process exited with code {code}", code=code)
                        send_notification("Blender Render Watchdog", f"Render exited with code {code}.")
                    continue

                if isinstance(message, tuple) and len(message) == 4 and message[0] == "__UPDATE__":
                    status = str(message[1])
                    text = str(message[2])
                    manifest = message[3]
                    if status == "available" and isinstance(manifest, dict):
                        self.set_localized(
                            self.update_status_var,
                            "Update available: {version}",
                            version=str(manifest.get("version") or ""),
                        )
                    elif status == "current":
                        self.set_localized(self.update_status_var, "Already latest: {version}", version=APP_VERSION)
                    else:
                        self.set_raw(self.update_status_var, text)
                    if hasattr(self, "check_update_button"):
                        self.check_update_button.configure(state="normal")
                    if status == "available" and isinstance(manifest, dict):
                        self.latest_update_manifest = manifest
                        if hasattr(self, "install_update_button"):
                            self.install_update_button.configure(state="normal")
                        self.log(f"[WATCHDOG] {text}")
                        if self.auto_install_updates_var.get():
                            self.log("[WATCHDOG] Auto install update is enabled. Installing...")
                            self.root.after(500, lambda: self.install_latest_update(ask=False))
                    else:
                        self.latest_update_manifest = None
                        if hasattr(self, "install_update_button"):
                            self.install_update_button.configure(state="disabled")
                        self.log(f"[WATCHDOG] {text}")
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__PROGRESS__":
                    percent = float(message[1])
                    progress_details = str(message[2])
                    self.animate_progress(percent)
                    self.set_raw(self.progress_text_var, f"{percent:.0f}%  {progress_details}")
                    eta_match = re.search(r"\bETA\s+(.+)$", progress_details)
                    if eta_match:
                        self.set_localized(self.remaining_time_var, "Approx. remaining: {time}", time=eta_match.group(1))
                    elif percent >= 100:
                        self.set_localized(self.remaining_time_var, "Render complete")
                    continue

                self.log(str(message))

            self.root.after(150, self.drain_log_queue)

        def log(self, message: str) -> None:
            tag = None
            lower = message.lower()
            if message.startswith("[FRAME]"):
                tag = "frame"
            elif "error" in lower or "crash" in lower or "failed" in lower:
                tag = "error"
            elif message.startswith("[WATCHDOG]") or "watchdog" in lower:
                tag = "watchdog"

            if tag:
                self.log_text.insert("end", message + "\n", tag)
            else:
                self.log_text.insert("end", message + "\n")
            self.log_text.see("end")

        def on_close(self) -> None:
            if self.worker and self.worker.is_alive():
                should_close = messagebox.askyesno(
                    self.tr("Stop render"),
                    self.tr("Render is running. Stop Blender and close the window?"),
                )
                if not should_close:
                    return
                self.stop_watchdog()

            self.save_current_config()
            self.save_render_queue()
            self.save_render_history()
            self.stop_network_worker()
            self.stop_network_controller()
            if self.mobile_dashboard:
                self.mobile_dashboard.stop()
            self.root.destroy()

    root = tk.Tk()
    WatchdogApp(root)
    root.mainloop()
    return 0

def main() -> int:
    args = parse_args()

    if args.write_update_cmd:
        cmd_path = write_update_check_cmd()
        print(f"Update checker created: {cmd_path}", flush=True)
        return 0

    if args.check_update:
        return check_update_cli(args.update_source, args.install_update)

    if args.worker_code:
        blender = Path(args.blender) if args.blender else find_blender(ask_if_missing=False)
        if blender is None or not blender.exists():
            print("Blender executable is required in worker mode.", flush=True)
            return 2
        cpu, gpus = detect_hardware()
        worker_code = args.worker_code
        cli_ssh_tunnel: SshTunnel | None = None
        connection = PairingCode.decode(args.worker_code)
        if connection.transport == "ssh":
            cli_identity = None
            if connection.ssh_private_key:
                try:
                    cli_identity = store_invitation_private_key(connection.ssh_private_key, app_config_dir() / "ssh")
                except (OSError, ValueError) as error:
                    print(f"Could not save the SSH invitation key: {error}", flush=True)
                    return 2
            cli_ssh_tunnel = SshTunnel(connection.ssh_host, connection.ssh_port, connection.ssh_user, connection.port, cli_identity)
            try:
                local_port = cli_ssh_tunnel.start()
            except Exception as error:
                print(f"Could not start SSH tunnel: {error}", flush=True)
                return 2
            worker_code = PairingCode("127.0.0.1", local_port, connection.token, "lan").encode()
        worker = NetworkWorker(
            worker_code,
            blender,
            name=args.worker_name,
            hardware=f"{cpu}; {'; '.join(gpus)}",
            cache_folder=app_config_dir() / "network_worker",
            on_event=lambda message: print(message, flush=True),
        )
        try:
            worker.run()
            return 0
        except KeyboardInterrupt:
            worker.stop()
            return 130
        finally:
            worker.cleanup_cache()
            if cli_ssh_tunnel:
                cli_ssh_tunnel.stop()

    if not args.blend or not args.frames:
        return run_gui(args)

    blender = Path(args.blender) if args.blender else find_blender()
    if not blender or not blender.exists():
        print("Blender executable was not selected or found.")
        return 1

    blend = Path(args.blend) if args.blend else choose_file(
        "Choose .blend file",
        [("Blender files", "*.blend"), ("All files", "*.*")],
    )
    if not blend or not blend.exists():
        print("Blend file was not selected or found.")
        return 1

    frames_folder = Path(args.frames) if args.frames else choose_folder(
        "Choose folder with rendered frames"
    )
    if not frames_folder:
        print("Frames folder was not selected.")
        return 1

    return run_watchdog(
        blender=blender,
        blend=blend,
        frames_folder=frames_folder,
        sleep_seconds=args.sleep,
        padding=args.padding,
        start=args.start,
        end=args.end,
        extra_args=args.extra,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.", flush=True)
        raise SystemExit(130)
