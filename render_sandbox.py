"""Compare test renders made with different Blender settings."""

from __future__ import annotations

import concurrent.futures
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from process_utils import hidden_subprocess_kwargs


@dataclass(slots=True)
class SandboxVariant:
    name: str
    samples: int
    resolution_percent: int
    use_denoise: bool = True

    def __post_init__(self) -> None:
        self.samples = max(1, min(100_000, int(self.samples)))
        self.resolution_percent = max(1, min(100, int(self.resolution_percent)))


@dataclass(slots=True)
class SandboxResult:
    variant: SandboxVariant
    duration_seconds: float
    return_code: int
    output_file: str = ""
    output_size_bytes: int = 0
    quality_score: float = 0.0
    log_tail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.return_code == 0 and bool(self.output_file)


def quality_proxy(variant: SandboxVariant, max_samples: int) -> float:
    sample_score = (variant.samples / max(1, max_samples)) ** 0.35
    resolution_score = (variant.resolution_percent / 100.0) ** 2
    denoise_score = 1.05 if variant.use_denoise else 1.0
    return min(100.0, 100.0 * sample_score * resolution_score * denoise_score)


def _variant_script(variant: SandboxVariant) -> Path:
    path = Path(tempfile.gettempdir()) / f"watchdog_sandbox_{uuid.uuid4().hex}.py"
    path.write_text(
        "import bpy\n"
        "scene = bpy.context.scene\n"
        f"scene.render.resolution_percentage = {variant.resolution_percent}\n"
        "if scene.render.engine == 'CYCLES':\n"
        f"    scene.cycles.samples = {variant.samples}\n"
        f"    scene.cycles.use_denoising = {variant.use_denoise!r}\n",
        encoding="utf-8",
    )
    return path


def build_sandbox_command(
    blender: Path,
    blend: Path,
    output_pattern: Path,
    frame: int,
    script_path: Path,
) -> list[str]:
    return [
        str(blender),
        "-b",
        str(blend),
        "-o",
        str(output_pattern),
        "--python",
        str(script_path),
        "-f",
        str(frame),
    ]


def run_variant(
    blender: Path,
    blend: Path,
    output_root: Path,
    frame: int,
    variant: SandboxVariant,
    max_samples: int,
) -> SandboxResult:
    safe_name = "".join(character if character.isalnum() else "_" for character in variant.name).strip("_") or "variant"
    variant_folder = output_root / safe_name
    variant_folder.mkdir(parents=True, exist_ok=True)
    script = _variant_script(variant)
    before = {path.resolve() for path in variant_folder.glob("sandbox_*") if path.is_file()}
    command = build_sandbox_command(blender, blend, variant_folder / "sandbox_####", frame, script)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            **hidden_subprocess_kwargs(),
        )
        duration = time.monotonic() - started
        candidates = [path for path in variant_folder.glob("sandbox_*") if path.is_file() and path.resolve() not in before]
        output_file = max(candidates, key=lambda path: path.stat().st_mtime, default=None)
        return SandboxResult(
            variant=variant,
            duration_seconds=duration,
            return_code=completed.returncode,
            output_file=str(output_file) if output_file else "",
            output_size_bytes=output_file.stat().st_size if output_file else 0,
            quality_score=quality_proxy(variant, max_samples),
            log_tail=(completed.stdout + completed.stderr)[-2000:],
        )
    finally:
        try:
            script.unlink()
        except OSError:
            pass


def run_sandbox(
    blender: Path,
    blend: Path,
    output_root: Path,
    frame: int,
    variants: list[SandboxVariant],
    parallel: bool = False,
) -> list[SandboxResult]:
    if not variants:
        return []
    max_samples = max(variant.samples for variant in variants)
    if not parallel:
        return [run_variant(blender, blend, output_root, frame, variant, max_samples) for variant in variants]
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(variants))) as executor:
        futures = [
            executor.submit(run_variant, blender, blend, output_root, frame, variant, max_samples)
            for variant in variants
        ]
        return [future.result() for future in futures]


def recommend_variant(results: list[SandboxResult]) -> SandboxResult | None:
    successful = [result for result in results if result.succeeded]
    if not successful:
        return None
    best_quality = max(result.quality_score for result in successful)
    acceptable = [result for result in successful if result.quality_score >= best_quality * 0.75]
    return min(acceptable, key=lambda result: result.duration_seconds)
