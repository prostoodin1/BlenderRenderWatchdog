import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from tailscale_support import (
    TAILSCALE_WINDOWS_INSTALLER_URL,
    parse_tailscale_status,
    query_tailscale_status,
)


class TailscaleSupportTests(unittest.TestCase):
    def test_running_status_selects_stable_ipv4_and_dns_name(self) -> None:
        state = parse_tailscale_status(
            json.dumps(
                {
                    "BackendState": "Running",
                    "Self": {
                        "TailscaleIPs": ["fd7a:115c:a1e0::1", "100.92.10.4"],
                        "DNSName": "main-pc.example.ts.net.",
                    },
                }
            ),
            Path("tailscale.exe"),
        )
        self.assertTrue(state.installed)
        self.assertTrue(state.online)
        self.assertEqual(state.ipv4, "100.92.10.4")
        self.assertEqual(state.dns_name, "main-pc.example.ts.net")

    def test_needs_login_is_installed_but_offline(self) -> None:
        state = parse_tailscale_status('{"BackendState":"NeedsLogin","Self":{}}')
        self.assertTrue(state.installed)
        self.assertFalse(state.online)
        self.assertIn("sign-in", state.message)

    @patch("tailscale_support.find_tailscale_cli", return_value=None)
    def test_missing_cli_reports_not_installed(self, _find) -> None:
        self.assertFalse(query_tailscale_status().installed)

    @patch("tailscale_support.find_tailscale_cli", return_value=Path("C:/Program Files/Tailscale/tailscale.exe"))
    def test_query_uses_machine_readable_status(self, _find) -> None:
        calls = []

        def runner(command, **_kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"BackendState": "Running", "Self": {"TailscaleIPs": ["100.64.1.2"]}}),
                "",
            )

        state = query_tailscale_status(runner=runner)
        self.assertTrue(state.online)
        self.assertEqual(calls[0][-2:], ["status", "--json"])

    def test_installer_comes_from_official_stable_package_host(self) -> None:
        self.assertEqual(TAILSCALE_WINDOWS_INSTALLER_URL, "https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe")


if __name__ == "__main__":
    unittest.main()
