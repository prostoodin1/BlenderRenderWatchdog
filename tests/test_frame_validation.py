import base64
import tempfile
import unittest
from pathlib import Path

from frame_validation import validate_frame


VALID_PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class FrameValidationTests(unittest.TestCase):
    def test_valid_png_passes_crc_and_end_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame_0001.png"
            frame.write_bytes(VALID_PNG)
            self.assertEqual(validate_frame(frame), (True, ""))

    def test_truncated_png_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame_0001.png"
            frame.write_bytes(VALID_PNG[:-8])
            valid, reason = validate_frame(frame)
            self.assertFalse(valid)
            self.assertIn("truncated", reason)


if __name__ == "__main__":
    unittest.main()
