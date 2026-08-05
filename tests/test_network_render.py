import base64
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from network_render import NetworkRenderPlan, NetworkWorker, PairingCode, RenderCoordinator, WorkerState, _request_json, worker_device_script


VALID_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class PairingCodeTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        original = PairingCode("192.168.1.5", 8765, "secret")
        self.assertEqual(PairingCode.decode(original.encode()), original)


class SchedulerTests(unittest.TestCase):
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


class CoordinatorHttpTests(unittest.TestCase):
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
        script = worker_device_script(True, True, 128)
        self.assertIn("USE_CPU = True", script)
        self.assertIn("USE_GPU = True", script)
        self.assertIn("SAMPLES = 128", script)
        self.assertIn('scene.cycles.device = "GPU"', script)

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
