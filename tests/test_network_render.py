import base64
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from network_render import NetworkRenderPlan, NetworkWorker, PairingCode, RenderCoordinator, WorkerState, _request_json, request_pairing, worker_device_script


VALID_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class PairingCodeTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        original = PairingCode("192.168.1.5", 8765, "secret")
        self.assertEqual(PairingCode.decode(original.encode()), original)

    def test_tailscale_code_carries_transport_and_keeps_legacy_compatibility(self) -> None:
        internet = PairingCode("100.92.10.4", 48620, "secret", "tailscale")
        self.assertTrue(internet.encode().startswith("BRW3-"))
        self.assertEqual(PairingCode.decode(internet.encode()), internet)
        self.assertEqual(PairingCode.decode(PairingCode("192.168.1.5", 8765, "secret").encode()).transport, "lan")

    def test_ssh_code_carries_tunnel_endpoint(self) -> None:
        ssh = PairingCode("127.0.0.1", 48620, "secret", "ssh", "render.example.com", 2222, "artist")
        self.assertTrue(ssh.encode().startswith("BRW4-"))
        self.assertEqual(PairingCode.decode(ssh.encode()), ssh)

    def test_connection_code_carries_persistent_group_identity(self) -> None:
        connection = PairingCode(
            "192.168.1.5",
            48620,
            "secret",
            group_id="group-123",
            group_name="Studio farm",
            coordinator_device_id="main-device",
        )

        restored = PairingCode.decode(connection.invitation_link)

        self.assertEqual(restored.group_id, "group-123")
        self.assertEqual(restored.group_name, "Studio farm")
        self.assertEqual(restored.coordinator_device_id, "main-device")

    def test_invitation_link_decodes_like_a_connection_code(self) -> None:
        connection = PairingCode("127.0.0.1", 48620, "secret")
        self.assertEqual(PairingCode.decode(f"brw://join/{connection.encode()}"), connection)

    def test_ssh_invitation_can_carry_and_then_drop_a_private_key(self) -> None:
        private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\ntrusted-render-key\n-----END OPENSSH PRIVATE KEY-----\n"
        invitation = PairingCode(
            "127.0.0.1",
            48620,
            "secret",
            "ssh",
            "render.example.com",
            22,
            "artist",
            private_key,
        )

        decoded = PairingCode.decode(invitation.invitation_link)

        self.assertEqual(decoded.ssh_private_key, private_key)
        self.assertNotIn("trusted-render-key", repr(decoded))
        self.assertEqual(decoded.without_private_key().ssh_private_key, "")


