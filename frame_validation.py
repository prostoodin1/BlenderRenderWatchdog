"""Dependency-free integrity checks for rendered image files."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _validate_png(path: Path) -> tuple[bool, str]:
    with path.open("rb") as stream:
        if stream.read(8) != b"\x89PNG\r\n\x1a\n":
            return False, "invalid PNG signature"
        while True:
            raw_length = stream.read(4)
            if len(raw_length) != 4:
                return False, "truncated PNG chunk"
            length = struct.unpack(">I", raw_length)[0]
            chunk_type = stream.read(4)
            if len(chunk_type) != 4:
                return False, "truncated PNG chunk type"
            crc = zlib.crc32(chunk_type)
            remaining = length
            while remaining:
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    return False, "truncated PNG chunk data"
                crc = zlib.crc32(block, crc)
                remaining -= len(block)
            raw_crc = stream.read(4)
            if len(raw_crc) != 4 or struct.unpack(">I", raw_crc)[0] != crc & 0xFFFFFFFF:
                return False, "invalid PNG checksum"
            if chunk_type == b"IEND":
                return (length == 0, "" if length == 0 else "invalid PNG end chunk")


def validate_frame(path: Path) -> tuple[bool, str]:
    """Validate common Blender output formats without loading the full image into memory."""
    try:
        size = path.stat().st_size
        if size < 8:
            return False, "file is empty or truncated"
        suffix = path.suffix.lower()
        if suffix == ".png":
            return _validate_png(path)
        with path.open("rb") as stream:
            head = stream.read(32)
            stream.seek(max(0, size - 32))
            tail = stream.read(32)
        if suffix in {".jpg", ".jpeg"}:
            return (head.startswith(b"\xff\xd8") and b"\xff\xd9" in tail, "invalid or truncated JPEG")
        if suffix == ".bmp":
            declared = struct.unpack("<I", head[2:6])[0] if len(head) >= 6 else 0
            return (head.startswith(b"BM") and 0 < declared <= size, "invalid or truncated BMP")
        if suffix == ".webp":
            declared = struct.unpack("<I", head[4:8])[0] + 8 if len(head) >= 12 else 0
            return (head.startswith(b"RIFF") and head[8:12] == b"WEBP" and declared <= size, "invalid or truncated WebP")
        if suffix in {".tif", ".tiff"}:
            little = head.startswith(b"II*\x00")
            big = head.startswith(b"MM\x00*")
            if not (little or big) or len(head) < 8:
                return False, "invalid TIFF header"
            offset = struct.unpack("<I" if little else ">I", head[4:8])[0]
            return (8 <= offset < size, "invalid or truncated TIFF")
        if suffix == ".exr":
            return (head.startswith(b"\x76\x2f\x31\x01") and size >= 64, "invalid or truncated OpenEXR")
        if suffix == ".hdr":
            return (head.startswith((b"#?RADIANCE", b"#?RGBE")) and size >= 64, "invalid or truncated HDR")
        if suffix == ".tga":
            return (size >= 18, "invalid or truncated TGA")
        return (size >= 32, "file is too small")
    except OSError as error:
        return False, str(error)
