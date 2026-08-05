"""Frame-sequence to video composition through FFmpeg."""

from __future__ import annotations

import shutil
import subprocess
import re
from dataclasses import dataclass
from pathlib import Path

from process_utils import hidden_subprocess_kwargs


VIDEO_FORMATS = {
    "MP4 (H.264)": (".mp4", ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart"]),
    "WebM (VP9)": (".webm", ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "30"]),
    "MKV (H.264)": (".mkv", ["-c:v", "libx264", "-pix_fmt", "yuv420p"]),
    "AVI (MJPEG)": (".avi", ["-c:v", "mjpeg", "-q:v", "3"]),
}


@dataclass(slots=True)
class VideoResult:
    output_path: Path
    return_code: int
    output: str

    @property
    def succeeded(self) -> bool:
        return self.return_code == 0 and self.output_path.exists()


def find_ffmpeg(extra_candidates: list[Path] | None = None) -> Path | None:
    executable = shutil.which("ffmpeg")
    if executable:
        return Path(executable)
    for candidate in extra_candidates or []:
        if candidate.exists():
            return candidate
    return None


def video_output_path(frames_folder: Path, project_stem: str, format_name: str) -> Path:
    extension = VIDEO_FORMATS.get(format_name, VIDEO_FORMATS["MP4 (H.264)"])[0]
    return frames_folder / f"{project_stem}{extension}"


def build_ffmpeg_command(
    ffmpeg: Path,
    frames_folder: Path,
    destination: Path,
    fps: float,
    format_name: str,
    padding: int = 4,
    start_number: int | None = None,
    frame_extension: str = ".png",
) -> list[str]:
    _, codec_args = VIDEO_FORMATS.get(format_name, VIDEO_FORMATS["MP4 (H.264)"])
    command = [str(ffmpeg), "-y", "-framerate", f"{max(1.0, fps):g}"]
    if start_number is not None:
        command.extend(["-start_number", str(start_number)])
    extension = frame_extension if frame_extension.startswith(".") else f".{frame_extension}"
    command.extend(["-i", str(frames_folder / f"frame_%0{max(1, padding)}d{extension}")])
    command.extend(codec_args)
    command.append(str(destination))
    return command


def compose_video(
    frames_folder: Path,
    destination: Path,
    fps: float,
    format_name: str,
    padding: int = 4,
    start_number: int | None = None,
    ffmpeg: Path | None = None,
) -> VideoResult:
    ffmpeg_path = ffmpeg or find_ffmpeg()
    if ffmpeg_path is None:
        raise FileNotFoundError("FFmpeg was not found. Add ffmpeg.exe to PATH.")
    sequence: list[tuple[int, str]] = []
    pattern = re.compile(r"^frame_(\d+)(\.[^.]+)$", re.IGNORECASE)
    for path in frames_folder.glob("frame_*.*"):
        match = pattern.match(path.name)
        if match:
            sequence.append((int(match.group(1)), match.group(2).lower()))
    if not sequence:
        raise FileNotFoundError(f"No frame_#### image sequence found in {frames_folder}")
    sequence.sort()
    detected_start, extension = sequence[0]
    if start_number is None:
        start_number = detected_start
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = build_ffmpeg_command(
        ffmpeg_path,
        frames_folder,
        destination,
        fps,
        format_name,
        padding=padding,
        start_number=start_number,
        frame_extension=extension,
    )
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **hidden_subprocess_kwargs(),
    )
    return VideoResult(destination, completed.returncode, completed.stdout + completed.stderr)
