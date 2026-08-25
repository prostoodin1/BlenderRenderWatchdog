"""Persistent render queue primitives for Blender Render Watchdog."""

from __future__ import annotations

import json
import hashlib
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable


QUEUE_STATUSES = {"pending", "running", "completed", "failed", "paused"}
CHUNK_MODES = {"adaptive", "fixed"}
RENDER_DEVICE_MODES = {"AUTO", "CPU", "GPU", "CPU_GPU"}
COMPUTE_BACKENDS = {"AUTO", "OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"}


def normalize_source_path(value: str | Path) -> str:
    """Return a stable local key without requiring the source to exist."""
    path = Path(str(value).strip()).expanduser()
    try:
        normalized = str(path.resolve(strict=False))
    except OSError:
        normalized = str(path)
    return os.path.normcase(normalized)


def project_fingerprint(path: str | Path, sample_size: int = 256 * 1024) -> str:
    """Create a cheap revision fingerprint for large .blend files.

    The file size plus samples from the beginning, middle and end are enough to
    identify revisions for queue synchronisation without hashing multi-gigabyte
    projects every time the UI refreshes.
    """
    source = Path(path)
    try:
        size = source.stat().st_size
        digest = hashlib.sha256()
        digest.update(str(size).encode("ascii"))
        with source.open("rb") as stream:
            offsets = (0, max(0, size // 2 - sample_size // 2), max(0, size - sample_size))
            for offset in dict.fromkeys(offsets):
                stream.seek(offset)
                digest.update(stream.read(sample_size))
        return digest.hexdigest()
    except OSError:
        return ""


def coerce_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(slots=True)
class RenderJob:
    blend_path: str
    output_path: str = ""
    use_scene_output: bool = False
    use_scene_range: bool = True
    start_frame: int | None = None
    end_frame: int | None = None
    resolution_percent: int = 100
    render_mode: str = "frames"
    compose_video: bool = False
    video_format: str = "MP4 (H.264)"
    fps: float = 24.0
    estimated_seconds: float | None = None
    chunk_mode: str = "adaptive"
    chunk_size: int = 10
    render_device_mode: str = "AUTO"
    compute_backend: str = "AUTO"
    source_fingerprint: str = ""
    status: str = "pending"
    attempts: int = 0
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    project_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def __post_init__(self) -> None:
        self.blend_path = str(self.blend_path).strip()
        self.output_path = str(self.output_path).strip()
        if not self.blend_path:
            raise ValueError("blend_path is required")
        if self.status not in QUEUE_STATUSES:
            self.status = "pending"
        self.resolution_percent = max(1, min(100, int(self.resolution_percent)))
        if self.render_mode not in {"frames", "video"}:
            self.render_mode = "frames"
        self.compose_video = bool(self.compose_video or self.render_mode == "video")
        self.fps = max(1.0, min(240.0, float(self.fps)))
        if self.estimated_seconds is not None:
            self.estimated_seconds = max(0.0, float(self.estimated_seconds))
        self.chunk_mode = self.chunk_mode if self.chunk_mode in CHUNK_MODES else "adaptive"
        self.chunk_size = max(1, min(1000, int(self.chunk_size)))
        self.render_device_mode = str(self.render_device_mode).strip().upper()
        if self.render_device_mode not in RENDER_DEVICE_MODES:
            self.render_device_mode = "AUTO"
        self.compute_backend = str(self.compute_backend).strip().upper()
        if self.compute_backend not in COMPUTE_BACKENDS:
            self.compute_backend = "AUTO"
        self.created_at = max(0.0, float(self.created_at or time.time()))
        self.updated_at = max(self.created_at, float(self.updated_at or self.created_at))
        self.project_id = str(self.project_id or uuid.uuid4().hex)
        self.job_id = str(self.job_id or uuid.uuid4().hex)
        if self.use_scene_range:
            self.start_frame = None
            self.end_frame = None
        elif (
            self.start_frame is not None
            and self.end_frame is not None
            and self.start_frame > self.end_frame
        ):
            raise ValueError("start_frame cannot be greater than end_frame")

    @property
    def project_name(self) -> str:
        return Path(self.blend_path).name

    @property
    def range_label(self) -> str:
        if self.use_scene_range:
            return ".blend"
        start = str(self.start_frame) if self.start_frame is not None else "scene start"
        end = str(self.end_frame) if self.end_frame is not None else "scene end"
        return f"{start}–{end}"

    @property
    def output_label(self) -> str:
        if self.use_scene_output:
            return ".blend output"
        return self.output_path or "Project folder"

    @property
    def source_key(self) -> str:
        return normalize_source_path(self.blend_path)

    @property
    def chunk_label(self) -> str:
        return "Auto" if self.chunk_mode == "adaptive" else str(self.chunk_size)

    def refresh_revision(self) -> str:
        fingerprint = project_fingerprint(self.blend_path)
        if fingerprint and fingerprint != self.source_fingerprint:
            self.source_fingerprint = fingerprint
            self.updated_at = time.time()
        return self.source_fingerprint

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "RenderJob":
        start_frame = data.get("start_frame")
        end_frame = data.get("end_frame")
        return cls(
            blend_path=str(data.get("blend_path") or ""),
            output_path=str(data.get("output_path") or ""),
            use_scene_output=coerce_bool(data.get("use_scene_output"), False),
            use_scene_range=coerce_bool(data.get("use_scene_range"), True),
            start_frame=int(start_frame) if start_frame is not None else None,
            end_frame=int(end_frame) if end_frame is not None else None,
            resolution_percent=int(data.get("resolution_percent") or 100),
            render_mode=str(data.get("render_mode") or "frames"),
            compose_video=coerce_bool(data.get("compose_video"), False),
            video_format=str(data.get("video_format") or "MP4 (H.264)"),
            fps=float(data.get("fps") or 24.0),
            estimated_seconds=(
                float(data["estimated_seconds"])
                if data.get("estimated_seconds") is not None
                else None
            ),
            chunk_mode=str(data.get("chunk_mode") or "adaptive"),
            chunk_size=int(data.get("chunk_size") or 10),
            render_device_mode=str(data.get("render_device_mode") or "AUTO"),
            compute_backend=str(data.get("compute_backend") or "AUTO"),
            source_fingerprint=str(data.get("source_fingerprint") or ""),
            status=str(data.get("status") or "pending"),
            attempts=max(0, int(data.get("attempts") or 0)),
            error=str(data.get("error") or ""),
            created_at=float(data.get("created_at") or time.time()),
            updated_at=float(data.get("updated_at") or data.get("created_at") or time.time()),
            project_id=str(data.get("project_id") or data.get("job_id") or uuid.uuid4().hex),
            job_id=str(data.get("job_id") or uuid.uuid4().hex),
        )


class RenderQueue:
    def __init__(self, jobs: Iterable[RenderJob] | None = None, active_job_id: str = "") -> None:
        self.jobs = list(jobs or [])
        self.active_job_id = str(active_job_id or "")
        self._repair_active_job()

    def _repair_active_job(self) -> None:
        if self.active_job_id and self.get(self.active_job_id) is not None:
            return
        active = next((job for job in self.jobs if job.status == "running"), None)
        self.active_job_id = active.job_id if active else (self.jobs[0].job_id if self.jobs else "")

    def add(self, job: RenderJob, activate: bool | None = None) -> RenderJob:
        self.jobs.append(job)
        if activate is True or (activate is None and not self.active_job_id):
            self.active_job_id = job.job_id
        return job

    def add_or_update(self, job: RenderJob, activate: bool = True) -> RenderJob:
        """Avoid duplicate queue rows for the same local project source."""
        existing = self.find_by_source(job.blend_path)
        if existing is None:
            return self.add(job, activate=activate)
        preserved_job_id = existing.job_id
        preserved_project_id = existing.project_id
        for field_name in RenderJob.__dataclass_fields__:
            if field_name not in {"job_id", "project_id", "created_at"}:
                setattr(existing, field_name, getattr(job, field_name))
        existing.job_id = preserved_job_id
        existing.project_id = preserved_project_id
        existing.updated_at = time.time()
        if activate:
            self.active_job_id = existing.job_id
        return existing

    def get(self, job_id: str) -> RenderJob | None:
        return next((job for job in self.jobs if job.job_id == job_id), None)

    def find_by_source(self, blend_path: str | Path) -> RenderJob | None:
        source_key = normalize_source_path(blend_path)
        return next((job for job in self.jobs if job.source_key == source_key), None)

    @property
    def active(self) -> RenderJob | None:
        return self.get(self.active_job_id)

    def set_active(self, job_id: str) -> RenderJob:
        job = self.get(job_id)
        if job is None:
            raise KeyError(f"Unknown queue job: {job_id}")
        self.active_job_id = job.job_id
        return job

    def remove(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        self.jobs.remove(job)
        if self.active_job_id == job_id:
            self.active_job_id = ""
            self._repair_active_job()
        return True

    def move(self, job_id: str, offset: int) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        old_index = self.jobs.index(job)
        new_index = max(0, min(len(self.jobs) - 1, old_index + offset))
        if new_index == old_index:
            return False
        self.jobs.pop(old_index)
        self.jobs.insert(new_index, job)
        return True

    def reset_unfinished(self) -> None:
        for job in self.jobs:
            if job.status in {"running", "failed", "paused"}:
                job.status = "pending"
                job.error = ""

    def pending(self) -> list[RenderJob]:
        return [job for job in self.jobs if job.status == "pending"]

    def smart_sort(self, shortest_first: bool = True) -> None:
        """Sort only waiting jobs; active and finished entries retain their positions."""
        waiting_indices = [index for index, job in enumerate(self.jobs) if job.status == "pending"]
        waiting = [self.jobs[index] for index in waiting_indices]
        unknown = float("inf") if shortest_first else -1.0
        waiting.sort(
            key=lambda job: job.estimated_seconds if job.estimated_seconds is not None else unknown,
            reverse=not shortest_first,
        )
        for index, job in zip(waiting_indices, waiting):
            self.jobs[index] = job

    def to_dict(self) -> dict[str, object]:
        return {
            "version": 2,
            "active_job_id": self.active_job_id,
            "jobs": [job.to_dict() for job in self.jobs],
        }

    def active_snapshot(self) -> dict[str, object] | None:
        job = self.active
        if job is None:
            return None
        return {
            "project_id": job.project_id,
            "job_id": job.job_id,
            "project_name": job.project_name,
            "blend_path": job.blend_path,
            "source_fingerprint": job.source_fingerprint,
            "frame_range": job.range_label,
            "output": job.output_label,
            "status": job.status,
            "chunk_mode": job.chunk_mode,
            "chunk_size": job.chunk_size,
            "render_device_mode": job.render_device_mode,
            "compute_backend": job.compute_backend,
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path) -> "RenderQueue":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        raw_jobs = data.get("jobs", []) if isinstance(data, dict) else []
        jobs: list[RenderJob] = []
        if isinstance(raw_jobs, list):
            for raw_job in raw_jobs:
                if not isinstance(raw_job, dict):
                    continue
                try:
                    jobs.append(RenderJob.from_dict(raw_job))
                except (TypeError, ValueError):
                    continue
        active_job_id = str(data.get("active_job_id") or "") if isinstance(data, dict) else ""
        queue = cls(jobs, active_job_id=active_job_id)
        queue.reset_unfinished()
        return queue
