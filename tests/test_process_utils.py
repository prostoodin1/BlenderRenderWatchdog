import unittest
from unittest.mock import patch

from process_utils import hidden_subprocess_kwargs


class HiddenProcessTests(unittest.TestCase):
    def test_windows_background_processes_use_create_no_window(self) -> None:
        with patch("process_utils.subprocess.CREATE_NO_WINDOW", 0x08000000, create=True):
            self.assertEqual(hidden_subprocess_kwargs("nt"), {"creationflags": 0x08000000})

    def test_other_platforms_do_not_receive_windows_flags(self) -> None:
        self.assertEqual(hidden_subprocess_kwargs("posix"), {})


if __name__ == "__main__":
    unittest.main()
