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

    def test_group_metadata_is_advertised_without_a_connection_secret(self) -> None:
        group = DiscoveredController(
            "legacy-controller",
            "Studio main",
            "192.168.1.10",
            48620,
            False,
            group_id="group-a",
            group_name="Studio farm",
            coordinator_device_id="device-main",
            device_count=4,
            security_mode="approval",
        )

        payload = encode_announcement(group)
        decoded = decode_announcement(payload, "192.168.1.50")

        self.assertNotIn(b"token", payload)
        self.assertEqual(decoded.effective_group_id, "group-a")
        self.assertEqual(decoded.effective_group_name, "Studio farm")
        self.assertEqual(decoded.device_count, 4)
        self.assertEqual(decoded.security_mode, "approval")

    def test_member_can_advertise_remembered_group_while_coordinator_is_offline(self) -> None:
        cached = DiscoveredController(
            "group-offline",
            "Studio farm",
            "192.168.1.20",
            0,
            True,
            group_id="group-offline",
            coordinator_online=False,
            joinable=False,
        )

        decoded = decode_announcement(encode_announcement(cached), "192.168.1.20")

        self.assertFalse(decoded.coordinator_online)
        self.assertFalse(decoded.joinable)
        self.assertEqual(decoded.port, 0)

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