class SchedulerTests(unittest.TestCase):
    def test_fixed_chunk_claims_contiguous_frames(self) -> None:
        plan = NetworkRenderPlan(Path("scene.blend"), Path("renders"), 1, 30, chunk_mode="fixed", chunk_size=10)
        worker = WorkerState("a", "Worker")

        batch = plan.claim_batch(worker)

        self.assertEqual(batch.frames, list(range(1, 11)))
        self.assertEqual(worker.current_frames, list(range(1, 11)))
        self.assertEqual((worker.batch_completed, worker.batch_total), (0, 10))
        self.assertEqual(plan.summary()["running"], 10)

        plan.complete(worker, 1, True)

        self.assertEqual((worker.batch_completed, worker.batch_total), (1, 10))

    def test_manual_worker_chunk_size_overrides_plan_default(self) -> None:
        plan = NetworkRenderPlan(Path("scene.blend"), Path("renders"), 1, 50, chunk_mode="adaptive", chunk_size=10)
        worker = WorkerState("a", "Worker", chunk_size=20)

        batch = plan.claim_batch(worker)

        self.assertEqual(batch.frames, list(range(1, 21)))

    def test_device_speed_contributes_to_network_eta(self) -> None:
        plan = NetworkRenderPlan(Path("scene.blend"), Path("renders"), 1, 12)
        worker = WorkerState("a", "Worker", average_seconds=5.0)
        plan.tasks[1].status = "completed"
        plan.initial_completed = 0

        summary = plan.summary({worker.worker_id: worker})

        self.assertAlmostEqual(summary["frames_per_minute"], 12.0)
        self.assertAlmostEqual(summary["eta_seconds"], 55.0)

    def test_existing_frames_are_skipped_when_resuming(self) -> None:
        plan = NetworkRenderPlan(Path("scene.blend"), Path("renders"), 1, 4, completed_frames={1, 3})
        worker = WorkerState("a", "Worker")
        self.assertEqual(plan.claim(worker).frame, 2)
        self.assertEqual(plan.summary()["completed"], 2)

    def test_manual_worker_range_never_spills_into_other_frames(self) -> None:
        plan = NetworkRenderPlan(Path("scene.blend"), Path("renders"), 1, 10)
        worker = WorkerState("a", "Worker", frame_start=4, frame_end=4)
        self.assertEqual(plan.claim(worker).frame, 4)
        plan.complete(worker, 4, True)
        self.assertIsNone(plan.claim(worker))

    def test_automatic_worker_does_not_take_reserved_manual_frames(self) -> None:
        plan = NetworkRenderPlan(Path("scene.blend"), Path("renders"), 1, 5)
        automatic = WorkerState("auto", "Automatic")
        task = plan.claim(automatic, reserved_ranges=[(1, 3)])
        self.assertEqual(task.frame, 4)

    def test_stop_marks_unassigned_frames_failed(self) -> None:
        plan = NetworkRenderPlan(Path("scene.blend"), Path("renders"), 1, 2)
        plan.stop()
        self.assertTrue(plan.summary()["finished"])

    def test_fast_worker_can_claim_more_frames(self) -> None:
        plan = NetworkRenderPlan(Path("scene.blend"), Path("renders"), 1, 3)
        worker = WorkerState("a", "Fast")
        first = plan.claim(worker)
        self.assertEqual(first.frame, 1)
        plan.complete(worker, 1, True)
        second = plan.claim(worker)
        self.assertEqual(second.frame, 2)

    def test_failed_frame_is_requeued(self) -> None:
        plan = NetworkRenderPlan(Path("scene.blend"), Path("renders"), 1, 1)
        worker = WorkerState("a", "Worker")
        plan.claim(worker)
        task = plan.complete(worker, 1, False, "crash")
        self.assertEqual(task.status, "pending")

    def test_disconnected_worker_frame_is_requeued_immediately(self) -> None:
        plan = NetworkRenderPlan(Path("scene.blend"), Path("renders"), 1, 2)
        worker = WorkerState("worker-a", "Worker")
        task = plan.claim(worker)
        self.assertEqual(task.status, "running")

        released = plan.release_worker(worker.worker_id)

        self.assertEqual(released, [1])
        self.assertEqual(task.status, "pending")
        self.assertEqual(task.worker_id, "")

    def test_summary_reports_range_rate_and_eta_for_current_session(self) -> None:
        plan = NetworkRenderPlan(Path("scene.blend"), Path("renders"), 10, 13, completed_frames={10})
        worker = WorkerState("a", "Worker")
        task = plan.claim(worker)
        self.assertEqual(task.frame, 11)
        task.started_at = 90.0
        plan.created_at = 40.0
        with patch("network_render.time.time", return_value=100.0), patch("network_render.time.monotonic", return_value=100.0):
            plan.complete(worker, 11, True)
            summary = plan.summary()

        self.assertEqual(summary["start_frame"], 10)
        self.assertEqual(summary["end_frame"], 13)
        self.assertAlmostEqual(summary["frames_per_minute"], 1.0)
        self.assertAlmostEqual(summary["frames_per_hour"], 60.0)
        self.assertEqual(summary["remaining_frames"], 2)
        self.assertAlmostEqual(summary["eta_seconds"], 120.0)


