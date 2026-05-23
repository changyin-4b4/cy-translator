"""
PDF parser: PyMuPDF-based text extraction with optional OCR fallback.

Entry point (contract per schema.py):
    parse_pdf(filepath, **kwargs) -> List[dict]

Returns a list of page dicts, each containing normalised word dicts
ready for the XY-Cut / post_merge_indexer pipeline.

OCR swap guide for third-party developers:
    1. Replace the body of _ocr_extract_words() with your own OCR call.
    2. The function must accept (fitz.Page, language: str, dpi: int) and
       return List[dict] of raw word dicts in PyMuPDF top-left coordinates.
    3. Normalisation is handled by the shared _normalize_page() helper —
       you do NOT need to normalise inside your OCR function.
"""

from typing import List, Dict, Optional, Callable
import os
import sys

import fitz  # PyMuPDF


# ═══════════════════════════════════════════════════════════════
# Public entry point
# ═══════════════════════════════════════════════════════════════

def parse_pdf(
    filepath: str,
    *,
    force_ocr: bool = False,
    ocr_language: str = "eng",
    ocr_dpi: int = 300,
    text_min_words: int = 5,
    _doc: fitz.Document | None = None,
) -> List[dict]:
    """
    Parse a PDF file into a list of page dicts with normalised word bboxes.

    Args:
        filepath: Path to the PDF file.
        force_ocr: If True, skip text extraction and use OCR on every page.
        ocr_language: Tesseract language code (e.g. "eng", "chi_sim", "fra").
        ocr_dpi: Render DPI for OCR (higher = better quality, slower).
        text_min_words: If a page has fewer than this many text-extracted
                        words, fall back to OCR for that page.
        _doc: Pre-opened fitz.Document. If provided, used directly instead
              of re-opening the file. Caller retains ownership (not closed).

    Returns:
        List of page dicts, each with keys:
            page_number  int
            width        float   (PDF points)
            height       float   (PDF points)
            words        List[dict]   canonical word dicts (normalised)
    """
    own_doc = _doc is None
    if own_doc:
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"PDF not found: {filepath}")
        doc = fitz.open(filepath)
    else:
        doc = _doc

    pages: List[dict] = []

    try:
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            page_info = _parse_page(
                page,
                page_number=page_idx + 1,
                force_ocr=force_ocr,
                ocr_language=ocr_language,
                ocr_dpi=ocr_dpi,
                text_min_words=text_min_words,
            )
            pages.append(page_info)
    finally:
        if own_doc:
            doc.close()

    return pages


# ═══════════════════════════════════════════════════════════════
# Internal: per-page parsing logic
# ═══════════════════════════════════════════════════════════════

def _parse_page(
    page: fitz.Page,
    page_number: int,
    force_ocr: bool,
    ocr_language: str,
    ocr_dpi: int,
    text_min_words: int,
) -> dict:
    """Parse a single page, choosing text-extraction or OCR path."""
    raw_words: List[dict] = []

    if not force_ocr:
        raw_words = _text_extract_words(page)
        if len(raw_words) >= text_min_words:
            return _normalize_page(page, page_number, raw_words, source="text")

    # Either forced or text extraction yielded too few words → OCR
    if force_ocr or len(raw_words) < text_min_words:
        ocr_words = _ocr_extract_words(page, language=ocr_language, dpi=ocr_dpi)
        if ocr_words is not None and len(ocr_words) > 0:
            return _normalize_page(page, page_number, ocr_words, source="ocr")

    # OCR unavailable or returned nothing — fall back to whatever text we got
    return _normalize_page(page, page_number, raw_words, source="text")


# ═══════════════════════════════════════════════════════════════
# Text extraction (PyMuPDF native — fast, works without tesseract)
# ═══════════════════════════════════════════════════════════════

