"""Persistent render queue primitives for Blender Render Watchdog."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable


QUEUE_STATUSES = {"pending", "running", "completed", "failed", "paused"}


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
    status: str = "pending"
    attempts: int = 0
    error: str = ""
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
            status=str(data.get("status") or "pending"),
            attempts=max(0, int(data.get("attempts") or 0)),
            error=str(data.get("error") or ""),
            job_id=str(data.get("job_id") or uuid.uuid4().hex),
        )


class RenderQueue:
    def __init__(self, jobs: Iterable[RenderJob] | None = None) -> None:
        self.jobs = list(jobs or [])

    def add(self, job: RenderJob) -> RenderJob:
        self.jobs.append(job)
        return job

    def get(self, job_id: str) -> RenderJob | None:
        return next((job for job in self.jobs if job.job_id == job_id), None)

    def remove(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None:
            return False
        self.jobs.remove(job)
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
        return {"version": 1, "jobs": [job.to_dict() for job in self.jobs]}

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
        queue = cls(jobs)
        queue.reset_unfinished()
        return queue
