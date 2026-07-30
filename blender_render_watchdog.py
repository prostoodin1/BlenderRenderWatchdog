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
from pathlib import Path

from auto_fix import AutoFixIssue, apply_safe_fixes, inspect_render_setup
from glass_ui import GlassCard, GlassTabView, GlassWidgetFactory
from mobile_dashboard import MobileDashboardServer
from network_render import MAX_WORKERS, NetworkWorker, RenderCoordinator, prepare_network_project
from render_analytics import RenderHistory, RenderSession, estimate_render
from render_queue import RenderJob, RenderQueue
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
COMPUTE_BACKENDS = ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL")
APP_VERSION = "2.1.0"
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
        )
    except Exception:
        pass

    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass



def detect_hardware() -> tuple[str, list[str]]:
    cpu = platform.processor() or platform.machine() or "Unknown CPU"
    gpus: list[str] = []

    if os.name == "nt":
        try:
            command = [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                "Get-CimInstance Win32_VideoController | Select-Object -ExpandProperty Name",
            ]
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
            )
            if completed.returncode == 0:
                gpus = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
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
BACKENDS = {list(COMPUTE_BACKENDS)!r}
OPTIMIZE = {optimize_options!r}


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
    return {"version": version, "exe_url": exe_url, "notes": notes}


