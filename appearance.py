"""Theme presets and custom accent generation for the desktop UI."""

from __future__ import annotations

import re


THEME_LABELS = {
    "graphite": "Graphite",
    "ocean": "Ocean",
    "emerald": "Emerald",
    "amber": "Amber",
    "rose": "Rose",
    "violet": "Violet",
    "custom": "Custom",
}

THEME_ACCENTS = {
    "graphite": "#70c9e8",
    "ocean": "#43c7ff",
    "emerald": "#55e6a5",
    "amber": "#ffbd59",
    "rose": "#ff7fa8",
    "violet": "#8b7cff",
}

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def normalize_theme(value: str | None) -> str:
    candidate = str(value or "").strip().lower()
    for code, label in THEME_LABELS.items():
        if candidate in {code, label.lower()}:
            return code
    return "graphite"


def normalize_color(value: str | None, fallback: str = "#70c9e8") -> str:
    candidate = str(value or "").strip()
    return candidate.lower() if HEX_COLOR.fullmatch(candidate) else fallback.lower()


def mix_hex(left: str, right: str, amount: float) -> str:
    amount = max(0.0, min(1.0, float(amount)))
    first = tuple(int(left[index:index + 2], 16) for index in (1, 3, 5))
    second = tuple(int(right[index:index + 2], 16) for index in (1, 3, 5))
    mixed = tuple(round(a + (b - a) * amount) for a, b in zip(first, second))
    return "#" + "".join(f"{channel:02x}" for channel in mixed)


def build_palette(theme: str | None, custom_accent: str | None = None) -> dict[str, str]:
    code = normalize_theme(theme)
    accent = normalize_color(custom_accent) if code == "custom" else THEME_ACCENTS[code]
    warm = code in {"amber", "rose"}
    base = {
        "bg": "#0b0e13" if not warm else "#110d10",
        "panel": "#151b23" if not warm else "#20171b",
        "panel_alt": "#1d2732" if not warm else "#2a1e23",
        "field": "#0d131a" if not warm else "#160f13",
        "field_border": "#334252" if not warm else "#4b323d",
        "text": "#f8fbff",
        "muted": "#9cabbc",
        "soft": "#dce7f2",
        "accent": accent,
        "accent_hot": mix_hex(accent, "#ffffff", 0.24),
        "accent_blue": mix_hex(accent, "#9ceaff", 0.36),
        "accent_dark": mix_hex(accent, "#05080d", 0.38),
        "accent_green": "#55e6a5",
        "danger": "#ff6f91",
        "warning": "#ffd166",
        "line": "#334252" if not warm else "#4b323d",
        "line_hot": mix_hex(accent, "#d9f4ff", 0.42),
        "shadow": "#030507",
    }
    return base
