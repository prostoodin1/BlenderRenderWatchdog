import unittest

from appearance import THEME_LABELS, build_palette, mix_hex, normalize_color, normalize_theme


class AppearanceTests(unittest.TestCase):
    def test_all_presets_build_complete_palettes(self) -> None:
        required = {"bg", "panel", "panel_alt", "field", "text", "accent", "accent_hot", "accent_dark", "line", "shadow"}
        for theme in THEME_LABELS:
            palette = build_palette(theme, "#123456")
            self.assertTrue(required.issubset(palette), theme)

    def test_custom_theme_uses_validated_accent(self) -> None:
        self.assertEqual(build_palette("custom", "#12ABef")["accent"], "#12abef")
        self.assertEqual(build_palette("custom", "not-a-colour")["accent"], "#70c9e8")

    def test_unknown_theme_is_graphite(self) -> None:
        self.assertEqual(normalize_theme("unknown"), "graphite")
        self.assertEqual(build_palette("unknown")["accent"], build_palette("graphite")["accent"])

    def test_colour_mix_clamps_amount(self) -> None:
        self.assertEqual(mix_hex("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(mix_hex("#000000", "#ffffff", 2), "#ffffff")
        self.assertEqual(normalize_color("#ABCDEF"), "#abcdef")


if __name__ == "__main__":
    unittest.main()
