import json
import tempfile
import unittest
from pathlib import Path

from resume_startup import (
    arm_resume,
    build_launch_command,
    clear_resume_artifacts,
    load_resume_state,
    mark_resume_attempt,
    windows_startup_dir,
)


class ResumeStartupTests(unittest.TestCase):
    def test_windows_startup_directory_uses_appdata(self) -> None:
        startup = windows_startup_dir(Path("C:/Users/Test/AppData/Roaming"))
        self.assertTrue(str(startup).endswith("Microsoft\\Windows\\Start Menu\\Programs\\Startup"))

    def test_arm_resume_creates_state_and_startup_command(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "resume.json"
            startup_file = root / "Startup" / "Watchdog.cmd"
            command = build_launch_command(Path("C:/Apps/Watchdog.exe"))

            arm_resume(state_path, startup_file, "single", command)

            self.assertEqual(load_resume_state(state_path)["mode"], "single")
            startup_text = startup_file.read_text(encoding="utf-8")
            self.assertIn("--resume-unfinished", startup_text)
            self.assertIn(str(state_path), startup_text)

    def test_attempt_counter_and_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "resume.json"
            startup_file = root / "Watchdog.cmd"
            arm_resume(state_path, startup_file, "queue", "start watchdog --resume-unfinished")

            state = mark_resume_attempt(state_path)
            self.assertEqual(state["attempts"], 1)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["attempts"], 1)

            clear_resume_artifacts(state_path, startup_file)
            self.assertFalse(state_path.exists())
            self.assertFalse(startup_file.exists())

    def test_invalid_state_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "resume.json"
            state_path.write_text('{"mode": "unknown"}', encoding="utf-8")
            self.assertIsNone(load_resume_state(state_path))


if __name__ == "__main__":
    unittest.main()
