import unittest
from unittest.mock import patch

from access_codes import (
    DEFAULT_MOBILE_PORT,
    MobileSyncCode,
    resolve_service_access,
    service_token,
)


class AccessCodeTests(unittest.TestCase):
    def test_persistent_codes_are_stable_and_separate_per_service(self) -> None:
        first = resolve_service_access("persistent", "my-private-key", "mobile")
        second = resolve_service_access("persistent", "my-private-key", "mobile")

        self.assertEqual(first, second)
        self.assertEqual(first.port, DEFAULT_MOBILE_PORT)
        self.assertNotEqual(first.token, service_token("my-private-key", "network"))

    @patch("access_codes.secrets.token_urlsafe", side_effect=["first-token", "second-token"])
    def test_rotating_codes_change_on_every_start(self, _token) -> None:
        first = resolve_service_access("rotate", "unused-key", "mobile")
        second = resolve_service_access("rotate", "unused-key", "mobile")

        self.assertEqual(first.port, 0)
        self.assertNotEqual(first.token, second.token)

    def test_mobile_sync_code_round_trip_supports_unicode_names(self) -> None:
        original = MobileSyncCode("192.168.1.20", 48621, "secret-token", "Ноутбук")

        self.assertEqual(MobileSyncCode.decode(original.encode()), original)

    def test_short_custom_key_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            service_token("short", "mobile")


if __name__ == "__main__":
    unittest.main()
