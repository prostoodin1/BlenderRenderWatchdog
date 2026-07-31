import base64
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from network_render import NetworkRenderPlan, NetworkWorker, PairingCode, RenderCoordinator, WorkerState, _request_json


class PairingCodeTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        original = PairingCode("192.168.1.5", 8765, "secret")
        self.assertEqual(PairingCode.decode(original.encode()), original)


class SchedulerTests(unittest.TestCase):
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


class CoordinatorHttpTests(unittest.TestCase):
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
                status = _request_json(base + "/api/status", coordinator.token)
                self.assertEqual(status["controller"]["name"], coordinator.controller_name)
                self.assertEqual(len(status["devices"]), 2)
                self.assertTrue(status["devices"][0]["is_controller"])
                self.assertEqual(status["devices"][1]["name"], "Test")
                task = _request_json(base + f"/api/task?worker_id={worker_id}", coordinator.token)
                self.assertEqual(task["frame"], 3)
                result = _request_json(
                    base + "/api/result",
                    coordinator.token,
                    {
                        "worker_id": worker_id,
                        "frame": 3,
                        "success": True,
                        "extension": ".png",
                        "file_base64": base64.b64encode(b"png").decode("ascii"),
                    },
                )
                self.assertEqual(result["state"], "completed")
                self.assertEqual((root / "renders" / "frame_0003.png").read_bytes(), b"png")
            finally:
                coordinator.stop()

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
                output.write_bytes(f"frame-{frame}".encode())
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
                self.assertEqual((root / "renders" / "frame_0001.png").read_bytes(), b"frame-1")
                self.assertEqual((root / "renders" / "frame_0002.png").read_bytes(), b"frame-2")
                self.assertIn("devices", worker.status_snapshot)
                self.assertEqual(worker.status_snapshot["controller"]["host"], "127.0.0.1")
            finally:
                worker.stop()
                coordinator.stop()


if __name__ == "__main__":
    unittest.main()
