import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ssh_support import ensure_ssh_invite_key, query_openssh_state, store_invitation_private_key


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

    @patch("ssh_support.find_ssh_keygen", return_value=Path("ssh-keygen.exe"))
    def test_group_invitation_key_is_created_and_reused(self, _find) -> None:
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            if Path(command[0]).name.casefold() == "icacls.exe":
                return subprocess.CompletedProcess(command, 0, "", "")
            private_path = Path(command[command.index("-f") + 1])
            private_path.write_text(
                "-----BEGIN OPENSSH PRIVATE KEY-----\nprivate\n-----END OPENSSH PRIVATE KEY-----\n",
                encoding="utf-8",
            )
            private_path.with_suffix(".pub").write_text("ssh-ed25519 AAAATEST watchdog\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory() as temp:
            first = ensure_ssh_invite_key(Path(temp), "my/group", runner=runner)
            second = ensure_ssh_invite_key(Path(temp), "my/group", runner=runner)

        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(sum(Path(command[0]).name.casefold() == "ssh-keygen.exe" for command in calls), 1)
        self.assertNotIn("my/group", first.private_path.name)

    def test_embedded_private_key_is_saved_to_a_stable_file(self) -> None:
        private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nprivate\n-----END OPENSSH PRIVATE KEY-----\n"
        with tempfile.TemporaryDirectory() as temp:
            first = store_invitation_private_key(private_key, Path(temp))
            second = store_invitation_private_key(private_key, Path(temp))
            self.assertEqual(first, second)
            self.assertEqual(first.read_text(encoding="utf-8"), private_key)


if __name__ == "__main__":
    unittest.main()
