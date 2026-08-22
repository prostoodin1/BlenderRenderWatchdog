import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from ssh_support import query_openssh_state


class OpenSshSupportTests(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows service detection")
    @patch("ssh_support.find_ssh_client", return_value=Path("C:/Windows/System32/OpenSSH/ssh.exe"))
    def test_running_windows_server_is_detected(self, _find) -> None:
        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, "SERVICE_NAME: sshd\nSTATE : 4 RUNNING", "")

        state = query_openssh_state(runner=runner)
        self.assertTrue(state.client_installed)
        self.assertTrue(state.server_installed)
        self.assertTrue(state.server_running)


if __name__ == "__main__":
    unittest.main()
