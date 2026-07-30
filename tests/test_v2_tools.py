import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from auto_fix import apply_safe_fixes, inspect_render_setup
from render_sandbox import SandboxResult, SandboxVariant, recommend_variant
from video_tools import build_ffmpeg_command, compose_video, video_output_path


class AutoFixTests(unittest.TestCase):
    def test_missing_output_can_be_created(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blender = root / "blender.exe"
            blend = root / "scene.blend"
            blender.touch()
            blend.touch()
            output = root / "new" / "renders"
            issues = inspect_render_setup(blender, blend, output, 1, 2, True)
            changes = apply_safe_fixes(issues, output)

            self.assertTrue(output.is_dir())
            self.assertTrue(changes["output_created"])


class VideoToolsTests(unittest.TestCase):
    def test_builds_mp4_command(self) -> None:
        command = build_ffmpeg_command(
            Path("ffmpeg.exe"), Path("renders"), Path("movie.mp4"), 24, "MP4 (H.264)", start_number=10
        )
        self.assertIn("libx264", command)
        self.assertEqual(command[command.index("-start_number") + 1], "10")
        self.assertEqual(video_output_path(Path("renders"), "scene", "WebM (VP9)").suffix, ".webm")

    def test_compose_detects_blender_sequence_extension_and_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "frame_0012.jpg").touch()
            with patch("video_tools.find_ffmpeg", return_value=Path("ffmpeg.exe")), patch("video_tools.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = ""
                run.return_value.stderr = ""
                destination = folder / "movie.mp4"
                destination.touch()
                compose_video(folder, destination, 24, "MP4 (H.264)")

            command = run.call_args.args[0]
            self.assertTrue(any(part.endswith("frame_%04d.jpg") for part in command))
            self.assertEqual(command[command.index("-start_number") + 1], "12")


class SandboxTests(unittest.TestCase):
    def test_recommends_fast_variant_with_acceptable_quality(self) -> None:
        slow = SandboxResult(SandboxVariant("quality", 256, 100), 30, 0, "slow.png", 10, 100)
        fast = SandboxResult(SandboxVariant("balanced", 128, 100), 10, 0, "fast.png", 10, 80)
        low = SandboxResult(SandboxVariant("draft", 8, 25), 1, 0, "low.png", 10, 10)
        self.assertIs(recommend_variant([slow, fast, low]), fast)


if __name__ == "__main__":
    unittest.main()
