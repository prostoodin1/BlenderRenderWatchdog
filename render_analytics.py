"""Render history, timing metrics, and lightweight render prediction."""

from __future__ import annotations

import json
import statistics
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class FrameMetric:
    frame: int
    duration_seconds: float
    finished_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "FrameMetric":
        return cls(
            frame=int(data.get("frame") or 0),
            duration_seconds=max(0.0, float(data.get("duration_seconds") or 0.0)),
            finished_at=str(data.get("finished_at") or utc_now()),
        )


@dataclass(slots=True)
class RenderRecord:
    project_path: str
    output_path: str
    status: str
    started_at: str
    finished_at: str
    duration_seconds: float
    start_frame: int | None = None
    end_frame: int | None = None
    frame_metrics: list[FrameMetric] = field(default_factory=list)
    peak_memory_mb: float | None = None
    mode: str = "frames"
    settings: dict[str, object] = field(default_factory=dict)
    record_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def project_name(self) -> str:
        return Path(self.project_path).name

    @property
    def rendered_frames(self) -> int:
        return len(self.frame_metrics)

    @property
    def average_frame_seconds(self) -> float | None:
        values = [metric.duration_seconds for metric in self.frame_metrics if metric.duration_seconds > 0]
        return statistics.fmean(values) if values else None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "RenderRecord":
        raw_metrics = data.get("frame_metrics") or []
        metrics = [
            FrameMetric.from_dict(item)
            for item in raw_metrics
            if isinstance(item, dict)
        ] if isinstance(raw_metrics, list) else []
        settings = data.get("settings")
        return cls(
            project_path=str(data.get("project_path") or ""),
            output_path=str(data.get("output_path") or ""),
            status=str(data.get("status") or "unknown"),
            started_at=str(data.get("started_at") or utc_now()),
            finished_at=str(data.get("finished_at") or utc_now()),
            duration_seconds=max(0.0, float(data.get("duration_seconds") or 0.0)),
            start_frame=int(data["start_frame"]) if data.get("start_frame") is not None else None,
            end_frame=int(data["end_frame"]) if data.get("end_frame") is not None else None,
            frame_metrics=metrics,
            peak_memory_mb=(
                max(0.0, float(data["peak_memory_mb"]))
                if data.get("peak_memory_mb") is not None
                else None
            ),
            mode=str(data.get("mode") or "frames"),
            settings=dict(settings) if isinstance(settings, dict) else {},
            record_id=str(data.get("record_id") or uuid.uuid4().hex),
        )


class RenderHistory:
    def __init__(self, records: Iterable[RenderRecord] | None = None, limit: int = 250) -> None:
        self.records = list(records or [])[-max(1, limit):]
        self.limit = max(1, limit)
        self._lock = threading.RLock()

    def add(self, record: RenderRecord) -> None:
        with self._lock:
            self.records.append(record)
            self.records = self.records[-self.limit:]

    def recent(self, limit: int = 20) -> list[RenderRecord]:
        with self._lock:
            return list(reversed(self.records[-max(0, limit):]))

    def for_project(self, project_path: str) -> list[RenderRecord]:
        target = str(Path(project_path).resolve()).lower()
        with self._lock:
            result = []
            for record in self.records:
                try:
                    candidate = str(Path(record.project_path).resolve()).lower()
                except OSError:
                    candidate = record.project_path.lower()
                if candidate == target:
                    result.append(record)
            return result

    def hardest_frames(self, project_path: str | None = None, limit: int = 10) -> list[FrameMetric]:
        records = self.for_project(project_path) if project_path else list(self.records)
        metrics = [metric for record in records for metric in record.frame_metrics]
        return sorted(metrics, key=lambda item: item.duration_seconds, reverse=True)[:max(0, limit)]

    def to_dict(self) -> dict[str, object]:
        with self._lock:
            return {"version": 1, "records": [record.to_dict() for record in self.records]}

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path, limit: int = 250) -> "RenderHistory":
        if not path.exists():
            return cls(limit=limit)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(limit=limit)
        raw_records = data.get("records", []) if isinstance(data, dict) else []
        records: list[RenderRecord] = []
        if isinstance(raw_records, list):
            for item in raw_records:
                if not isinstance(item, dict):
                    continue
                try:
                    records.append(RenderRecord.from_dict(item))
                except (TypeError, ValueError):
                    continue
        return cls(records, limit=limit)


