import unittest
from unittest.mock import patch

from localization import (
    LANGUAGE_LABELS,
    language_code_from_label,
    normalize_language,
    translate,
)


class LocalizationTests(unittest.TestCase):
    def test_language_labels_round_trip_to_stable_codes(self) -> None:
        for code, label in LANGUAGE_LABELS.items():
            self.assertEqual(language_code_from_label(label), code)
            self.assertEqual(normalize_language(code), code)

    def test_russian_interface_translation(self) -> None:
        self.assertEqual(translate("Settings", "ru"), "Настройки")
        self.assertEqual(translate("Language", "ru"), "Язык")
        self.assertEqual(translate("Start render", "ru"), "Начать рендер")

    def test_formatted_status_translation(self) -> None:
        self.assertEqual(
            translate("Current version: {version}", "ru", version="2.1.1"),
            "Текущая версия: 2.1.1",
        )
        self.assertEqual(
            translate(
                "{completed} complete · {failed} failed",
                "ru",
                completed=12,
                failed=1,
            ),
            "Готово: 12 · ошибок: 1",
        )

    def test_english_and_unknown_strings_fall_back_cleanly(self) -> None:
        self.assertEqual(translate("Settings", "en"), "Settings")
        self.assertEqual(translate("Blender-specific value", "ru"), "Blender-specific value")

    @patch("localization.locale.getlocale", return_value=("ru_RU", "UTF-8"))
    def test_russian_system_locale_is_detected(self, _getlocale) -> None:
        self.assertEqual(normalize_language(None), "ru")


if __name__ == "__main__":
    unittest.main()
