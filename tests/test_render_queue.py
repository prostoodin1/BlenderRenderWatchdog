import json
import tempfile
import unittest
from pathlib import Path

from render_queue import RenderJob, RenderQueue


class RenderJobTests(unittest.TestCase):
    def test_scene_range_clears_manual_values(self) -> None:
        job = RenderJob("scene.blend", use_scene_range=True, start_frame=10, end_frame=20)
        self.assertIsNone(job.start_frame)
        self.assertIsNone(job.end_frame)
        self.assertEqual(job.range_label, ".blend")

    def test_invalid_manual_range_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RenderJob("scene.blend", use_scene_range=False, start_frame=20, end_frame=10)


class RenderQueueTests(unittest.TestCase):
    def test_move_and_remove_keep_stable_job_ids(self) -> None:
        first = RenderJob("first.blend")
        second = RenderJob("second.blend")
        queue = RenderQueue([first, second])

        self.assertTrue(queue.move(second.job_id, -1))
        self.assertEqual([job.project_name for job in queue.jobs], ["second.blend", "first.blend"])
        self.assertTrue(queue.remove(first.job_id))
        self.assertEqual([job.job_id for job in queue.jobs], [second.job_id])

    def test_round_trip_resets_interrupted_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.json"
            running = RenderJob("running.blend", status="running", attempts=1)
            complete = RenderJob("complete.blend", status="completed")
            RenderQueue([running, complete]).save(path)

            restored = RenderQueue.load(path)

        self.assertEqual(restored.jobs[0].status, "pending")
        self.assertEqual(restored.jobs[0].attempts, 1)
        self.assertEqual(restored.jobs[1].status, "completed")

    def test_invalid_jobs_are_skipped_while_loading(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.json"
            path.write_text(
                json.dumps({"jobs": [{"blend_path": ""}, {"blend_path": "good.blend"}]}),
                encoding="utf-8",
            )
            restored = RenderQueue.load(path)

        self.assertEqual([job.project_name for job in restored.jobs], ["good.blend"])

    def test_smart_sort_orders_only_pending_jobs(self) -> None:
        complete = RenderJob("complete.blend", status="completed", estimated_seconds=1)
        long_job = RenderJob("long.blend", estimated_seconds=900)
        short_job = RenderJob("short.blend", estimated_seconds=30)
        queue = RenderQueue([complete, long_job, short_job])

        queue.smart_sort(shortest_first=True)

        self.assertEqual([job.project_name for job in queue.jobs], ["complete.blend", "short.blend", "long.blend"])


if __name__ == "__main__":
    unittest.main()