class RenderSession:
    """Collects per-frame timings without touching Tk from worker threads."""

    def __init__(
        self,
        project_path: str,
        output_path: str,
        start_frame: int | None,
        end_frame: int | None,
        mode: str = "frames",
        settings: dict[str, object] | None = None,
    ) -> None:
        self.project_path = project_path
        self.output_path = output_path
        self.start_frame = start_frame
        self.end_frame = end_frame
        self.mode = mode
        self.settings = dict(settings or {})
        self.started_at = utc_now()
        self._started_monotonic = time.monotonic()
        self._last_frame_monotonic = self._started_monotonic
        self.frame_metrics: list[FrameMetric] = []
        self._lock = threading.Lock()

    def mark_frame(self, frame: int) -> FrameMetric:
        now = time.monotonic()
        with self._lock:
            duration = max(0.0, now - self._last_frame_monotonic)
            self._last_frame_monotonic = now
            metric = FrameMetric(frame=frame, duration_seconds=duration)
            self.frame_metrics.append(metric)
            return metric

    def finish(self, status: str, peak_memory_mb: float | None = None) -> RenderRecord:
        return RenderRecord(
            project_path=self.project_path,
            output_path=self.output_path,
            status=status,
            started_at=self.started_at,
            finished_at=utc_now(),
            duration_seconds=max(0.0, time.monotonic() - self._started_monotonic),
            start_frame=self.start_frame,
            end_frame=self.end_frame,
            frame_metrics=list(self.frame_metrics),
            peak_memory_mb=peak_memory_mb,
            mode=self.mode,
            settings=dict(self.settings),
        )


@dataclass(slots=True)
class RenderPrediction:
    total_seconds: float
    seconds_per_frame: float
    memory_mb: float
    frame_count: int
    confidence: str
    source: str
    difficult_frames: list[int] = field(default_factory=list)


def _positive_number(value: object, fallback: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return number if number > 0 else fallback


def estimate_render(
    blend_path: Path,
    start_frame: int,
    end_frame: int,
    scene_settings: dict[str, object] | None,
    history: RenderHistory,
    workers: int = 1,
) -> RenderPrediction:
    settings = scene_settings or {}
    frame_count = max(1, end_frame - start_frame + 1)
    completed = [
        record for record in history.for_project(str(blend_path))
        if record.status == "completed" and record.average_frame_seconds
    ]
    if completed:
        weighted = [record.average_frame_seconds or 0.0 for record in completed[-5:]]
        seconds_per_frame = statistics.fmean(weighted)
        source = "project history"
        confidence = "high" if len(weighted) >= 2 else "medium"
    else:
        samples = _positive_number(settings.get("samples"), 128.0)
        width = _positive_number(settings.get("resolution_x"), 1920.0)
        height = _positive_number(settings.get("resolution_y"), 1080.0)
        resolution_percent = _positive_number(settings.get("resolution_percentage"), 100.0) / 100.0
        engine_factor = 1.0 if str(settings.get("engine") or "").upper() == "BLENDER_EEVEE_NEXT" else 2.2
        size_mb = blend_path.stat().st_size / 1_048_576 if blend_path.exists() else 20.0
        complexity = max(0.35, min(6.0, 0.65 + size_mb / 150.0))
        seconds_per_frame = 12.0 * (samples / 128.0) * ((width * height * resolution_percent**2) / (1920 * 1080)) * engine_factor * complexity
        source = "scene heuristic"
        confidence = "low"

    effective_workers = max(1, min(5, int(workers)))
    total_seconds = seconds_per_frame * frame_count / effective_workers
    size_mb = blend_path.stat().st_size / 1_048_576 if blend_path.exists() else 20.0
    width = _positive_number(settings.get("resolution_x"), 1920.0)
    height = _positive_number(settings.get("resolution_y"), 1080.0)
    memory_mb = max(512.0, size_mb * 3.0 + (width * height * 16 / 1_048_576) * 6.0)
    prior_hard = history.hardest_frames(str(blend_path), limit=5)
    difficult = [metric.frame for metric in prior_hard]
    if not difficult:
        difficult = sorted({start_frame, (start_frame + end_frame) // 2, end_frame})
    return RenderPrediction(
        total_seconds=total_seconds,
        seconds_per_frame=seconds_per_frame,
        memory_mb=memory_mb,
        frame_count=frame_count,
        confidence=confidence,
        source=source,
        difficult_frames=difficult,
    )
