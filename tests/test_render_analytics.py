import tempfile
import unittest
from pathlib import Path

from render_analytics import FrameMetric, RenderHistory, RenderRecord, estimate_render


class RenderAnalyticsTests(unittest.TestCase):
    def test_project_history_drives_prediction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            blend = Path(directory) / "scene.blend"
            blend.write_bytes(b"blend")
            record = RenderRecord(
                project_path=str(blend),
                output_path=directory,
                status="completed",
                started_at="start",
                finished_at="end",
                duration_seconds=20,
                frame_metrics=[FrameMetric(1, 10), FrameMetric(2, 20)],
            )
            prediction = estimate_render(blend, 1, 4, {}, RenderHistory([record]), workers=2)

        self.assertEqual(prediction.source, "project history")
        self.assertAlmostEqual(prediction.seconds_per_frame, 15)
        self.assertAlmostEqual(prediction.total_seconds, 30)

    def test_history_round_trip_and_hardest_frames(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            record = RenderRecord(
                project_path="scene.blend",
                output_path="renders",
                status="completed",
                started_at="start",
                finished_at="end",
                duration_seconds=9,
                frame_metrics=[FrameMetric(1, 2), FrameMetric(2, 7)],
            )
            history = RenderHistory([record])
            history.save(path)
            restored = RenderHistory.load(path)

        self.assertEqual(restored.hardest_frames()[0].frame, 2)
        self.assertEqual(restored.recent()[0].project_name, "scene.blend")


if __name__ == "__main__":
    unittest.main()
