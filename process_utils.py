"""Cross-platform helpers for quiet background child processes."""

from __future__ import annotations

import os
import subprocess


def hidden_subprocess_kwargs(platform_name: str | None = None) -> dict[str, int]:
    """Prevent console-based helpers from flashing a CMD window on Windows."""
    if (platform_name or os.name) != "nt":
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
