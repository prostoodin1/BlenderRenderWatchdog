import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
ANDROID = ROOT / "android"


class AndroidProjectTests(unittest.TestCase):
    def test_android_release_matches_desktop_version(self) -> None:
        build = (ANDROID / "app" / "build.gradle").read_text(encoding="utf-8")
        self.assertIn('versionName "2.4.1"', build)
        self.assertIn("minSdk 26", build)

    def test_native_app_has_required_tabs_and_saved_devices(self) -> None:
        activity = (ANDROID / "app" / "src" / "main" / "java" / "io" / "github" / "prostoodin1" / "blenderwatchdog" / "MainActivity.java").read_text(encoding="utf-8")
        self.assertIn("showDevices()", activity)
        self.assertIn("showHistory()", activity)
        self.assertIn("showSettings()", activity)
        self.assertIn("SharedPreferences", activity)
        self.assertIn('startsWith("BRWM1-")', activity)
        self.assertIn('postJson(device, "/api/action"', activity)

    def test_lan_http_and_internet_permission_are_explicit(self) -> None:
        manifest = (ANDROID / "app" / "src" / "main" / "AndroidManifest.xml").read_text(encoding="utf-8")
        self.assertIn("android.permission.INTERNET", manifest)
        self.assertIn('android:usesCleartextTraffic="true"', manifest)

    def test_russian_android_resources_are_present(self) -> None:
        strings = (ANDROID / "app" / "src" / "main" / "res" / "values-ru" / "strings.xml").read_text(encoding="utf-8")
        self.assertIn("Устройства", strings)
        self.assertIn("История рендеров", strings)


if __name__ == "__main__":
    unittest.main()
