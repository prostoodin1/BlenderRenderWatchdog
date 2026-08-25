import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from device_groups import DeviceCapabilities, DeviceIdentity, GroupRegistry, RenderGroup, infer_compute_backends


class DeviceGroupTests(unittest.TestCase):
    def test_compute_backends_are_inferred_from_gpu_vendor(self) -> None:
        self.assertEqual(infer_compute_backends(["NVIDIA GeForce RTX 4090"]), ["OPTIX", "CUDA"])
        self.assertEqual(infer_compute_backends(["AMD Radeon RX 7900"]), ["HIP"])
        self.assertEqual(infer_compute_backends(["Intel Arc A770"]), ["ONEAPI"])

    def test_device_identity_survives_registry_round_trip(self) -> None:
        registry = GroupRegistry(DeviceIdentity("device-a-123456789", "secret-value-that-is-long-enough"))
        registry.create_group("Studio", "approval")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "groups.json"
            registry.save(path)
            restored = GroupRegistry.load(path)

        self.assertEqual(restored.identity.device_id, registry.identity.device_id)
        self.assertEqual(restored.identity.fingerprint, registry.identity.fingerprint)
        self.assertEqual(restored.active.name, "Studio")

    def test_rejoining_device_updates_record_instead_of_duplicating_it(self) -> None:
        group = RenderGroup("Farm", "main-device")
        first = group.register_device(
            "worker-device",
            "Worker",
            identity_fingerprint="fingerprint",
            capabilities=DeviceCapabilities(cpu="Old CPU", gpus=["GPU A"]),
            address="192.168.1.20",
        )
        second = group.register_device(
            "worker-device",
            "Worker renamed",
            identity_fingerprint="fingerprint",
            capabilities=DeviceCapabilities(cpu="New CPU", gpus=["GPU B"], compute_backends=["optix"]),
            address="192.168.1.44",
        )

        self.assertIs(first, second)
        self.assertEqual(len(group.members), 1)
        self.assertEqual(second.name, "Worker renamed")
        self.assertEqual(second.capabilities.cpu, "New CPU")
        self.assertEqual(second.addresses[0], "192.168.1.44")
        self.assertEqual(second.capabilities.compute_backends, ["OPTIX"])

    def test_changed_identity_fingerprint_is_rejected(self) -> None:
        group = RenderGroup("Farm", "main-device")
        group.register_device("worker-device", "Worker", identity_fingerprint="first")

        with self.assertRaises(ValueError):
            group.register_device("worker-device", "Impostor", identity_fingerprint="second")

    def test_cached_offline_device_does_not_look_fresh_after_refresh(self) -> None:
        group = RenderGroup("Farm", "main-device")

        member = group.register_device("worker-device", "Worker", seen_at=10.0)
        group.register_device("worker-device", "Worker", seen_at=10.0)

        self.assertEqual(member.last_seen, 10.0)

    def test_offline_coordinator_fails_over_to_oldest_trusted_member(self) -> None:
        group = RenderGroup("Farm", "main", coordinator_device_id="main", allow_failover=True)
        main = group.register_device("main", "Main", role="coordinator")
        first = group.register_device("first", "First")
        second = group.register_device("second", "Second")
        main.last_seen = 1
        first.last_seen = 100
        second.last_seen = 100
        first.joined_at = 20
        second.joined_at = 30

        with patch("device_groups.time.time", return_value=110):
            elected = group.elect_coordinator(timeout=30)

        self.assertEqual(elected, "first")
        self.assertEqual(group.coordinator_device_id, "first")


if __name__ == "__main__":
    unittest.main()
