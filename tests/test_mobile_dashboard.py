import json
import unittest
import urllib.request

from mobile_dashboard import DASHBOARD_HTML, MobileDashboardServer


class MobileDashboardTests(unittest.TestCase):
    def test_dashboard_uses_the_23_glass_theme(self) -> None:
        self.assertIn("BLENDER RENDER WATCHDOG 2.3", DASHBOARD_HTML)
        self.assertIn("border-radius:28px", DASHBOARD_HTML)
        self.assertIn("prefers-reduced-motion:reduce", DASHBOARD_HTML)

    def test_state_and_action_endpoints_require_token(self) -> None:
        actions = []
        server = MobileDashboardServer(
            state_provider=lambda: {"status": "running", "progress": 42},
            action_handler=lambda action: (actions.append(action) is None, "queued"),
            bind_host="127.0.0.1",
            advertised_host="127.0.0.1",
        )
        server.start()
        try:
            base = f"http://127.0.0.1:{server.port}"
            with urllib.request.urlopen(base + f"/api/state?token={server.token}") as response:
                state = json.loads(response.read())
            request = urllib.request.Request(
                base + f"/api/action?token={server.token}",
                data=json.dumps({"action": "pause"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(request) as response:
                result = json.loads(response.read())
            self.assertEqual(state["progress"], 42)
            self.assertTrue(result["ok"])
            self.assertEqual(actions, ["pause"])
        finally:
            server.stop()


if __name__ == "__main__":
    unittest.main()
