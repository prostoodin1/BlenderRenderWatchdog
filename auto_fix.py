"""Preflight checks and safe one-click fixes for common render problems."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AutoFixIssue:
    code: str
    severity: str
    message: str
    fixable: bool = False
    fixed: bool = False


def _is_writable_directory(path: Path) -> bool:
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    return existing.exists() and os.access(existing, os.W_OK)


def inspect_render_setup(
    blender: Path,
    blend: Path,
    output_folder: Path,
    start_frame: int | None,
    end_frame: int | None,
    use_gpu: bool,
    compose_after_render: bool = False,
    scene_settings: dict[str, object] | None = None,
) -> list[AutoFixIssue]:
    issues: list[AutoFixIssue] = []
    settings = scene_settings or {}
    if not blender.exists():
        issues.append(AutoFixIssue("blender_missing", "error", "Blender executable was not found."))
    if not blend.exists():
        issues.append(AutoFixIssue("blend_missing", "error", "The .blend project was not found."))
    elif blend.suffix.lower() != ".blend":
        issues.append(AutoFixIssue("blend_extension", "warning", "The selected project does not use the .blend extension."))
    if start_frame is not None and end_frame is not None and start_frame > end_frame:
        issues.append(AutoFixIssue("invalid_range", "error", "Start frame is greater than end frame."))
    if not output_folder.exists():
        issues.append(AutoFixIssue("output_missing", "warning", "Output folder does not exist.", fixable=True))
    elif not _is_writable_directory(output_folder):
        issues.append(AutoFixIssue("output_readonly", "error", "Output folder is not writable."))
    if not use_gpu and str(settings.get("engine") or "").upper() == "CYCLES":
        issues.append(AutoFixIssue("gpu_disabled", "warning", "Cycles is selected but GPU rendering is disabled.", fixable=True))
    missing_assets = settings.get("missing_external_files")
    if isinstance(missing_assets, list) and missing_assets:
        issues.append(
            AutoFixIssue(
                "missing_assets",
                "error",
                f"{len(missing_assets)} external asset(s) are missing. Pack or relink them in Blender.",
            )
        )
    if compose_after_render and shutil.which("ffmpeg") is None:
        issues.append(AutoFixIssue("ffmpeg_missing", "error", "FFmpeg is required to compose the video."))
    free_target = output_folder if output_folder.exists() else output_folder.parent
    try:
        free_bytes = shutil.disk_usage(free_target).free
        blend_bytes = blend.stat().st_size if blend.exists() else 0
        if free_bytes < max(1_073_741_824, blend_bytes * 5):
            issues.append(AutoFixIssue("disk_space", "warning", "Free disk space may be too low for the render."))
    except OSError:
        pass
    return issues


def apply_safe_fixes(issues: list[AutoFixIssue], output_folder: Path) -> dict[str, bool]:
    changes = {"output_created": False, "enable_gpu": False}
    for issue in issues:
        if issue.code == "output_missing" and issue.fixable:
            try:
                output_folder.mkdir(parents=True, exist_ok=True)
                issue.fixed = True
                changes["output_created"] = True
            except OSError:
                issue.fixed = False
        elif issue.code == "gpu_disabled" and issue.fixable:
            issue.fixed = True
            changes["enable_gpu"] = True
    return changes
