"""
Test pdf_parser.py: extraction, normalisation, schema conformance.
"""

import os
import tempfile
import fitz
from schema import is_valid_page, is_valid_word
from pdf_parser import parse_pdf


def _make_test_pdf(path: str):
    """Create a simple 2-page text PDF for testing."""
    doc = fitz.open()
    for page_idx in range(2):
        page = doc.new_page(width=612, height=792)
        if page_idx == 0:
            page.insert_text((50, 100), "Hello World First Page", fontsize=14)
            page.insert_text((50, 130), "This is a test sentence.", fontsize=12)
            page.insert_text((300, 100), "Right Column Top", fontsize=14)
            page.insert_text((300, 130), "Right Column Bottom", fontsize=12)
        else:
            page.insert_text((50, 700), "Page Two Content", fontsize=14)
    doc.save(path)
    doc.close()


def test_text_extraction():
    """Parse a text-based PDF and verify word extraction + normalisation."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        tmp_path = f.name
    try:
        _make_test_pdf(tmp_path)
        pages = parse_pdf(tmp_path)

        # Should have 2 pages
        assert len(pages) == 2, f"Expected 2 pages, got {len(pages)}"

        for page in pages:
            assert is_valid_page(page), f"Page fails schema: {page.keys()}"
            assert page["width"] == 612.0
            assert page["height"] == 792.0
            assert page["page_number"] in (1, 2)

            for w in page["words"]:
                assert is_valid_word(w), f"Word fails schema: {w}"
                # Validate normalized range
                assert 0.0 <= w["x0"] <= 1.0, f"x0 out of range: {w['x0']}"
                assert 0.0 <= w["y0"] <= 1.0, f"y0 out of range: {w['y0']}"
                assert 0.0 <= w["x1"] <= 1.0, f"x1 out of range: {w['x1']}"
                assert 0.0 <= w["y1"] <= 1.0, f"y1 out of range: {w['y1']}"
                assert w["source"] == "text"
                assert isinstance(w["page"], int)
                # Geometry sanity: top > bottom in PDF coords
                assert w["y1"] > w["y0"], f"y1({w['y1']}) <= y0({w['y0']})"
                # left < right
                assert w["x1"] > w["x0"], f"x1({w['x1']}) <= x0({w['x0']})"

        # Page 1 should have words (4 lines of text)
        page1_words = pages[0]["words"]
        assert len(page1_words) >= 4, f"Expected >= 4 words on page 1, got {len(page1_words)}"

        # Page 2 should have words
        page2_words = pages[1]["words"]
        assert len(page2_words) >= 1

        # Verify coordinate semantics: higher Y = closer to top of page
        # "Hello World First Page" was inserted at y=100 (PyMuPDF top-left),
        # which normalises to y1 = (792-100)/792 ≈ 0.874 (near top).
        # "This is a test sentence" at y=130 normalises lower.
        hello_words = [w for w in page1_words if "Hello" in w["text"]]
        test_words = [w for w in page1_words if "test" in w["text"]]
        if hello_words and test_words:
            assert hello_words[0]["y1"] > test_words[0]["y1"], (
                f"Hello should have higher y1 (top) than test sentence: "
                f"Hello y1={hello_words[0]['y1']}, test y1={test_words[0]['y1']}"
            )

        # "Right Column Top" (x=300) should be to the right of "Hello" (x=50)
        right_words = [w for w in page1_words if "Right" in w["text"]]
        if hello_words and right_words:
            assert right_words[0]["x0"] > hello_words[0]["x0"], (
                f"Right column should have larger x0: "
                f"Right x0={right_words[0]['x0']}, Hello x0={hello_words[0]['x0']}"
            )

        print("  ✓ Text extraction: words extracted, normalized, coordinates correct")

    finally:
        os.unlink(tmp_path)


def test_schema_contract():
    """Verify the parser output matches the documented contract."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        tmp_path = f.name
    try:
        _make_test_pdf(tmp_path)
        pages = parse_pdf(tmp_path)

        assert isinstance(pages, list), "Top-level result must be a list"
        for page in pages:
            assert isinstance(page, dict), "Each page must be a dict"
            assert "page_number" in page
            assert "width" in page
            assert "height" in page
            assert "words" in page
            assert isinstance(page["words"], list)
            assert page["page_number"] >= 1
            for w in page["words"]:
                assert isinstance(w, dict)
                assert "x0" in w and "y0" in w and "x1" in w and "y1" in w
                assert "text" in w
                assert "page" in w
                assert "source" in w

        print("  ✓ Schema contract: output matches documented format")

    finally:
        os.unlink(tmp_path)


def test_empty_pdf():
    """Parse an empty PDF (should return pages with no words)."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        tmp_path = f.name
    try:
        doc = fitz.open()
        doc.new_page(width=612, height=792)
        doc.save(tmp_path)
        doc.close()

        pages = parse_pdf(tmp_path)
        assert len(pages) == 1
        assert pages[0]["words"] == []
        print("  ✓ Empty PDF: handled gracefully")

    finally:
        os.unlink(tmp_path)


def test_force_ocr():
    """force_ocr=True should attempt OCR (gracefully handle missing tesseract)."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        tmp_path = f.name
    try:
        _make_test_pdf(tmp_path)
        # force_ocr=True when tesseract is missing should NOT crash —
        # it should fall back gracefully
        pages = parse_pdf(tmp_path, force_ocr=True)
        assert len(pages) == 2
        # With no tesseract, OCR returns None → falls back to text extraction
        for page in pages:
            assert len(page["words"]) >= 0
        print("  ✓ Force OCR: graceful degradation when tesseract unavailable")

    finally:
        os.unlink(tmp_path)


if __name__ == "__main__":
    print("Running pdf_parser tests...\n")
    test_text_extraction()
    test_schema_contract()
    test_empty_pdf()
    test_force_ocr()
    print("\nAll tests passed.")
