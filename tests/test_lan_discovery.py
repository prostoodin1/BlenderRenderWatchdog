import unittest
import socket

from lan_discovery import DiscoveredController, LanDiscoveryAdvertiser, decode_announcement, discover_controllers, encode_announcement


class LanDiscoveryTests(unittest.TestCase):
    def test_announcement_contains_no_access_token_and_uses_sender_address(self) -> None:
        controller = DiscoveredController("controller-a", "Main PC", "192.168.1.10", 48620, True)
        payload = encode_announcement(controller)
        self.assertNotIn(b"token", payload)
        decoded = decode_announcement(payload, "192.168.1.44")
        self.assertEqual(decoded.host, "192.168.1.44")
        self.assertEqual(decoded.name, "Main PC")
        self.assertTrue(decoded.requires_code)

    def test_invalid_announcement_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            decode_announcement(b'{}', "127.0.0.1")

    def test_local_probe_finds_running_controller(self) -> None:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        advertiser = LanDiscoveryAdvertiser(
            DiscoveredController("controller-local", "Studio PC", "127.0.0.1", 48620, False),
            discovery_port=port,
        )
        advertiser.start()
        try:
            found = discover_controllers(timeout=0.4, discovery_port=port)
            self.assertEqual([item.controller_id for item in found], ["controller-local"])
            self.assertFalse(found[0].requires_code)
        finally:
            advertiser.stop()


if __name__ == "__main__":
    unittest.main()