def _text_extract_words(page: fitz.Page) -> List[dict]:
    """
    Extract words via PyMuPDF's built-in text extraction.

    PyMuPDF returns (x0, y0, x1, y1, word, block_no, line_no, word_no)
    with origin at TOP-LEFT (y increases downward).
    """
    words: List[dict] = []
    for item in page.get_text("words"):
        x0, y0, x1, y1, text, block_no, line_no, word_no = item
        text = text.strip()
        if not text:
            continue
        words.append({
            "x0": x0,
            "y0": y0,   # PyMuPDF top (will be flipped during normalisation)
            "x1": x1,
            "y1": y1,   # PyMuPDF bottom
            "text": text,
            "block_no": block_no,
            "line_no": line_no,
            "word_no": word_no,
        })
    return words


# ═══════════════════════════════════════════════════════════════
# OCR extraction (PyMuPDF built-in Tesseract wrapper)
# ═══════════════════════════════════════════════════════════════
#
# SWAP GUIDE — to replace the OCR backend:
#   1. Implement a function with this exact signature:
#          def my_ocr(page: fitz.Page, language: str, dpi: int) -> List[dict]
#   2. Each returned dict must have keys: x0, y0, x1, y1, text
#      in PyMuPDF TOP-LEFT coordinates.
#   3. Assign your function to _ocr_extract_words at module level,
#      or monkey-patch:  pdf_parser._ocr_extract_words = my_ocr
#   4. Normalisation is handled downstream — do NOT normalise here.
# ═══════════════════════════════════════════════════════════════

def _ocr_extract_words(
    page: fitz.Page,
    language: str = "eng",
    dpi: int = 300,
) -> Optional[List[dict]]:
    """
    OCR a page via PyMuPDF's built-in Tesseract integration.

    Returns None if OCR is unavailable (no tesseract binary, missing
    language data, or runtime error).  Callers must handle None gracefully.
    """
    try:
        textpage = page.get_textpage_ocr(
            flags=3,           # recognise text
            language=language,
            dpi=dpi,
            full=False,
        )
    except Exception:
        # Typical causes: tesseract binary missing, language pack not
        # installed, or memory pressure on large pages.
        return None

    if textpage is None:
        return None

    words: List[dict] = []
    try:
        # extractWORDS() returns (x0, y0, x1, y1, word, block_no, line_no, word_no)
        # in PyMuPDF top-left coordinates — same format as _text_extract_words.
        for item in textpage.extractWORDS():
            x0, y0, x1, y1, text, block_no, line_no, word_no = item
            text = text.strip()
            if not text:
                continue
            words.append({
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "text": text,
                "block_no": block_no,
                "line_no": line_no,
                "word_no": word_no,
            })
    finally:
        try:
            textpage.free()
        except AttributeError:
            pass

    return words


# ═══════════════════════════════════════════════════════════════
# Coordinate normalization
# ═══════════════════════════════════════════════════════════════

def _normalize_page(
    page: fitz.Page,
    page_number: int,
    raw_words: List[dict],
    source: str,
) -> dict:
    """
    Convert raw PyMuPDF words (top-left origin) into canonical normalised
    words (bottom-left origin, 0–1 range).
    """
    pw = page.rect.width
    ph = page.rect.height

    normalized_words: List[dict] = []
    for rw in raw_words:
        # PyMuPDF top-left → PDF bottom-left
        left   = rw["x0"]
        bottom = ph - rw["y1"]    # PyMuPDF y1 (bottom edge) → distance from true bottom
        right  = rw["x1"]
        top    = ph - rw["y0"]    # PyMuPDF y0 (top edge) → distance from true bottom

        nw = {
            "x0": round(left / pw, 8),
            "y0": round(bottom / ph, 8),
            "x1": round(right / pw, 8),
            "y1": round(top / ph, 8),
            "text": rw["text"],
            "page": page_number,
            "source": source,
        }
        # Carry forward PyMuPDF structural hints (useful for debugging)
        if "block_no" in rw:
            nw["block_no"] = rw["block_no"]
        if "line_no" in rw:
            nw["line_no"] = rw["line_no"]
        if "word_no" in rw:
            nw["word_no"] = rw["word_no"]

        normalized_words.append(nw)

    return {
        "page_number": page_number,
        "width": pw,
        "height": ph,
        "words": normalized_words,
    }
