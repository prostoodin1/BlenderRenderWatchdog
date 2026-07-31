import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from blender_render_watchdog import APP_VERSION, build_blender_command, find_last_frame, hidden_subprocess_kwargs, run_watchdog


class ReleaseVersionTests(unittest.TestCase):
    def test_release_version_is_221(self) -> None:
        self.assertEqual(APP_VERSION, "2.2.1")


class BackgroundProcessTests(unittest.TestCase):
    def test_windows_background_tools_do_not_open_a_console(self) -> None:
        self.assertEqual(hidden_subprocess_kwargs("nt")["creationflags"], 0x08000000)

    def test_other_platforms_do_not_receive_windows_flags(self) -> None:
        self.assertEqual(hidden_subprocess_kwargs("posix"), {})


class FrameDetectionTests(unittest.TestCase):
    def test_last_frame_is_limited_to_active_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("frame_0001.png", "frame_0010.png", "old_9000.png", "notes.txt"):
                (folder / name).touch()

            result = find_last_frame(folder, min_frame=1, max_frame=20)

        self.assertEqual(result, 10)


class BlenderCommandTests(unittest.TestCase):
    def test_manual_range_and_output_are_forwarded(self) -> None:
        command = build_blender_command(
            blender=Path("blender.exe"),
            blend=Path("scene.blend"),
            frames_folder=Path("renders"),
            start_frame=12,
            end_frame=34,
            padding=5,
            extra_args=["--threads", "4"],
        )

        self.assertEqual(command[-1], "-a")
        self.assertIn("renders\\frame_#####", command)
        self.assertEqual(command[command.index("-s") + 1], "12")
        self.assertEqual(command[command.index("-e") + 1], "34")


class WatchdogRetryTests(unittest.TestCase):
    @patch("blender_render_watchdog.run_blender_process", return_value=7)
    @patch("blender_render_watchdog.rendered_frame_files", return_value={})
    @patch("blender_render_watchdog.build_blender_command", return_value=["blender"])
    @patch("blender_render_watchdog.create_device_script", return_value=None)
    @patch("blender_render_watchdog.query_frame_range", return_value=(1, 1))
    def test_retry_limit_returns_last_blender_exit_code(
        self,
        _query_range,
        _device_script,
        _build_command,
        _rendered_frames,
        run_process,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code = run_watchdog(
                blender=Path("blender.exe"),
                blend=Path("scene.blend"),
                frames_folder=Path(directory),
                sleep_seconds=0,
                max_restarts=2,
                log=lambda _message: None,
            )

        self.assertEqual(code, 7)
        self.assertEqual(run_process.call_count, 3)


if __name__ == "__main__":
    unittest.main()
