"""
Post-processing module for PDF layout analysis (normalized coordinates 0.0–1.0).

Coordinate convention: standard PDF — origin at bottom-left, Y increases upward.
  left = x0, top = y1, right = x1, bottom = y0.

Three steps:
  1. Wrap discarded noise words into standalone blocks.
  2. Block-level Z-sort (group by page, then top-to-bottom + left-to-right).
  3. Inject global sequential index (idx) into every word.
"""

from typing import List, Tuple, Any, Optional


# ---------------------------------------------------------------------------
# Coordinate helpers
# ---------------------------------------------------------------------------

def get_word_coords(w: Any) -> Tuple[float, float, float, float]:
    """
    Return (left, top, right, bottom) for a word, correctly mapping
    PDF y1 → top  and  y0 → bottom.
    """
    # Object with x0/y0/x1/y1 attributes
    if hasattr(w, "x0") and hasattr(w, "y0") and hasattr(w, "x1") and hasattr(w, "y1"):
        return float(w.x0), float(w.y1), float(w.x1), float(w.y0)
    # Object with left/top/right/bottom attributes
    if hasattr(w, "left") and hasattr(w, "top") and hasattr(w, "right") and hasattr(w, "bottom"):
        return float(w.left), float(w.top), float(w.right), float(w.bottom)
    # Dict with x0/y0/x1/y1 keys
    if isinstance(w, dict):
        if "x0" in w and "y0" in w and "x1" in w and "y1" in w:
            return float(w["x0"]), float(w["y1"]), float(w["x1"]), float(w["y0"])
        if "left" in w and "top" in w and "right" in w and "bottom" in w:
            return float(w["left"]), float(w["top"]), float(w["right"]), float(w["bottom"])
    raise ValueError(
        f"Unable to extract coordinates from {type(w)}. "
        "Expected keys/attrs: (x0,y0,x1,y1) or (left,top,right,bottom)."
    )


# ---------------------------------------------------------------------------
# Block helpers
# ---------------------------------------------------------------------------

def get_block_words(block: Any) -> List[Any]:
    """Extract list of words from a block container."""
    if isinstance(block, list):
        return block
    if isinstance(block, dict) and "words" in block:
        return block["words"]
    if hasattr(block, "words"):
        return block.words
    return [block]


def _get_page_number(word: Any) -> Optional[int]:
    """Extract page number from a word if present, else None."""
    if isinstance(word, dict):
        for key in ("page", "page_number", "page_num"):
            if key in word:
                pn = word[key]
                return int(pn) if pn is not None else None
    for attr in ("page", "page_number", "page_num"):
        if hasattr(word, attr):
            pn = getattr(word, attr, None)
            if pn is not None:
                return int(pn)
    return None


def get_block_page(block: Any) -> Optional[int]:
    """Return the page number for a block (from its first word), or None."""
    words = get_block_words(block)
    if words:
        return _get_page_number(words[0])
    return None


def get_block_bbox(block: Any) -> Tuple[float, float, float, float]:
    """
    Calculate the overall bounding box of a block.
    Returns (left, top, right, bottom).
    Top = max(y1) of all words, Bottom = min(y0) of all words.
    """
    words = get_block_words(block)
    if not words:
        return 0.0, 0.0, 0.0, 0.0

    min_l = float("inf")
    max_t = float("-inf")   # max top  = max y1
    max_r = float("-inf")
    min_b = float("inf")    # min bottom = min y0

    for w in words:
        left, top, right, bottom = get_word_coords(w)
        if left < min_l:
            min_l = left
        if top > max_t:
            max_t = top
        if right > max_r:
            max_r = right
        if bottom < min_b:
            min_b = bottom

    return min_l, max_t, max_r, min_b


# ---------------------------------------------------------------------------
# Step 1 — Noise word blockification
# ---------------------------------------------------------------------------

def make_noise_block(word: Any) -> dict:
    """Create a standalone single-word block for a discarded noise word.
    Does NOT copy metadata from reference blocks."""
    return {"words": [word]}


# ---------------------------------------------------------------------------
# Step 2 — Block-level Z-sort
# ---------------------------------------------------------------------------

def _sort_single_page(blocks: List[Any], epsilon: float) -> List[Any]:
    """Z-sort blocks within a single page: top-to-bottom, left-to-right."""
    if not blocks:
        return []

    # Pre-compute (left, top) for each block
    infos = []
    for b in blocks:
        left, top, _, _ = get_block_bbox(b)
        infos.append((b, left, top))

    # Sort by top descending (higher Y = closer to top of page = read first).
    # When two blocks are within epsilon on Y, sort by left ascending.
    def sort_key(info):
        _b, left, top = info
        # Quantize top into bands so epsilon tolerance works naturally.
        # Round top to nearest epsilon — blocks in the same band get the
        # same band key, then left breaks the tie.
        band = round(top / epsilon)
        return (-band, left)

    infos.sort(key=sort_key)
    return [info[0] for info in infos]


def page_grouped_zsort(blocks: List[Any], epsilon: float = 0.015) -> List[Any]:
    """
    Group blocks by page, Z-sort within each page, then concatenate pages
    in ascending page-number order.  Blocks without a page number go last.
    """
    # Partition by page
    paged: dict = {}       # page_number → list of blocks
    no_page: list = []     # blocks without page info

    for b in blocks:
        pn = get_block_page(b)
        if pn is not None:
            paged.setdefault(pn, []).append(b)
        else:
            no_page.append(b)

    result: List[Any] = []

    # Pages in ascending order
    for pn in sorted(paged.keys()):
        result.extend(_sort_single_page(paged[pn], epsilon))

    # Blocks without page info at the end (also Z-sorted)
    if no_page:
        result.extend(_sort_single_page(no_page, epsilon))

    return result


# ---------------------------------------------------------------------------
# Step 3 — Global index injection
# ---------------------------------------------------------------------------

def inject_global_index(word: Any, idx: int) -> None:
    """Inject global sequential index into a word."""
    if isinstance(word, dict):
        word["idx"] = idx
    else:
        try:
            setattr(word, "idx", idx)
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def post_process_and_index(
    sorted_text_blocks: List[Any],
    discarded_noise_words: List[Any],
    epsilon: float = 0.015,
) -> Tuple[List[Any], List[Any]]:
    """
    Post-process PDF layout analysis results.

    1. Wraps discarded noise words into individual blocks (no metadata copy).
    2. Merges with sorted text blocks, groups by page, Z-sorts at block level.
    3. Injects global sequential index (idx) into every word.

    Args:
        sorted_text_blocks: Existing blocks with sorted words.
        discarded_noise_words: Isolated words discarded during pre-processing.
        epsilon: Y-alignment tolerance for Z-sorting bands (normalized 0-1).

    Returns:
        (sorted_blocks, flat_word_list) — both with idx injected.
    """
    # Step 1 — noise blockification
    noise_blocks = [make_noise_block(w) for w in discarded_noise_words]

    # Merge
    merged = list(sorted_text_blocks) + noise_blocks
    if not merged:
        return [], []

    # Step 2 — page-grouped Z-sort
    sorted_blocks = page_grouped_zsort(merged, epsilon)

    # Step 3 — global index injection + flatten
    flat_word_list: List[Any] = []
    idx = 0
    for block in sorted_blocks:
        for w in get_block_words(block):
            inject_global_index(w, idx)
            flat_word_list.append(w)
            idx += 1

    return sorted_blocks, flat_word_list
