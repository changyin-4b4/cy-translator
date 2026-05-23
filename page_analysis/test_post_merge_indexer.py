"""
Tests for post_merge_indexer.py — verify all four fixes.
"""

from post_merge_indexer import (
    get_word_coords,
    get_block_bbox,
    make_noise_block,
    page_grouped_zsort,
    post_process_and_index,
)


def test_coordinate_naming():
    """Fix 1: get_word_coords returns (left, top, right, bottom) with correct mapping."""
    # PDF: y1=top (higher), y0=bottom (lower)
    w = {"x0": 0.1, "y0": 0.2, "x1": 0.3, "y1": 0.8}
    left, top, right, bottom = get_word_coords(w)
    assert left == 0.1, f"left: {left}"
    assert top == 0.8, f"top should be y1=0.8, got {top}"       # <-- the key fix
    assert right == 0.3, f"right: {right}"
    assert bottom == 0.2, f"bottom should be y0=0.2, got {bottom}"
    print("  ✓ Fix 1: coordinate naming — (left, top, right, bottom) correct")


def test_comparator_top_to_bottom():
    """Fix 2: blocks sorted by max-top descending, then left ascending."""
    # Block A: top=0.9 (higher on page → should be first)
    # Block B: top=0.5 (lower on page → should be second)
    # Block C: top=0.9, left=0.6 (same band as A, but to the right)
    block_a = {"words": [{"x0": 0.1, "y0": 0.85, "x1": 0.4, "y1": 0.90}]}
    block_b = {"words": [{"x0": 0.1, "y0": 0.40, "x1": 0.4, "y1": 0.50}]}
    block_c = {"words": [{"x0": 0.6, "y0": 0.85, "x1": 0.9, "y1": 0.90}]}  # same top as A

    sorted_blocks = page_grouped_zsort([block_b, block_c, block_a], epsilon=0.015)
    assert sorted_blocks == [block_a, block_c, block_b], (
        f"Expected A(top=0.9) → C(top=0.9,right) → B(top=0.5), got tops: "
        f"{[get_block_bbox(b)[1] for b in sorted_blocks]}"
    )
    print("  ✓ Fix 2: Z-sort top-to-bottom, left-to-right within epsilon band")


def test_page_grouping():
    """Fix 3: blocks grouped by page, sorted within each page."""
    block_p1_a = {
        "words": [{"x0": 0.1, "y0": 0.4, "x1": 0.3, "y1": 0.5, "page": 1}]
    }
    block_p1_b = {
        "words": [{"x0": 0.1, "y0": 0.8, "x1": 0.3, "y1": 0.9, "page": 1}]  # top=0.9, should be first in page 1
    }
    block_p2 = {
        "words": [{"x0": 0.1, "y0": 0.1, "x1": 0.3, "y1": 0.2, "page": 2}]
    }

    # Scrambled order
    sorted_blocks = page_grouped_zsort([block_p2, block_p1_a, block_p1_b], epsilon=0.015)
    pages = [get_block_bbox(b) for b in sorted_blocks]
    # Page 1 first: p1_b (top=0.9) → p1_a (top=0.5) → then page 2
    assert len(sorted_blocks) == 3
    assert sorted_blocks[0] == block_p1_b, "Page 1, higher block should be first"
    assert sorted_blocks[1] == block_p1_a, "Page 1, lower block second"
    assert sorted_blocks[2] == block_p2, "Page 2 comes after page 1"
    print("  ✓ Fix 3: page grouping — inter-page isolation works")


def test_noise_block_independent():
    """Fix 4: noise block does NOT leak reference block metadata."""
    ref = {
        "words": [{"x0": 0.1, "y0": 0.2, "x1": 0.3, "y1": 0.4}],
        "label": "body",
        "type": "paragraph",
        "bbox": [0.1, 0.2, 0.3, 0.4],
    }
    noise_word = {"x0": 0.5, "y0": 0.5, "x1": 0.6, "y1": 0.6}

    nb = make_noise_block(noise_word)

    # Should NOT carry over label/type from reference
    assert "label" not in nb, f"Noise block leaked 'label': {nb}"
    assert "type" not in nb, f"Noise block leaked 'type': {nb}"
    assert nb["words"] == [noise_word]
    print("  ✓ Fix 4: noise block is clean, no metadata leak")


def test_end_to_end():
    """Full pipeline: merge, Z-sort, page-group, inject idx."""
    # 3 text blocks (page 1, 2-col layout)
    col1 = {
        "words": [
            {"x0": 0.1, "y0": 0.65, "x1": 0.4, "y1": 0.70, "text": "A1", "page": 1},
            {"x0": 0.1, "y0": 0.55, "x1": 0.4, "y1": 0.60, "text": "A2", "page": 1},
        ]
    }
    col2 = {
        "words": [
            {"x0": 0.6, "y0": 0.65, "x1": 0.9, "y1": 0.70, "text": "B1", "page": 1},
            {"x0": 0.6, "y0": 0.55, "x1": 0.9, "y1": 0.60, "text": "B2", "page": 1},
        ]
    }
    # Page 2 block
    p2_block = {
        "words": [
            {"x0": 0.1, "y0": 0.80, "x1": 0.5, "y1": 0.85, "text": "P2-Top", "page": 2},
        ]
    }
    # Noise word from page 1
    noise = {"x0": 0.2, "y0": 0.45, "x1": 0.25, "y1": 0.48, "text": "noise1", "page": 1}

    sorted_blocks, flat = post_process_and_index(
        sorted_text_blocks=[col1, col2, p2_block],
        discarded_noise_words=[noise],
        epsilon=0.02,
    )

    # All 4 blocks present
    assert len(sorted_blocks) == 4, f"Expected 4 blocks, got {len(sorted_blocks)}"

    # Flat list has 6 words total
    assert len(flat) == 6, f"Expected 6 words, got {len(flat)}"

    # Verify reading order:
    # Page 1: col1 (top=0.70) → col2 (top=0.70, right) → noise (top=0.48, bottom of page)
    # Page 2: P2-Top
    texts = [w.get("text") for w in flat]
    print(f"  Reading order: {texts}")

    # Page 1 words come before page 2
    p1_texts = [w["text"] for w in flat if w.get("page") == 1]
    p2_texts = [w["text"] for w in flat if w.get("page") == 2]
    assert p1_texts == ["A1", "A2", "B1", "B2", "noise1"], f"Page 1 order wrong: {p1_texts}"
    assert p2_texts == ["P2-Top"], f"Page 2 order wrong: {p2_texts}"

    # Global idx injected and sequential
    for i, w in enumerate(flat):
        assert w["idx"] == i, f"Word {w.get('text')} expected idx={i}, got {w['idx']}"

    print("  ✓ End-to-end: merge + page-sort + idx injection correct")


if __name__ == "__main__":
    print("Running post_merge_indexer tests...\n")
    test_coordinate_naming()
    test_comparator_top_to_bottom()
    test_page_grouping()
    test_noise_block_independent()
    test_end_to_end()
    print("\nAll tests passed.")
