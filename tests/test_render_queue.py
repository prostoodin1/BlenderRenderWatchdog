import json
import tempfile
import unittest
from pathlib import Path

from render_queue import RenderJob, RenderQueue, project_fingerprint


class RenderJobTests(unittest.TestCase):
    def test_scene_range_clears_manual_values(self) -> None:
        job = RenderJob("scene.blend", use_scene_range=True, start_frame=10, end_frame=20)
        self.assertIsNone(job.start_frame)
        self.assertIsNone(job.end_frame)
        self.assertEqual(job.range_label, ".blend")

    def test_invalid_manual_range_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            RenderJob("scene.blend", use_scene_range=False, start_frame=20, end_frame=10)

    def test_render_distribution_settings_are_normalized(self) -> None:
        job = RenderJob(
            "scene.blend",
            chunk_mode="fixed",
            chunk_size=20,
            render_device_mode="gpu",
            compute_backend="optix",
        )

        self.assertEqual(job.chunk_label, "20")
        self.assertEqual(job.render_device_mode, "GPU")
        self.assertEqual(job.compute_backend, "OPTIX")

    def test_project_fingerprint_changes_with_file_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scene.blend"
            path.write_bytes(b"first revision")
            first = project_fingerprint(path)
            path.write_bytes(b"second revision")

            self.assertTrue(first)
            self.assertNotEqual(first, project_fingerprint(path))


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

    def test_round_trip_preserves_one_active_project(self) -> None:
        first = RenderJob("first.blend")
        second = RenderJob("second.blend", chunk_mode="fixed", chunk_size=20)
        queue = RenderQueue([first, second])
        queue.set_active(second.job_id)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.json"
            queue.save(path)
            restored = RenderQueue.load(path)

        self.assertEqual(restored.active_job_id, second.job_id)
        self.assertEqual(restored.active.project_name, "second.blend")
        self.assertEqual(restored.active_snapshot()["chunk_size"], 20)

    def test_old_queue_selects_a_single_active_project_during_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "queue.json"
            path.write_text(
                json.dumps({"version": 1, "jobs": [{"blend_path": "first.blend"}, {"blend_path": "second.blend"}]}),
                encoding="utf-8",
            )
            restored = RenderQueue.load(path)

        self.assertEqual(restored.active, restored.jobs[0])

    def test_add_or_update_deduplicates_the_same_project(self) -> None:
        first = RenderJob("scene.blend", chunk_size=10)
        queue = RenderQueue([first])

        updated = queue.add_or_update(RenderJob("scene.blend", chunk_mode="fixed", chunk_size=20))

        self.assertEqual(len(queue.jobs), 1)
        self.assertEqual(updated.job_id, first.job_id)
        self.assertEqual(updated.project_id, first.project_id)
        self.assertEqual(updated.chunk_size, 20)
        self.assertEqual(queue.active, updated)

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
