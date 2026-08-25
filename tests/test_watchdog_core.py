import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from blender_render_watchdog import (
    APP_VERSION,
    build_blender_command,
    build_update_script,
    find_last_frame,
    hidden_subprocess_kwargs,
    normalize_update_manifest,
    run_watchdog,
    stable_hardware_snapshot,
)


class ReleaseVersionTests(unittest.TestCase):
    def test_release_version_is_301(self) -> None:
        self.assertEqual(APP_VERSION, "3.0.1")


class BackgroundProcessTests(unittest.TestCase):
    def test_windows_background_tools_do_not_open_a_console(self) -> None:
        self.assertEqual(hidden_subprocess_kwargs("nt")["creationflags"], 0x08000000)

    def test_other_platforms_do_not_receive_windows_flags(self) -> None:
        self.assertEqual(hidden_subprocess_kwargs("posix"), {})


class UpdaterTests(unittest.TestCase):
    def test_update_script_waits_retries_verifies_and_restarts_from_install_folder(self) -> None:
        script = build_update_script(
            "https://example.test/BlenderRenderWatchdog.exe",
            Path("C:/Temp/watchdog-new.exe"),
            Path("C:/Apps/Watchdog/BlenderRenderWatchdog.exe"),
            1234,
            "a" * 64,
        )

        self.assertIn("Get-FileHash", script)
        self.assertIn('PYINSTALLER_RESET_ENVIRONMENT = "1"', script)
        self.assertIn("for ($Attempt = 0; $Attempt -lt 30", script)
        self.assertIn("-WorkingDirectory $TargetDirectory", script)
        self.assertIn("the previous version was restored", script)

    def test_github_digest_is_normalized_for_updater(self) -> None:
        manifest = normalize_update_manifest(
            {"tag_name": "v2.5.0", "exe_url": "https://example.test/app.exe", "digest": "sha256:" + "b" * 64}
        )
        self.assertEqual(manifest["sha256"], "b" * 64)


class FrameDetectionTests(unittest.TestCase):
    def test_last_frame_is_limited_to_active_range(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for name in ("frame_0001.png", "frame_0010.png", "old_9000.png", "notes.txt"):
                (folder / name).touch()

            result = find_last_frame(folder, min_frame=1, max_frame=20)

        self.assertEqual(result, 10)


class HardwareDetectionTests(unittest.TestCase):
    def test_transient_gpu_failure_keeps_last_real_devices(self) -> None:
        cpu, gpus = stable_hardware_snapshot(
            "Ryzen 9",
            ["NVIDIA RTX 4090"],
            "Unknown CPU",
            ["No GPU detected by Windows"],
        )
        self.assertEqual(cpu, "Ryzen 9")
        self.assertEqual(gpus, ["NVIDIA RTX 4090"])

    def test_new_hardware_replaces_previous_snapshot(self) -> None:
        cpu, gpus = stable_hardware_snapshot("Old CPU", ["Old GPU"], "New CPU", ["New GPU"])
        self.assertEqual(cpu, "New CPU")
        self.assertEqual(gpus, ["New GPU"])


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
