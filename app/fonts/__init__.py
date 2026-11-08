"""Bundled DejaVu fonts (free licence, see LICENSE-DejaVu.txt) so PDFs can show accented
letters, Greek, Cyrillic and common symbols - the PDF base fonts only cover Latin-1."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parent
FILES = {
    "": "DejaVuSans.ttf",
    "B": "DejaVuSans-Bold.ttf",
    "I": "DejaVuSans-Oblique.ttf",
    "BI": "DejaVuSans-BoldOblique.ttf",
    "mono": "DejaVuSansMono.ttf",
}


def path(style: str) -> str:
    return str(FONT_DIR / FILES[style])


@lru_cache(maxsize=1)
def supported_chars() -> frozenset[int]:
    """Code points the body font can draw (used to warn about missing characters)."""
    from fontTools.ttLib import TTFont

    with TTFont(path(""), lazy=True) as font:
        return frozenset(font.getBestCmap().keys())
