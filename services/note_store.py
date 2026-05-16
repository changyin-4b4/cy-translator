import json
import os
from pathlib import Path

DEFAULT_FONT_SIZE = 12
DEFAULT_NOTE_WIDTH = 200
DEFAULT_NOTE_HEIGHT = 120


def load_notes(path: str | None) -> tuple[list[dict], int]:
    """Load notes from JSON file. Returns (notes_list, font_size).
    Silently returns empty list + default font size on any error."""
    if not path:
        return [], DEFAULT_FONT_SIZE
    p = Path(path)
    if not p.exists():
        return [], DEFAULT_FONT_SIZE
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return [], DEFAULT_FONT_SIZE
    if not isinstance(data, dict):
        return [], DEFAULT_FONT_SIZE
    notes = data.get("notes", [])
    if not isinstance(notes, list):
        notes = []
    font_size = data.get("font_size", DEFAULT_FONT_SIZE)
    if not isinstance(font_size, int) or font_size < 10 or font_size > 24:
        font_size = DEFAULT_FONT_SIZE
    return notes, font_size


def save_notes(path: str, notes: list[dict], font_size: int) -> None:
    """Save notes to JSON file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {"font_size": font_size, "notes": notes}
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