def fetch_github_release_manifest(repository: str) -> dict[str, object]:
    import urllib.request

    api_url = f"https://api.github.com/repos/{repository}/releases/latest"
    request = urllib.request.Request(api_url, headers={"User-Agent": "BlenderRenderWatchdog"})
    with urllib.request.urlopen(request, timeout=20) as response:
        release = json.loads(response.read().decode("utf-8"))

    if not isinstance(release, dict):
        raise ValueError("GitHub release response must be a JSON object.")

    exe_url = ""
    for asset in release.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name = str(asset.get("name") or "").lower()
        if name == "blenderrenderwatchdog.exe" or name.endswith(".exe"):
            exe_url = str(asset.get("browser_download_url") or "")
            break

    return normalize_update_manifest(
        {
            "version": release.get("tag_name") or release.get("name") or "",
            "exe_url": exe_url,
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


def install_update_from_manifest(manifest: dict[str, object]) -> None:
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Auto-install updates is available only in the exe build.")

    exe_url = str(manifest.get("exe_url") or DEFAULT_RELEASE_EXE_URL).strip()
    if not exe_url:
        raise ValueError("Update manifest does not contain exe_url.")

    target = app_target_path()
    temp_exe = Path(tempfile.gettempdir()) / "BlenderRenderWatchdog_update.exe"
    updater_script = Path(tempfile.gettempdir()) / "BlenderRenderWatchdog_apply_update.ps1"
    current_pid = os.getpid()

    script = f'''
$ErrorActionPreference = "Stop"
$Url = {exe_url!r}
$TempExe = {str(temp_exe)!r}
$Target = {str(target)!r}
$PidToWait = {current_pid}
Write-Host "Downloading update..."
Invoke-WebRequest -Uri $Url -OutFile $TempExe -UseBasicParsing
Write-Host "Waiting for app to close..."
try {{ Wait-Process -Id $PidToWait -Timeout 60 }} catch {{ }}
Start-Sleep -Seconds 2
Copy-Item -LiteralPath $TempExe -Destination $Target -Force
Start-Process -FilePath $Target
'''
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
    parser.add_argument("--worker-code", default=None, help="Run as a network worker using a BRW2 connection code.")
    parser.add_argument("--worker-name", default=None, help="Display name used in network worker mode.")
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
    from tkinter import filedialog, messagebox, scrolledtext, ttk

    class WatchdogApp:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.root.title(f"Blender Render Watchdog {APP_VERSION}")
            self.root.geometry("1280x860")
            self.root.minsize(1080, 720)

            self.colors = {
                "bg": "#070a12",
                "panel": "#111a2a",
                "panel_alt": "#18243a",
                "field": "#0b1220",
                "field_border": "#2d3c58",
                "text": "#f8fbff",
                "muted": "#9aa9c2",
                "soft": "#dce6f7",
                "accent": "#8b7cff",
                "accent_hot": "#aa9cff",
                "accent_blue": "#6ee7ff",
                "accent_dark": "#6553e8",
                "accent_green": "#55f7b0",
                "danger": "#ff6b8b",
                "warning": "#ffd166",
                "line": "#2d3c58",
                "line_hot": "#7797c7",
                "shadow": "#03050a",
            }

            self.config = load_config()
            self.log_queue: queue.Queue[str | tuple[str, float, str]] = queue.Queue()
            self.stop_event: threading.Event | None = None
            self.pause_event: threading.Event | None = None
            self.worker: threading.Thread | None = None
            self.is_paused = False
            self.queue_running = False
            self.paused_queue = False
            self.active_queue_job_id: str | None = None
            self.render_queue = RenderQueue.load(QUEUE_PATH)
            self.render_history = RenderHistory.load(HISTORY_PATH)
            self.network_controller: RenderCoordinator | None = None
            self.network_worker: NetworkWorker | None = None
            self.mobile_dashboard: MobileDashboardServer | None = None
            self.network_session: RenderSession | None = None
            self.network_history_saved = False
            self.latest_frame_path: Path | None = None
            self.current_analysis_issues: list[AutoFixIssue] = []
            self.current_analysis_output: Path | None = None
            self.hardware_poll_running = False
            self.mobile_state_cache: dict[str, object] = {}
            self.glass_cards: list[GlassCard] = []
            self.card_reveal_index = 0
            self.progress_animation_id: str | None = None
            self.progress_animation_target = 0.0

            saved_blender = args.blender or self.config.get("blender") or ""
            if not saved_blender:
                found_blender = find_blender(ask_if_missing=False)
                saved_blender = str(found_blender) if found_blender else ""

            cpu_name, gpu_names = detect_hardware()
            self.cpu_name = cpu_name
            self.gpu_names = gpu_names
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
            self.status_var = tk.StringVar(value="Ready")
            self.status_detail_var = tk.StringVar(value="Waiting for render setup")
            self.progress_var = tk.DoubleVar(value=0.0)
            self.progress_text_var = tk.StringVar(value="0%")
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
            self.update_manifest_url_var = tk.StringVar(value=normalize_update_source(self.config.get("update_manifest_url")))
            self.check_updates_on_start_var = tk.BooleanVar(value=(self.config.get("check_updates_on_start", "1") == "1"))
            self.auto_install_updates_var = tk.BooleanVar(value=(self.config.get("auto_install_updates", "0") == "1"))
            self.shutdown_after_render_var = tk.BooleanVar(value=(self.config.get("shutdown_after_render", "0") == "1"))
            self.mobile_enabled_var = tk.BooleanVar(value=(self.config.get("mobile_enabled", "0") == "1"))
            self.mobile_url_var = tk.StringVar(value="Mobile dashboard is stopped")
            self.prediction_var = tk.StringVar(value="Select a project and run Analyze")
            self.memory_prediction_var = tk.StringVar(value="Memory: —")
            self.autofix_var = tk.StringVar(value="Preflight has not been run")
            self.network_code_var = tk.StringVar(value="")
            self.network_join_code_var = tk.StringVar(value="")
            self.network_status_var = tk.StringVar(value="Controller is stopped")
            self.worker_name_var = tk.StringVar(value=platform.node() or "Render worker")
            self.worker_range_start_var = tk.StringVar(value="")
            self.worker_range_end_var = tk.StringVar(value="")
            self.sandbox_frame_var = tk.StringVar(value="1")
            self.sandbox_parallel_var = tk.BooleanVar(value=False)
            self.sandbox_status_var = tk.StringVar(value="Ready to compare Draft, Balanced and Quality")
            self.config_save_after_id: str | None = None
            self.update_status_var = tk.StringVar(value=f"Current version: {APP_VERSION}")
            self.latest_update_manifest: dict[str, object] | None = None

            self.build_style(ttk)
            self.build_layout(tk, ttk, scrolledtext)
            self.bind_config_autosave()
            self.update_manual_controls()
            self.root.protocol("WM_DELETE_WINDOW", self.on_close)
            self.root.after(150, self.drain_log_queue)
            self.root.after(1000, self.refresh_network_state)
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
            style.map("TButton", background=[("active", "#1b2942"), ("disabled", "#111827")], foreground=[("disabled", "#65738a")])
            style.configure("Primary.TButton", background=c["accent"], foreground="#031014", padding=(24, 13), font=("Segoe UI", 11, "bold"))
            style.map("Primary.TButton", background=[("active", "#8cf3ff"), ("disabled", "#174a55")], foreground=[("disabled", "#8db5bd")])
            style.configure("Danger.TButton", background="#3b1724", foreground="#ffd9e2", padding=(17, 12), font=("Segoe UI", 10, "bold"))
            style.map("Danger.TButton", background=[("active", "#5a2234"), ("disabled", "#151923")], foreground=[("disabled", "#65738a")])
            style.configure("Modern.TCheckbutton", background=c["panel_alt"], foreground=c["text"], font=("Segoe UI", 10, "bold"), padding=7)
            style.map("Modern.TCheckbutton", background=[("active", c["panel_alt"])], foreground=[("active", c["accent"]), ("selected", c["text"])])
            style.configure("Modern.Horizontal.TProgressbar", troughcolor=c["field"], background=c["accent"], bordercolor=c["panel"], lightcolor=c["accent"], darkcolor=c["accent"], thickness=16)
            style.configure("Modern.TNotebook", background=c["bg"], borderwidth=0, tabmargins=(0, 0, 0, 0))
            style.configure("Modern.TNotebook.Tab", background=c["panel"], foreground=c["muted"], padding=(22, 12), font=("Segoe UI", 10, "bold"))
            style.map("Modern.TNotebook.Tab", background=[("selected", c["panel_alt"]), ("active", "#1b2942")], foreground=[("selected", c["accent"]), ("active", c["text"])])
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
            c = self.colors
            ttk_module = GlassWidgetFactory(ttk_module, c)
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
            self.status_var.trace_add("write", lambda *_args: self.status_card.pulse())

            self.notebook = GlassTabView(outer, palette=c)
            self.notebook.grid(row=1, column=0, sticky="nsew")

            render_tab = ttk_module.Frame(self.notebook.page_host, style="App.TFrame", padding=(0, 8, 0, 0))
            render_tab.columnconfigure(0, weight=1)
            render_tab.rowconfigure(1, weight=1)
            self.notebook.add(render_tab, text="  Render  ")

            queue_tab = ttk_module.Frame(self.notebook.page_host, style="App.TFrame", padding=(0, 8, 0, 0))
            queue_tab.columnconfigure(0, weight=1)
            queue_tab.rowconfigure(0, weight=1)
            self.notebook.add(queue_tab, text="  Queue  ")

            network_tab = ttk_module.Frame(self.notebook.page_host, style="App.TFrame", padding=(0, 8, 0, 0))
            network_tab.columnconfigure(0, weight=1)
            network_tab.rowconfigure(0, weight=1)
            self.notebook.add(network_tab, text="  Network  ")

            insights_tab = ttk_module.Frame(self.notebook.page_host, style="App.TFrame", padding=(0, 8, 0, 0))
            insights_tab.columnconfigure(0, weight=1)
            insights_tab.rowconfigure(0, weight=1)
            self.notebook.add(insights_tab, text="  Insights  ")

            sandbox_tab = ttk_module.Frame(self.notebook.page_host, style="App.TFrame", padding=(0, 8, 0, 0))
            sandbox_tab.columnconfigure(0, weight=1)
            sandbox_tab.rowconfigure(0, weight=1)
            self.notebook.add(sandbox_tab, text="  Sandbox  ")

            advanced_tab = ttk_module.Frame(self.notebook.page_host, style="App.TFrame", padding=(0, 8, 0, 0))
            advanced_tab.columnconfigure(0, weight=1)
            advanced_tab.rowconfigure(0, weight=1)
            self.notebook.add(advanced_tab, text="  Advanced  ")

            settings_tab = ttk_module.Frame(self.notebook.page_host, style="App.TFrame", padding=(0, 8, 0, 0))
            settings_tab.columnconfigure(0, weight=1)
            settings_tab.rowconfigure(0, weight=1)
            self.notebook.add(settings_tab, text="  Settings  ")

            logs_tab = ttk_module.Frame(self.notebook.page_host, style="App.TFrame", padding=(0, 8, 0, 0))
            logs_tab.columnconfigure(0, weight=1)
            logs_tab.rowconfigure(0, weight=1)
            self.notebook.add(logs_tab, text="  Logs  ")

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
                self.queue_tree.heading(column, text=headings[column])
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
            self.queue_summary_var = tk.StringVar(value="Queue is empty")
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
            parent.rowconfigure(1, weight=1)

            controller_card = self.make_card(parent, ttk_module, row=0, column=0, sticky="nsew", padx=(0, 12), pady=(0, 12))
            controller_card.columnconfigure(0, weight=1)
            ttk_module.Label(controller_card, text="Controller", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(controller_card, text="Create a LAN code and distribute individual frames to up to five PCs.", style="CardHint.TLabel", wraplength=350).grid(row=1, column=0, sticky="w", pady=(3, 12))
            ttk_module.Entry(controller_card, textvariable=self.network_code_var, state="readonly").grid(row=2, column=0, sticky="ew")
            controller_actions = ttk_module.Frame(controller_card, style="Surface.TFrame")
            controller_actions.grid(row=3, column=0, sticky="ew", pady=(12, 0))
            ttk_module.Button(controller_actions, text="Start controller", style="Primary.TButton", command=self.start_network_controller).grid(row=0, column=0)
            ttk_module.Button(controller_actions, text="Copy code", command=self.copy_network_code).grid(row=0, column=1, padx=(8, 0))
            ttk_module.Button(controller_actions, text="Stop", command=self.stop_network_controller).grid(row=0, column=2, padx=(8, 0))
            ttk_module.Button(controller_card, text="Start distributed render", command=self.start_network_render).grid(row=4, column=0, sticky="ew", pady=(10, 0))
            ttk_module.Label(controller_card, textvariable=self.network_status_var, style="CardHint.TLabel", wraplength=350).grid(row=5, column=0, sticky="w", pady=(10, 0))

            worker_card = self.make_card(parent, ttk_module, row=1, column=0, sticky="nsew", padx=(0, 12))
            worker_card.columnconfigure(0, weight=1)
            ttk_module.Label(worker_card, text="Worker mode", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(worker_card, text="Enter the code on another computer. The project is downloaded automatically.", style="CardHint.TLabel", wraplength=350).grid(row=1, column=0, sticky="w", pady=(3, 12))
            ttk_module.Label(worker_card, text="This PC name", style="Field.TLabel").grid(row=2, column=0, sticky="w")
            ttk_module.Entry(worker_card, textvariable=self.worker_name_var).grid(row=3, column=0, sticky="ew", pady=(4, 10))
            ttk_module.Label(worker_card, text="Connection code", style="Field.TLabel").grid(row=4, column=0, sticky="w")
            ttk_module.Entry(worker_card, textvariable=self.network_join_code_var).grid(row=5, column=0, sticky="ew", pady=(4, 10))
            worker_actions = ttk_module.Frame(worker_card, style="Surface.TFrame")
            worker_actions.grid(row=6, column=0, sticky="ew")
            ttk_module.Button(worker_actions, text="Connect and render", style="Primary.TButton", command=self.start_network_worker).grid(row=0, column=0)
            ttk_module.Button(worker_actions, text="Use this PC", command=self.start_local_network_worker).grid(row=0, column=1, padx=(8, 0))
            ttk_module.Button(worker_actions, text="Disconnect", command=self.stop_network_worker).grid(row=0, column=2, padx=(8, 0))

            nodes_card = self.make_card(parent, ttk_module, row=0, column=1, rowspan=2, sticky="nsew", padx=(12, 0))
            nodes_card.columnconfigure(0, weight=1)
            nodes_card.rowconfigure(1, weight=1)
            ttk_module.Label(nodes_card, text="Connected devices", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
            columns = ("name", "hardware", "current", "done", "average", "range")
            self.network_tree = ttk_module.Treeview(nodes_card, columns=columns, show="headings", style="Queue.Treeview", selectmode="browse")
            headings = {"name": "Device", "hardware": "Hardware", "current": "Frame", "done": "Done", "average": "Avg", "range": "Allocation"}
            widths = {"name": 130, "hardware": 240, "current": 60, "done": 60, "average": 75, "range": 100}
            for column in columns:
                self.network_tree.heading(column, text=headings[column])
                self.network_tree.column(column, width=widths[column], anchor="center" if column in {"current", "done", "average", "range"} else "w")
            self.network_tree.grid(row=1, column=0, sticky="nsew")
            allocation = ttk_module.Frame(nodes_card, style="Surface.TFrame")
            allocation.grid(row=2, column=0, sticky="ew", pady=(12, 0))
            ttk_module.Label(allocation, text="Manual allocation", style="Field.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Entry(allocation, textvariable=self.worker_range_start_var, width=9).grid(row=0, column=1, padx=(10, 4))
            ttk_module.Label(allocation, text="to", style="CardHint.TLabel").grid(row=0, column=2)
            ttk_module.Entry(allocation, textvariable=self.worker_range_end_var, width=9).grid(row=0, column=3, padx=(4, 10))
            ttk_module.Button(allocation, text="Apply", command=self.apply_network_allocation).grid(row=0, column=4)
            ttk_module.Label(allocation, text="Leave empty for automatic balancing", style="CardHint.TLabel").grid(row=0, column=5, sticky="w", padx=(10, 0))

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
                self.history_tree.heading(column, text=title)
                self.history_tree.column(column, width=width, anchor="w")
            self.history_tree.grid(row=1, column=0, sticky="nsew")

            hard_card = self.make_card(parent, ttk_module, row=1, column=1, sticky="nsew", padx=(12, 0))
            hard_card.columnconfigure(0, weight=1)
            hard_card.rowconfigure(1, weight=1)
            ttk_module.Label(hard_card, text="Hardest frames", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
            self.hardest_tree = ttk_module.Treeview(hard_card, columns=("frame", "duration"), show="headings", style="Queue.Treeview")
            self.hardest_tree.heading("frame", text="Frame")
            self.hardest_tree.heading("duration", text="Render time")
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
                self.sandbox_tree.heading(column, text=headings[column])
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
            ttk_module.Label(action_row, textvariable=self.update_status_var, style="CardHint.TLabel").grid(row=0, column=3, sticky="e")

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
            ttk_module.Checkbutton(
                power_card,
                text="Shutdown PC after successful render",
                variable=self.shutdown_after_render_var,
                command=self.save_current_config,
                style="Modern.TCheckbutton",
            ).grid(row=2, column=0, sticky="w")
            ttk_module.Label(
                power_card,
                text="Tip: if shutdown starts accidentally, run `shutdown /a` in Windows to cancel it.",
                style="CardHint.TLabel",
                wraplength=300,
                justify="left",
            ).grid(row=3, column=0, sticky="ew", pady=(14, 0))

            mobile_card = self.make_card(parent, ttk_module, row=1, column=0, sticky="ew", padx=(0, 12), pady=(14, 0))
            mobile_card.columnconfigure(1, weight=1)
            ttk_module.Label(mobile_card, text="Mobile dashboard", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")
            ttk_module.Label(mobile_card, text="Open the private LAN link on a phone to view progress, previews and control the render.", style="CardHint.TLabel").grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 10))
            ttk_module.Entry(mobile_card, textvariable=self.mobile_url_var, state="readonly").grid(row=2, column=0, columnspan=3, sticky="ew")
            ttk_module.Checkbutton(mobile_card, text="Start with the app", variable=self.mobile_enabled_var, command=self.save_current_config, style="Modern.TCheckbutton").grid(row=3, column=0, sticky="w", pady=(10, 0))
            ttk_module.Button(mobile_card, text="Start", command=self.start_mobile_dashboard).grid(row=3, column=1, sticky="e", pady=(10, 0))
            ttk_module.Button(mobile_card, text="Copy link", command=self.copy_mobile_url).grid(row=3, column=2, sticky="e", padx=(8, 0), pady=(10, 0))

            privacy_card = self.make_card(parent, ttk_module, row=1, column=1, sticky="nsew", padx=(12, 0), pady=(14, 0))
            ttk_module.Label(privacy_card, text="LAN security", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
            ttk_module.Label(
                privacy_card,
                text="Network and mobile links use random access tokens. Share them only with devices on networks you trust.",
                style="CardHint.TLabel",
                wraplength=300,
                justify="left",
            ).grid(row=1, column=0, sticky="ew", pady=(8, 0))
        def add_optimization_check(self, parent, ttk_module, row: int, variable: tk.BooleanVar, title: str, hint: str) -> None:
            ttk_module.Checkbutton(parent, text=title, variable=variable, command=self.save_current_config, style="Modern.TCheckbutton").grid(row=row, column=0, sticky="w", pady=6)
            ttk_module.Label(parent, text=hint, style="CardHint.TLabel").grid(row=row, column=1, columnspan=2, sticky="w", padx=(12, 0), pady=6)
        def make_card(self, parent, ttk_module, row: int, column: int, sticky: str = "nsew", padx=0, pady=0, rowspan: int = 1):
            card = GlassCard(parent, palette=self.colors, padding=18, radius=24, backdrop=self.colors["bg"])
            card.grid(row=row, column=column, rowspan=rowspan, sticky=sticky, padx=padx, pady=pady)
            self.glass_cards.append(card)
            card.reveal(self.card_reveal_index * 38)
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
            def reveal_visible_cards() -> None:
                delay = 0
                for card in self.glass_cards:
                    if card.winfo_ismapped():
                        card.reveal(delay)
                        delay += 34

            self.root.after(20, reveal_visible_cards)

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
                self.queue_tree.insert(
                    "",
                    "end",
                    iid=job.job_id,
                    values=(
                        index,
                        job.project_name,
                        job.range_label,
                        "Video" if job.compose_video else "Frames",
                        format_duration(job.estimated_seconds) if job.estimated_seconds is not None else "—",
                        job.output_label,
                        status_labels.get(job.status, job.status.title()),
                    ),
                )
            pending_count = len(self.render_queue.pending())
            total_count = len(self.render_queue.jobs)
            if total_count:
                self.queue_summary_var.set(f"{total_count} projects · {pending_count} waiting")
            else:
                self.queue_summary_var.set("Queue is empty")
            if selected_job_id and self.queue_tree.exists(selected_job_id):
                self.queue_tree.selection_set(selected_job_id)
                self.queue_tree.focus(selected_job_id)

        def queue_job_from_current(self, blend_path: Path | None = None) -> RenderJob | None:
            blend_text = str(blend_path or self.blend_var.get().strip().strip('"'))
            if not blend_text or not Path(blend_text).exists():
                messagebox.showerror("Blend file not found", "Choose an existing .blend file first.")
                return None

            frame_range = self.frame_range_values()
            if frame_range is None:
                return None
            start_frame, end_frame = frame_range
            output_path = self.frames_var.get().strip().strip('"')
            if not self.use_scene_output_var.get() and not output_path:
                messagebox.showerror("Frames folder missing", "Choose an output folder or enable the .blend output path.")
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
            )
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
            self.render_queue.add(job)
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
                self.render_queue.add(job)
                last_job = job
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
            self.queue_summary_var.set("Analyzing queued projects…")
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
                self.update_manifest_url_var,
                self.check_updates_on_start_var,
                self.auto_install_updates_var,
                self.shutdown_after_render_var,
                self.mobile_enabled_var,
            ]
            for variable in variables:
                variable.trace_add("write", lambda *_: self.schedule_config_save())

        def schedule_config_save(self) -> None:
            if self.config_save_after_id:
                self.root.after_cancel(self.config_save_after_id)
            self.config_save_after_id = self.root.after(350, self.save_current_config)


        def save_current_config(self) -> None:
            self.config_save_after_id = None
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
                    "update_manifest_url": self.update_manifest_url_var.get().strip(),
                    "check_updates_on_start": "1" if self.check_updates_on_start_var.get() else "0",
                    "auto_install_updates": "1" if self.auto_install_updates_var.get() else "0",
                    "shutdown_after_render": "1" if self.shutdown_after_render_var.get() else "0",
                    "mobile_enabled": "1" if self.mobile_enabled_var.get() else "0",
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
                messagebox.showerror("Publisher missing", "Publish To GitHub.cmd was not found near the app.")
                return
            try:
                subprocess.Popen(["cmd.exe", "/c", "start", "", str(script_path)], cwd=str(script_path.parent))
                self.log(f"[WATCHDOG] GitHub publisher started: {script_path}")
            except Exception as error:
                messagebox.showerror("Publisher failed", str(error))


        def check_for_updates(self) -> None:
            raw_update_source = self.update_manifest_url_var.get()
            update_source = normalize_update_source(raw_update_source)
            if raw_update_source.strip() != update_source:
                self.update_manifest_url_var.set(update_source)
            self.save_current_config()
            self.update_status_var.set("Checking for updates...")
            self.check_update_button.configure(state="disabled")
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
                    messagebox.showerror("No update", "Check updates first.")
                return
            if ask:
                should_install = messagebox.askyesno(
                    "Install update",
                    "The app will close, download the new version and restart. Install update?",
                )
                if not should_install:
                    return
            try:
                self.save_current_config()
                install_update_from_manifest(self.latest_update_manifest)
                self.root.destroy()
            except Exception as error:
                if ask:
                    messagebox.showerror("Update failed", str(error))
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
                messagebox.showerror("Invalid frame range", f"{field_name} должен быть числом.")
                return None
            if frame < 0:
                messagebox.showerror("Invalid frame range", f"{field_name} не может быть меньше 0.")
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
                messagebox.showerror("Invalid frame range", "Start frame не может быть больше End frame.")
                return None
            return start_frame, end_frame

        def output_folder_values(self, blender: Path, blend: Path, manual_frames: Path) -> tuple[Path, bool] | None:
            if not self.use_scene_output_var.get():
                return manual_frames, True

            settings = query_scene_settings(blender, blend, log=lambda message: self.log(message))
            if not settings:
                messagebox.showerror("Scene output not found", "Не удалось прочитать путь сохранения из .blend файла.")
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
            }

        def validate_paths(self) -> tuple[Path, Path, Path] | None:
            blender_text = self.blender_var.get().strip().strip('"')
            blend_text = self.blend_var.get().strip().strip('"')
            frames_text = self.frames_var.get().strip().strip('"')

            if not frames_text and not self.use_scene_output_var.get():
                messagebox.showerror("Frames folder missing", "Выбери папку для кадров или включи Use .blend output path.")
                return None
            if not self.use_cpu_var.get() and not self.use_gpu_var.get():
                messagebox.showerror("Render device missing", "Выбери хотя бы CPU или GPU для рендера.")
                return None

            blender = Path(blender_text)
            blend = Path(blend_text)
            frames = Path(frames_text) if frames_text else blend.parent

            if not blender.exists():
                messagebox.showerror("Blender not found", "Не найден blender.exe. Выбери его вручную.")
                return None
            if not blend.exists():
                messagebox.showerror("Blend file not found", "Не найден .blend файл.")
                return None
            return blender, blend, frames

        def analyze_current_project(self) -> None:
            blender = Path(self.blender_var.get().strip().strip('"'))
            blend = Path(self.blend_var.get().strip().strip('"'))
            manual_output = Path(self.frames_var.get().strip().strip('"') or blend.parent)
            if not blend.exists() or not blender.exists():
                messagebox.showerror("Project missing", "Choose an existing Blender executable and .blend project first.")
                return
            frame_range = self.frame_range_values()
            if frame_range is None:
                return
            self.prediction_var.set("Analyzing scene…")
            self.autofix_var.set("Running preflight…")
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
            self.autofix_var.set(f"Applied {fixed} safe fix(es). Re-run preflight to verify.")
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
                messagebox.showerror("Invalid frame", "Sandbox frame must be a number.")
                return
            samples = self.parse_positive_int(self.samples_var.get(), 256, 1, 100000)
            resolution = self.parse_positive_int(self.resolution_percent_var.get(), 100, 1, 100)
            variants = [
                SandboxVariant("Draft", min(32, samples), min(50, resolution)),
                SandboxVariant("Balanced", min(128, samples), min(75, resolution)),
                SandboxVariant("Quality", samples, resolution),
            ]
            self.sandbox_status_var.set("Running sandbox variants…")
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

        def start_network_controller(self) -> None:
            if self.network_controller is not None:
                self.network_code_var.set(self.network_controller.pairing_code)
                return
            try:
                self.network_controller = RenderCoordinator(
                    on_event=lambda message: self.log_queue.put(message),
                    on_frame=self.on_network_frame,
                )
                code = self.network_controller.start()
                self.network_code_var.set(code)
                self.network_join_code_var.set(code)
                self.network_status_var.set(f"Controller ready · 0/{MAX_WORKERS} devices")
            except Exception as error:
                self.network_controller = None
                messagebox.showerror("Network controller", str(error))

        def stop_network_controller(self) -> None:
            if self.network_controller:
                if self.network_controller.plan:
                    self.network_controller.plan.stopped = True
                self.network_controller.stop()
            self.network_controller = None
            self.network_code_var.set("")
            self.network_status_var.set("Controller is stopped")

        def copy_network_code(self) -> None:
            code = self.network_code_var.get().strip()
            if code:
                self.root.clipboard_clear()
                self.root.clipboard_append(code)
                self.network_status_var.set("Connection code copied")

        def start_network_render(self) -> None:
            paths = self.validate_paths()
            if paths is None:
                return
            if self.network_controller is None:
                self.start_network_controller()
            if self.network_controller is None:
                return
            blender, blend, manual_output = paths
            frame_range = self.frame_range_values()
            if frame_range is None:
                return
            settings = query_scene_settings(blender, blend, log=lambda message: self.log(message)) or {}
            start = frame_range[0] if frame_range[0] is not None else int(settings.get("frame_start") or 1)
            end = frame_range[1] if frame_range[1] is not None else int(settings.get("frame_end") or start)
            output = (
                output_folder_from_scene_path(str(settings.get("output_path") or ""), blend)
                if self.use_scene_output_var.get()
                else manual_output
            )
            self.status_var.set("Network setup")
            self.status_detail_var.set("Packing project assets for workers")
            self.network_status_var.set("Preparing a packed project copy…")
            controller = self.network_controller
            threading.Thread(
                target=self.prepare_network_plan_worker,
                args=(controller, blender, blend, output, start, end, settings),
                daemon=True,
            ).start()

        def prepare_network_plan_worker(
            self,
            controller: RenderCoordinator,
            blender: Path,
            blend: Path,
            output: Path,
            start: int,
            end: int,
            settings: dict[str, object],
        ) -> None:
            try:
                stamp = int(blend.stat().st_mtime)
                packed = app_config_dir() / "network_projects" / f"{blend.stem}_{stamp}.blend"
                prepare_network_project(blender, blend, packed, log=lambda message: self.log_queue.put(message))
                if self.network_controller is not controller:
                    return
                controller.start_plan(packed, output, start, end)
                self.network_session = RenderSession(str(blend), str(output), start, end, mode="network", settings=settings)
                self.network_history_saved = False
                self.log_queue.put(("__NETWORK_STARTED__", start, end))
            except Exception as error:
                self.log_queue.put(("__NETWORK_ERROR__", str(error), ""))

        def on_network_frame(self, frame: int, path: Path, _duration: float) -> None:
            self.latest_frame_path = path
            if self.network_session:
                self.network_session.mark_frame(frame)
            self.log_queue.put(("__NETWORK_FRAME__", frame, str(path)))

        def start_network_worker(self) -> None:
            if self.network_worker is not None:
                return
            code = self.network_join_code_var.get().strip()
            blender = Path(self.blender_var.get().strip().strip('"'))
            if not code or not blender.exists():
                messagebox.showerror("Worker setup", "Enter a connection code and choose blender.exe.")
                return
            if not messagebox.askyesno("Join render network", "This computer will download the project and render assigned frames. Ready to connect?"):
                return
            try:
                self.network_worker = NetworkWorker(
                    code,
                    blender,
                    name=self.worker_name_var.get().strip() or platform.node(),
                    hardware=f"{self.cpu_name}; {'; '.join(self.gpu_names)}",
                    cache_folder=app_config_dir() / "network_worker",
                    on_event=lambda message: self.log_queue.put(message),
                )
                threading.Thread(target=self.network_worker.run, daemon=True).start()
            except Exception as error:
                self.network_worker = None
                messagebox.showerror("Worker connection", str(error))

        def start_local_network_worker(self) -> None:
            if self.network_controller is None:
                self.start_network_controller()
            if self.network_controller:
                self.network_join_code_var.set(self.network_controller.pairing_code)
                self.start_network_worker()

        def stop_network_worker(self) -> None:
            if self.network_worker:
                self.network_worker.stop()
            self.network_worker = None

        def apply_network_allocation(self) -> None:
            if not self.network_controller:
                return
            selection = self.network_tree.selection()
            if not selection:
                return
            try:
                start = int(self.worker_range_start_var.get()) if self.worker_range_start_var.get().strip() else None
                end = int(self.worker_range_end_var.get()) if self.worker_range_end_var.get().strip() else None
                self.network_controller.set_worker_range(str(selection[0]), start, end)
            except ValueError as error:
                messagebox.showerror("Allocation", str(error))

        def refresh_network_state(self) -> None:
            controller = self.network_controller
            if hasattr(self, "network_tree"):
                for item in self.network_tree.get_children():
                    self.network_tree.delete(item)
                if controller:
                    for worker in controller.workers.values():
                        allocation = "Auto" if worker.frame_start is None and worker.frame_end is None else f"{worker.frame_start or '…'}-{worker.frame_end or '…'}"
                        self.network_tree.insert(
                            "",
                            "end",
                            iid=worker.worker_id,
                            values=(worker.name, worker.hardware, worker.current_frame or "—", worker.completed_frames, format_duration(worker.average_seconds), allocation),
                        )
                    online = sum(time.time() - worker.last_seen < 30 for worker in controller.workers.values())
                    self.network_status_var.set(f"Controller active · {online}/{MAX_WORKERS} devices")
                    if controller.plan:
                        summary = controller.plan.summary()
                        self.animate_progress(float(summary["progress"]))
                        self.progress_text_var.set(f"{summary['completed']} / {summary['total']} network frames")
                        if summary["finished"] and not self.network_history_saved and self.network_session:
                            status = "completed" if int(summary["failed"]) == 0 else "failed"
                            self.render_history.add(self.network_session.finish(status))
                            self.save_render_history()
                            self.refresh_history_views()
                            self.network_history_saved = True
                            self.status_var.set("Network complete")
                            self.status_detail_var.set(f"{summary['completed']} complete · {summary['failed']} failed")
                            send_notification("Blender Render Watchdog", "Distributed render finished.")
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
                self.mobile_url_var.set(self.mobile_dashboard.public_url)
                return
            try:
                self.mobile_dashboard = MobileDashboardServer(
                    state_provider=self.mobile_state_provider,
                    action_handler=self.mobile_action_handler,
                    preview_provider=lambda: self.latest_frame_path,
                )
                self.mobile_url_var.set(self.mobile_dashboard.start())
                self.log(f"[MOBILE] Dashboard: {self.mobile_url_var.get()}")
            except Exception as error:
                self.mobile_dashboard = None
                self.mobile_url_var.set(f"Could not start: {error}")

        def copy_mobile_url(self) -> None:
            value = self.mobile_url_var.get()
            if value.startswith("http"):
                self.root.clipboard_clear()
                self.root.clipboard_append(value)

        def refresh_mobile_state_cache(self) -> None:
            queue_text = ", ".join(f"{job.project_name}: {job.status}" for job in self.render_queue.jobs[:8])
            workers = 0
            if self.network_controller:
                workers = sum(time.time() - worker.last_seen < 30 for worker in self.network_controller.workers.values())
            self.mobile_state_cache = {
                "project": Path(self.blend_var.get().strip().strip('"')).name or "Waiting for render",
                "status": self.status_var.get(),
                "detail": self.status_detail_var.get(),
                "progress": float(self.progress_var.get()),
                "workers": workers,
                "queue": queue_text or "No queued projects",
                "preview": bool(self.latest_frame_path and self.latest_frame_path.exists()),
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
                    self.network_controller.plan.stopped = True
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
            self.save_current_config()
            self.stop_event = threading.Event()
            self.pause_event = threading.Event()
            self.is_paused = False
            self.paused_queue = False
            self.progress_var.set(0.0)
            self.progress_text_var.set("Starting")
            self.status_var.set("Running")
            self.status_detail_var.set("Blender process is active")
            self.start_button.configure(state="disabled")
            self.pause_button.configure(state="normal")
            self.stop_button.configure(state="normal")
            self.start_queue_button.configure(state="disabled")
            self.log("")
            self.log("Starting watchdog...")

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
                self.render_history.add(session.finish(status))
                self.save_render_history()
                history_recorded = True
                self.log_queue.put(("__HISTORY_REFRESH__", 0, ""))
                self.log_queue.put(("__FINISHED__", code, "Paused" if code == 131 else "Finished"))
                self.log_queue.put(f"Process finished with code {code}.")
            except Exception as error:
                self.log_queue.put(f"Error: {error}")
                self.log_queue.put(("__FINISHED__", 1, "Error"))
            finally:
                if not history_recorded:
                    self.render_history.add(session.finish("failed"))
                    self.save_render_history()
                    self.log_queue.put(("__HISTORY_REFRESH__", 0, ""))
                self.log_queue.put("__WATCHDOG_DONE__")

        def start_render_queue(self) -> None:
            blender_text = self.blender_var.get().strip().strip('"')
            blender = Path(blender_text)
            if not blender.exists():
                messagebox.showerror("Blender not found", "Choose blender.exe on the Render tab first.")
                return
            if not self.use_cpu_var.get() and not self.use_gpu_var.get():
                messagebox.showerror("Render device missing", "Choose at least CPU or GPU for rendering.")
                return

            self.render_queue.reset_unfinished()
            if not self.render_queue.pending():
                messagebox.showinfo("Queue complete", "There are no waiting projects in the queue.")
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
            self.progress_text_var.set("Queue starting")
            self.status_var.set("Queue")
            self.status_detail_var.set("Preparing the first project")
            self.start_button.configure(state="disabled")
            self.start_queue_button.configure(state="disabled")
            self.pause_button.configure(state="normal")
            self.stop_button.configure(state="normal")

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
                        use_cpu=use_cpu,
                        use_gpu=use_gpu,
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
                self.status_var.set("Pausing")
                self.status_detail_var.set("Waiting for current frame to finish")
                self.pause_button.configure(state="disabled")
                self.log("[WATCHDOG] Pause requested. Waiting for current frame to finish...")

        def stop_watchdog(self) -> None:
            if self.stop_event:
                self.stop_event.set()
                self.status_var.set("Stopping")
                self.status_detail_var.set("Terminating Blender safely")
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
                        self.start_button.configure(text="Start render", state="normal")
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
                    self.prediction_var.set(
                        f"≈ {format_duration(prediction.total_seconds)} total · {format_duration(prediction.seconds_per_frame)}/frame"
                    )
                    self.memory_prediction_var.set(
                        f"Memory ≈ {prediction.memory_mb / 1024:.1f} GB · {prediction.confidence} confidence · {prediction.source} · difficult frames: {', '.join(map(str, prediction.difficult_frames))}"
                    )
                    if issues:
                        self.autofix_var.set("\n".join(f"[{issue.severity.upper()}] {issue.message}" for issue in issues[:7]))
                    else:
                        self.autofix_var.set("No common problems found. Ready to render.")
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
                        self.sandbox_status_var.set(f"Sandbox failed: {error}")
                    elif recommendation:
                        self.sandbox_status_var.set(
                            f"Recommended: {recommendation.variant.name} · {format_duration(recommendation.duration_seconds)} · quality {recommendation.quality_score:.0f}%"
                        )
                    else:
                        self.sandbox_status_var.set("No sandbox variant completed successfully")
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__QUEUE_REFRESH__":
                    self.refresh_queue_tree()
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] in {"__FRAME_METRIC__", "__NETWORK_FRAME__"}:
                    self.latest_frame_path = Path(str(message[2]))
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__NETWORK_STARTED__":
                    start = int(message[1])
                    end = int(message[2])
                    self.status_var.set("Network render")
                    self.status_detail_var.set(f"Distributing frames {start}-{end}")
                    self.network_status_var.set("Distributed render is running")
                    if self.network_controller and not self.network_controller.workers:
                        self.log("[NETWORK] No workers yet. Connect a device or choose Use this PC.")
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__NETWORK_ERROR__":
                    self.status_var.set("Network error")
                    self.status_detail_var.set(str(message[1]))
                    self.network_status_var.set(f"Could not start: {message[1]}")
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__HISTORY_REFRESH__":
                    self.refresh_history_views()
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__REMOTE_ACTION__":
                    self.handle_remote_action(str(message[1]))
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__HARDWARE__":
                    cpu = str(message[1])
                    gpus = [str(item) for item in message[2]]
                    previous = set(self.gpu_names)
                    self.cpu_name = cpu
                    self.gpu_names = gpus
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
                        self.status_var.set("Queue")
                        self.status_detail_var.set(f"Rendering {job.project_name}")
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
                        self.status_var.set("Paused")
                        self.status_detail_var.set("Queue can continue from the next frame")
                        self.start_queue_button.configure(text="Continue queue", state="normal")
                        send_notification("Blender Render Watchdog", "Render queue paused after the current frame.")
                    elif self.stop_event and self.stop_event.is_set():
                        self.status_var.set("Stopped")
                        self.status_detail_var.set("Render queue stopped")
                        self.start_queue_button.configure(text="Continue queue", state="normal")
                    else:
                        self.status_var.set("Queue complete")
                        self.status_detail_var.set(f"{completed} complete · {failed} failed")
                        self.start_queue_button.configure(text="Start queue", state="normal")
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
                        self.is_paused = False
                        self.paused_queue = False
                        self.status_var.set("Complete")
                        self.status_detail_var.set("Render finished normally")
                        send_notification("Blender Render Watchdog", "Render finished successfully.")
                        if self.shutdown_after_render_var.get():
                            self.log("[WATCHDOG] Shutdown enabled. Windows will shut down in 60 seconds.")
                            send_notification("Blender Render Watchdog", "Render finished. Shutdown starts in 60 seconds.")
                            schedule_system_shutdown(60)
                    elif code == 131:
                        self.is_paused = True
                        self.paused_queue = False
                        self.status_var.set("Paused")
                        self.status_detail_var.set("Ready to resume from the next frame")
                        self.start_button.configure(text="Resume Render", state="normal")
                        send_notification("Blender Render Watchdog", "Render paused after current frame.")
                    elif code == 130:
                        self.is_paused = False
                        self.paused_queue = False
                        self.status_var.set("Stopped")
                        self.status_detail_var.set("Render stopped by user")
                        send_notification("Blender Render Watchdog", "Render stopped.")
                    else:
                        self.is_paused = False
                        self.paused_queue = False
                        self.status_var.set("Error")
                        self.status_detail_var.set(f"Process exited with code {code}")
                        send_notification("Blender Render Watchdog", f"Render exited with code {code}.")
                    continue

                if isinstance(message, tuple) and len(message) == 4 and message[0] == "__UPDATE__":
                    status = str(message[1])
                    text = str(message[2])
                    manifest = message[3]
                    self.update_status_var.set(text)
                    self.check_update_button.configure(state="normal")
                    if status == "available" and isinstance(manifest, dict):
                        self.latest_update_manifest = manifest
                        self.install_update_button.configure(state="normal")
                        self.log(f"[WATCHDOG] {text}")
                        if self.auto_install_updates_var.get():
                            self.log("[WATCHDOG] Auto install update is enabled. Installing...")
                            self.root.after(500, lambda: self.install_latest_update(ask=False))
                    else:
                        self.latest_update_manifest = None
                        self.install_update_button.configure(state="disabled")
                        self.log(f"[WATCHDOG] {text}")
                    continue

                if isinstance(message, tuple) and len(message) == 3 and message[0] == "__PROGRESS__":
                    percent = float(message[1])
                    self.animate_progress(percent)
                    self.progress_text_var.set(f"{percent:.0f}%  {message[2]}")
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
                should_close = messagebox.askyesno("Stop render", "Рендер сейчас запущен. Остановить Blender и закрыть окно?")
                if not should_close:
                    return
                self.stop_watchdog()

            self.save_current_config()
            self.save_render_queue()
            self.save_render_history()
            if self.network_worker:
                self.network_worker.stop()
            if self.network_controller:
                self.network_controller.stop()
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
        worker = NetworkWorker(
            args.worker_code,
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
