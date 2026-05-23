"""
Canonical data contracts for the PDF layout pipeline.

All coordinates are NORMALIZED (0.0–1.0), standard PDF origin at bottom-left
(Y increases upward).  Field naming follows the convention used by the
downstream XY-Cut and post-processing modules:
    x0 = left,  y0 = bottom,  x1 = right,  y1 = top

This module does NOT import any third-party libraries.  It defines the format
that every parser must output and every consumer expects.
"""

from typing import List, Optional, Dict, Any

# ──────────────────────────────────────────────────────────────
# Canonical Word dict
# ──────────────────────────────────────────────────────────────
# Keys every word dict MUST contain:
#
#   "x0"     float   left edge   (normalized 0–1)
#   "y0"     float   bottom edge (normalized 0–1)
#   "x1"     float   right edge  (normalized 0–1)
#   "y1"     float   top edge    (normalized 0–1)
#   "text"   str     text content (may be empty for whitespace-only boxes)
#   "page"   int     page number, 1-indexed
#
# Keys every word dict MAY contain:
#
#   "source"       str     "text" | "ocr" — how this word was obtained
#   "confidence"   float   OCR confidence (0–1), if applicable
#   "block_id"     str     opaque block identifier from the parser
#
# There is no class — plain dicts are used throughout for zero-copy interop
# with JSON serialisation and to avoid coupling consumers to any specific
# object hierarchy.

REQUIRED_WORD_KEYS = {"x0", "y0", "x1", "y1", "text", "page"}


def is_valid_word(obj: Any) -> bool:
    """Return True if *obj* (dict or object) carries the required word fields."""
    if isinstance(obj, dict):
        return REQUIRED_WORD_KEYS.issubset(obj.keys())
    return all(hasattr(obj, k) for k in REQUIRED_WORD_KEYS)


# ──────────────────────────────────────────────────────────────
# Page descriptor (output of a parser)
# ──────────────────────────────────────────────────────────────
#   "page_number"   int           1-indexed page number
#   "width"         float         page width in PDF points  (before normalisation)
#   "height"        float         page height in PDF points (before normalisation)
#   "words"         List[dict]    canonical word dicts for this page

REQUIRED_PAGE_KEYS = {"page_number", "width", "height", "words"}


def is_valid_page(obj: Any) -> bool:
    """Return True if *obj* carries the required page-level fields."""
    if isinstance(obj, dict):
        return REQUIRED_PAGE_KEYS.issubset(obj.keys())
    return all(hasattr(obj, k) for k in REQUIRED_PAGE_KEYS)


# ──────────────────────────────────────────────────────────────
# Parser interface (documented contract, not an ABC)
# ──────────────────────────────────────────────────────────────
#
# Every parser must expose a function with this signature:
#
#   def parse_pdf(filepath: str, **kwargs) -> List[dict]:
#       '''
#       Parse a PDF file into a list of page dicts.
#
#       Args:
#           filepath: Absolute or relative path to the PDF.
#           **kwargs: Parser-specific options (e.g. force_ocr, language).
#
#       Returns:
#           List of page dicts, one per page, each conforming to
#           REQUIRED_PAGE_KEYS and containing words that conform to
#           REQUIRED_WORD_KEYS.
#
#       Words MUST have normalised coordinates (0.0–1.0).
#       Pages MUST appear in natural page order (ascending page_number).
#       '''
#
# Third-party developers swapping in a different OCR engine or parser need
# only implement a function matching this signature.  The rest of the
# pipeline (XY-Cut, post_merge_indexer) consumes its output unchanged.
