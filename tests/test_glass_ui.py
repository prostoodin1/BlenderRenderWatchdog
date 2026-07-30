import unittest

from glass_ui import blend_hex, clamp, rounded_points


class GlassUiMathTests(unittest.TestCase):
    def test_blend_hex_interpolates_and_clamps(self) -> None:
        self.assertEqual(blend_hex("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(blend_hex("#112233", "#ffffff", -2), "#112233")
        self.assertEqual(blend_hex("#112233", "#ffffff", 4), "#ffffff")

    def test_clamp_limits_animation_progress(self) -> None:
        self.assertEqual(clamp(-0.2), 0.0)
        self.assertEqual(clamp(0.4), 0.4)
        self.assertEqual(clamp(1.2), 1.0)

    def test_rounded_points_clamps_radius_to_geometry(self) -> None:
        points = rounded_points(0, 0, 20, 10, 50)
        self.assertEqual(len(points), 24)
        self.assertEqual(points[0:2], [5.0, 0])
        self.assertEqual(points[4:6], [20, 0])


if __name__ == "__main__":
    unittest.main()
