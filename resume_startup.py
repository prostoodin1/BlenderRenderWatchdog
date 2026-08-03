"""Persistent Windows-startup recovery for unfinished renders."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


RESUME_FILE_NAME = "BlenderRenderWatchdog-Resume.cmd"
VALID_MODES = {"single", "queue"}


def windows_startup_dir(appdata: str | Path | None = None) -> Path:
    base = Path(appdata or os.environ.get("APPDATA") or Path.home())
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def quote_cmd(value: str | Path) -> str:
    return f'"{str(value).replace(chr(34), chr(34) * 2)}"'


def build_launch_command(app_path: Path, source_script: Path | None = None) -> str:
    if source_script is None:
        return f"start \"\" /min {quote_cmd(app_path)} --resume-unfinished"
    return f"start \"\" /min {quote_cmd(app_path)} {quote_cmd(source_script)} --resume-unfinished"


def load_resume_state(path: Path) -> dict[str, object] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or str(data.get("mode") or "") not in VALID_MODES:
        return None
    return data


def write_resume_state(path: Path, mode: str, attempts: int = 0) -> dict[str, object]:
    if mode not in VALID_MODES:
        raise ValueError(f"Unsupported resume mode: {mode}")
    data: dict[str, object] = {
        "version": 1,
        "mode": mode,
        "attempts": max(0, int(attempts)),
        "updated_at": int(time.time()),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temporary.replace(path)
    return data


def mark_resume_attempt(path: Path) -> dict[str, object] | None:
    state = load_resume_state(path)
    if state is None:
        return None
    return write_resume_state(path, str(state["mode"]), int(state.get("attempts") or 0) + 1)


def write_startup_file(path: Path, state_path: Path, launch_command: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        f"if not exist {quote_cmd(state_path)} exit /b 0\r\n"
        f"{launch_command}\r\n",
        encoding="utf-8",
    )
    return path


def arm_resume(
    state_path: Path,
    startup_file: Path,
    mode: str,
    launch_command: str,
    attempts: int = 0,
) -> tuple[Path, Path]:
    write_resume_state(state_path, mode, attempts=attempts)
    write_startup_file(startup_file, state_path, launch_command)
    return state_path, startup_file


def clear_resume_artifacts(state_path: Path, startup_file: Path) -> None:
    for path in (state_path, startup_file):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