class CoordinatorHttpTests(unittest.TestCase):
    def test_same_stable_device_reconnects_without_duplicate_row(self) -> None:
        coordinator = RenderCoordinator()
        first, first_status = coordinator.join(
            "Worker",
            "CPU A",
            device_id="stable-device",
            identity_fingerprint="same-key",
            capabilities={"cpu": "CPU A", "compute_backends": ["CUDA"]},
        )
        second, second_status = coordinator.join(
            "Worker renamed",
            "CPU B",
            device_id="stable-device",
            identity_fingerprint="same-key",
            capabilities={"cpu": "CPU B", "compute_backends": ["OPTIX"]},
        )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(first["worker_id"], second["worker_id"])
        self.assertTrue(second["reconnected"])
        self.assertEqual(len(coordinator.workers), 1)
        worker = coordinator.workers[str(second["worker_id"])]
        self.assertEqual(worker.name, "Worker renamed")
        self.assertEqual(worker.capabilities["compute_backends"], ["OPTIX"])

    def test_device_cannot_reconnect_with_changed_identity(self) -> None:
        coordinator = RenderCoordinator()
        coordinator.join("Worker", "GPU", device_id="stable-device", identity_fingerprint="first-key")

        result, status = coordinator.join(
            "Impostor",
            "GPU",
            device_id="stable-device",
            identity_fingerprint="another-key",
        )

        self.assertEqual(status, 403)
        self.assertFalse(result["ok"])

    def test_one_time_pin_issues_reusable_device_token_and_rotates(self) -> None:
        trusted: list[tuple[str, str]] = []
        coordinator = RenderCoordinator(
            bind_host="127.0.0.1",
            advertised_host="127.0.0.1",
            require_pairing_code=True,
            on_trusted_token=lambda token, name: trusted.append((token, name)),
        )
        coordinator.start()
        try:
            first_pin = coordinator.pairing_pin
            code = request_pairing("127.0.0.1", coordinator.port, "Worker A", first_pin)
            connection = PairingCode.decode(code)
            self.assertNotEqual(connection.token, coordinator.token)
            self.assertIn(connection.token, coordinator.trusted_tokens)
            self.assertEqual(trusted, [(connection.token, "Worker A")])
            self.assertNotEqual(coordinator.pairing_pin, first_pin)
            joined = _request_json(
                f"http://127.0.0.1:{coordinator.port}/api/join",
                connection.token,
                {"name": "Worker A", "hardware": "CPU"},
            )
            self.assertTrue(joined["ok"])
            with self.assertRaises(ConnectionError):
                request_pairing("127.0.0.1", coordinator.port, "Worker B", first_pin)
        finally:
            coordinator.stop()

    def test_controller_can_allow_code_free_lan_pairing(self) -> None:
        coordinator = RenderCoordinator(
            bind_host="127.0.0.1",
            advertised_host="127.0.0.1",
            require_pairing_code=False,
        )
        coordinator.start()
        try:
            connection = PairingCode.decode(request_pairing("127.0.0.1", coordinator.port, "Trusted LAN PC"))
            self.assertIn(connection.token, coordinator.trusted_tokens)
        finally:
            coordinator.stop()

    def test_saved_device_token_survives_controller_restart(self) -> None:
        token = "saved-device-token-that-is-long-enough"
        coordinator = RenderCoordinator(
            bind_host="127.0.0.1",
            advertised_host="127.0.0.1",
            trusted_tokens={token},
        )
        coordinator.start()
        try:
            joined = _request_json(
                f"http://127.0.0.1:{coordinator.port}/api/join",
                token,
                {"name": "Returning worker", "hardware": "GPU"},
            )
            self.assertTrue(joined["ok"])
        finally:
            coordinator.stop()
    def test_controller_disconnects_worker_and_requeues_frame(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blend = root / "scene.blend"
            blend.write_bytes(b"blend")
            coordinator = RenderCoordinator(bind_host="127.0.0.1", advertised_host="127.0.0.1")
            coordinator.start()
            try:
                coordinator.start_plan(blend, root / "renders", 1, 1)
                base = f"http://127.0.0.1:{coordinator.port}"
                joined = _request_json(base + "/api/join", coordinator.token, {"name": "Remote", "hardware": "GPU"})
                worker_id = str(joined["worker_id"])
                _request_json(base + f"/api/task?worker_id={worker_id}", coordinator.token)

                self.assertTrue(coordinator.disconnect_worker(worker_id))
                self.assertEqual(coordinator.plan.summary()["pending"], 1)
                response = _request_json(base + f"/api/task?worker_id={worker_id}", coordinator.token)
                self.assertEqual(response["state"], "disconnected")
                self.assertFalse(response["ok"])
            finally:
                coordinator.stop()

    def test_worker_join_task_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blend = root / "scene.blend"
            blend.write_bytes(b"blend")
            coordinator = RenderCoordinator(bind_host="127.0.0.1", advertised_host="127.0.0.1")
            coordinator.start()
            try:
                coordinator.start_plan(blend, root / "renders", 3, 3)
                base = f"http://127.0.0.1:{coordinator.port}"
                joined = _request_json(base + "/api/join", coordinator.token, {"name": "Test", "hardware": "CPU"})
                worker_id = str(joined["worker_id"])
                coordinator.set_worker_settings(worker_id, 3, 3, 64, False, True)
                status = _request_json(base + "/api/status", coordinator.token)
                self.assertEqual(status["controller"]["name"], coordinator.controller_name)
                self.assertEqual(len(status["devices"]), 2)
                self.assertTrue(status["devices"][0]["is_controller"])
                self.assertEqual(status["devices"][1]["name"], "Test")
                self.assertEqual(status["devices"][1]["render_device"], "GPU")
                task = _request_json(base + f"/api/task?worker_id={worker_id}", coordinator.token)
                self.assertEqual(task["frame"], 3)
                self.assertEqual(task["samples"], 64)
                self.assertFalse(task["use_cpu"])
                self.assertTrue(task["use_gpu"])
                result = _request_json(
                    base + "/api/result",
                    coordinator.token,
                    {
                        "worker_id": worker_id,
                        "frame": 3,
                        "success": True,
                        "extension": ".png",
                        "file_base64": base64.b64encode(VALID_PNG).decode("ascii"),
                    },
                )
                self.assertEqual(result["state"], "completed")
                self.assertEqual((root / "renders" / "frame_0003.png").read_bytes(), VALID_PNG)
            finally:
                coordinator.stop()

    def test_controller_rejects_disabling_every_render_device(self) -> None:
        coordinator = RenderCoordinator()
        worker_id = coordinator.join("Test", "GPU")[0]["worker_id"]
        with self.assertRaises(ValueError):
            coordinator.set_worker_settings(str(worker_id), None, None, None, False, False)

    def test_worker_device_script_applies_gpu_cpu_and_samples(self) -> None:
        script = worker_device_script(True, True, 128, "OPTIX")
        self.assertIn("USE_CPU = True", script)
        self.assertIn("USE_GPU = True", script)
        self.assertIn("SAMPLES = 128", script)
        self.assertIn('scene.cycles.device = "GPU"', script)
        self.assertIn("COMPUTE_BACKEND = 'OPTIX'", script)
        self.assertIn("Requested GPU backend is unavailable", script)

    def test_worker_backend_must_be_supported_when_capabilities_are_known(self) -> None:
        coordinator = RenderCoordinator()
        joined = coordinator.join(
            "Test",
            "GPU",
            device_id="gpu-device",
            capabilities={"compute_backends": ["CUDA"]},
        )[0]

        with self.assertRaises(ValueError):
            coordinator.set_worker_settings(
                str(joined["worker_id"]),
                None,
                None,
                None,
                False,
                True,
                compute_backend="OPTIX",
            )

        self.assertTrue(
            coordinator.set_worker_settings(
                str(joined["worker_id"]),
                None,
                None,
                None,
                False,
                True,
                compute_backend="CUDA",
                chunk_size=20,
            )
        )

    def test_full_worker_loop_downloads_project_and_uploads_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blend = root / "scene.blend"
            blend.write_bytes(b"blend")
            fake_blender = root / "blender.exe"
            fake_blender.touch()
            coordinator = RenderCoordinator(bind_host="127.0.0.1", advertised_host="127.0.0.1")
            coordinator.start()
            coordinator.start_plan(blend, root / "renders", 1, 2)

            def render(frame, _project):
                output = root / f"worker_{frame}.png"
                output.write_bytes(VALID_PNG)
                return True, output, ""

            worker = NetworkWorker(
                coordinator.pairing_code,
                fake_blender,
                cache_folder=root / "cache",
                render_frame=render,
            )
            thread = threading.Thread(target=worker.run, kwargs={"poll_seconds": 0.01})
            thread.start()
            thread.join(timeout=5)
            try:
                self.assertFalse(thread.is_alive())
                self.assertEqual((root / "renders" / "frame_0001.png").read_bytes(), VALID_PNG)
                self.assertEqual((root / "renders" / "frame_0002.png").read_bytes(), VALID_PNG)
                self.assertIn("devices", worker.status_snapshot)
                self.assertEqual(worker.status_snapshot["controller"]["host"], "127.0.0.1")
                self.assertFalse((root / "cache").exists())
            finally:
                worker.stop()
                coordinator.stop()

    def test_worker_continues_with_new_chunk_after_finishing_n_of_n(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blend = root / "scene.blend"
            blend.write_bytes(b"blend")
            fake_blender = root / "blender.exe"
            fake_blender.touch()
            events: list[str] = []
            coordinator = RenderCoordinator(bind_host="127.0.0.1", advertised_host="127.0.0.1")
            coordinator.start()
            coordinator.start_plan(blend, root / "renders", 1, 5, chunk_mode="fixed", chunk_size=2)

            def render(frame, _project):
                output = root / f"worker_{frame}.png"
                output.write_bytes(VALID_PNG)
                return True, output, ""

            worker = NetworkWorker(
                coordinator.pairing_code,
                fake_blender,
                cache_folder=root / "cache",
                render_frame=render,
                on_event=events.append,
            )
            try:
                worker.run(poll_seconds=0.01)
                assignments = [event for event in events if "Rendering frames" in event]
                self.assertEqual(len(assignments), 3)
                self.assertTrue(any("Batch 2/2" in event for event in events))
                self.assertEqual(coordinator.plan.summary()["completed"], 5)
            finally:
                worker.stop()
                coordinator.stop()

    def test_worker_rejoins_after_the_main_pc_temporarily_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blend = root / "scene.blend"
            blend.write_bytes(b"blend")
            fake_blender = root / "blender.exe"
            fake_blender.touch()
            token = "stable-group-token-that-survives-restart"
            first = RenderCoordinator(
                bind_host="127.0.0.1",
                advertised_host="127.0.0.1",
                token=token,
                group_id="remembered-group",
            )
            first.start()
            port = first.port

            def render(frame, _project):
                output = root / f"reconnected_{frame}.png"
                output.write_bytes(VALID_PNG)
                return True, output, ""

            worker = NetworkWorker(
                first.pairing_code,
                fake_blender,
                device_id="stable-worker-device",
                cache_folder=root / "cache",
                render_frame=render,
            )
            thread = threading.Thread(target=worker.run, kwargs={"poll_seconds": 0.02, "stay_connected": True})
            thread.start()
            deadline = time.monotonic() + 3
            while not first.workers and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(first.workers)
            first.stop()
            deadline = time.monotonic() + 4
            while not worker.connection_failed.is_set() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(worker.connection_failed.is_set())

            second = RenderCoordinator(
                bind_host="127.0.0.1",
                port=port,
                advertised_host="127.0.0.1",
                token=token,
                group_id="remembered-group",
            )
            second.start()
            second.start_plan(blend, root / "renders", 1, 1)
            try:
                deadline = time.monotonic() + 8
                output = root / "renders" / "frame_0001.png"
                while not output.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(output.exists())
                self.assertEqual(len(second.workers_by_device), 1)
                self.assertIn("stable-worker-device", second.workers_by_device)
            finally:
                worker.stop()
                thread.join(timeout=3)
                second.stop()

    def test_worker_heartbeats_while_a_frame_is_rendering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blend = root / "scene.blend"
            blend.write_bytes(b"blend")
            fake_blender = root / "blender.exe"
            fake_blender.touch()
            coordinator = RenderCoordinator(bind_host="127.0.0.1", advertised_host="127.0.0.1")
            coordinator.start()
            coordinator.start_plan(blend, root / "renders", 1, 1)
            heartbeat_count = 0
            original_heartbeat = coordinator.heartbeat

            def counted_heartbeat(worker_id: str, **progress):
                nonlocal heartbeat_count
                heartbeat_count += 1
                return original_heartbeat(worker_id, **progress)

            coordinator.heartbeat = counted_heartbeat

            def render(frame, _project):
                time.sleep(0.14)
                output = root / f"worker_{frame}.png"
                output.write_bytes(VALID_PNG)
                return True, output, ""

            worker = NetworkWorker(
                coordinator.pairing_code,
                fake_blender,
                cache_folder=root / "cache",
                render_frame=render,
            )
            try:
                worker.run(poll_seconds=0.01, heartbeat_seconds=0.02)
                self.assertGreaterEqual(heartbeat_count, 2)
                self.assertTrue(coordinator.workers[worker.worker_id].public_dict()["online"])
            finally:
                worker.stop()
                coordinator.stop()

    def test_corrupt_final_frame_is_quarantined_and_requeued(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blend = root / "scene.blend"
            blend.write_bytes(b"blend")
            coordinator = RenderCoordinator(bind_host="127.0.0.1", advertised_host="127.0.0.1")
            coordinator.start()
            try:
                coordinator.start_plan(blend, root / "renders", 1, 1)
                base = f"http://127.0.0.1:{coordinator.port}"
                joined = _request_json(base + "/api/join", coordinator.token, {"name": "Test", "hardware": "CPU"})
                worker_id = str(joined["worker_id"])
                _request_json(base + f"/api/task?worker_id={worker_id}", coordinator.token)
                result = _request_json(
                    base + "/api/result",
                    coordinator.token,
                    {
                        "worker_id": worker_id,
                        "frame": 1,
                        "success": True,
                        "extension": ".png",
                        "file_base64": base64.b64encode(b"broken").decode("ascii"),
                    },
                )
                self.assertEqual(result["summary"]["pending"], 1)
                self.assertEqual(result["summary"]["integrity_retries"], 1)
                self.assertTrue(list((root / "renders").glob("*.corrupt-*")))
                retry = _request_json(base + f"/api/task?worker_id={worker_id}", coordinator.token)
                self.assertEqual(retry["frame"], 1)
                repaired = _request_json(
                    base + "/api/result",
                    coordinator.token,
                    {
                        "worker_id": worker_id,
                        "frame": 1,
                        "success": True,
                        "extension": ".png",
                        "file_base64": base64.b64encode(VALID_PNG).decode("ascii"),
                    },
                )
                self.assertTrue(repaired["summary"]["finished"])
                self.assertTrue(repaired["summary"]["integrity_audited"])
                self.assertEqual(repaired["summary"]["integrity_retries"], 1)
            finally:
                coordinator.stop()


if __name__ == "__main__":
    unittest.main()
