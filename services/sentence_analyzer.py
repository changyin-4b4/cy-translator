from dataclasses import dataclass

SENTENCE_ENDS = {'.', '。', '!', '？', '?'}
CHINESE_SPLITTERS = {'。', '！', '？', '.', '!', '?'}


@dataclass
class ExpansionResult:
    new_lo: int
    new_hi: int
    head_fragment: bool  # True = "残" (selection doesn't start at sentence boundary)
    tail_fragment: bool  # True = "残" (selection doesn't end at sentence boundary)


def is_sentence_end(text: str) -> bool:
    """Check if stripped text's last non-space character is a sentence terminator."""
    s = text.strip()
    return bool(s) and s[-1] in SENTENCE_ENDS


def classify(text: str, word_count: int, auto_complete: bool) -> str:
    """Return 'phrase' or 'sentence'."""
    if auto_complete:
        return "sentence" if word_count > 5 else "phrase"
    else:
        return "sentence" if _has_period(text) else "phrase"


def _has_period(text: str) -> bool:
    return any(ch in SENTENCE_ENDS for ch in text)


def expand_to_sentence(words, lo: int, hi: int) -> ExpansionResult:
    """Expand lo/hi to nearest enclosing sentence boundaries within the same page.

    `words` is a list of objects with .text, .page_idx, .size, .flags, .y0_pct.
    Scans stop at page boundaries and paragraph boundaries.
    head_fragment/tail_fragment are True when no period is found within the page.
    """
    n = len(words)
    page = words[lo].page_idx

    # Find word boundaries of the current page
    page_start = lo
    while page_start > 0 and words[page_start - 1].page_idx == page:
        page_start -= 1
    # ── Baseline statistics from selection ──────────────────────────
    sizes = [w.size for w in words[lo:hi + 1] if w.size > 0]
    median_size = sorted(sizes)[len(sizes) // 2] if sizes else 0.0

    bold_count = sum(1 for w in words[lo:hi + 1] if w.flags & 16)
    bold_ratio = bold_count / (hi - lo + 1)

    gaps = []
    for i in range(lo, hi):
        if words[i].page_idx == words[i + 1].page_idx:
            gap = abs(words[i + 1].y0_pct - words[i].y0_pct)
            if gap > 0:
                gaps.append(gap)
    median_line_gap = sorted(gaps)[len(gaps) // 2] if gaps else 0.0

    x0_vals = [w.x0_pct for w in words[lo:hi + 1]]
    x0_median = sorted(x0_vals)[len(x0_vals) // 2] if x0_vals else 0.0

    # Left scan within lo's page
    new_lo = page_start
    head_fragment = True
    for i in range(lo - 1, page_start - 1, -1):
        if _is_para_boundary(
            words, i, median_size, bold_ratio, median_line_gap, x0_median,
        ):
            new_lo = i + 1
            break
        if is_sentence_end(words[i].text):
            new_lo = i + 1
            head_fragment = False
            break

    # Right scan within hi's own page
    hi_page = words[hi].page_idx
    hi_page_end = hi
    while hi_page_end < n - 1 and words[hi_page_end + 1].page_idx == hi_page:
        hi_page_end += 1

    if is_sentence_end(words[hi].text):
        new_hi = hi
        tail_fragment = False
    else:
        new_hi = hi_page_end
        tail_fragment = True
        for i in range(hi + 1, hi_page_end + 1):
            if is_sentence_end(words[i].text):
                new_hi = i
                tail_fragment = False
                break

    return ExpansionResult(
        new_lo=new_lo,
        new_hi=new_hi,
        head_fragment=head_fragment,
        tail_fragment=tail_fragment,
    )


def _is_para_boundary(words, i: int, median_size: float,
                      bold_ratio: float, median_line_gap: float,
                      x0_median: float) -> bool:
    """Check if word i is a paragraph boundary (i.e. word i+1 starts new para)."""
    w = words[i]
    if i + 1 < len(words) and words[i].page_idx == words[i + 1].page_idx and median_line_gap > 0:
        gap = abs(words[i + 1].y0_pct - words[i].y0_pct)
        if gap > median_line_gap * 1.1:
            return True
    return False


def split_sentences(words, lo: int, hi: int) -> list[dict]:
    """Split words[lo..hi] into individual sentences by period boundaries.

    `words` is a list of objects with .text, .page_idx, .x0_pct, .y0_pct, .x1_pct, .y1_pct.
    Returns list of sentence dicts with coordinates, src, and fragment flags.
    """
    if lo > hi:
        return []
    sentences = []
    current_start = lo
    for i in range(lo, hi + 1):
        text = words[i].text.strip()
        if text and text[-1] in SENTENCE_ENDS and i < hi:
            sentences.append(_build_sub_entry(words, current_start, i))
            current_start = i + 1
    if current_start <= hi:
        sentences.append(_build_sub_entry(words, current_start, hi))
    return sentences


def _build_sub_entry(words, start_idx: int, end_idx: int) -> dict:
    sw = words[start_idx]
    ew = words[end_idx]
    return {
        "start_page": sw.page_idx,
        "start_x_pct": sw.x0_pct,
        "start_y_pct": sw.y0_pct,
        "end_page": ew.page_idx,
        "end_x_pct": ew.x1_pct,
        "end_y_pct": ew.y0_pct,
        "src": " ".join(words[i].text for i in range(start_idx, end_idx + 1)),
        "tgt": "",
        "is_head_fragment": False,
        "is_tail_fragment": False,
    }


def transform_dual_column_coords(sub_sentences: list[dict]) -> list[dict]:
    """Shift right-column (x_pct >= 0.5) coordinates into logical space:
    x_pct -= 0.5, y_pct += 1.0. Left-column coordinates unchanged.

    After transform, left-column y stays in [0, 1), right-column y in [1, 2),
    eliminating Y-axis overlap between the two columns.
    """
    for sub in sub_sentences:
        cx = (sub["start_x_pct"] + sub["end_x_pct"]) / 2.0
        if cx >= 0.5:
            sub["start_x_pct"] -= 0.5
            sub["start_y_pct"] += 1.0
            sub["end_x_pct"] -= 0.5
            sub["end_y_pct"] += 1.0
    return sub_sentences


def split_translation(translation: str, expected_count: int) -> list[str]:
    """Split Chinese translation by sentence delimiters into expected_count pieces.

    Always splits by Chinese/English periods first.
    - Count matches → perfect 1:1 mapping.
    - Too few pieces → pad with empty strings at the end.
    - Too many pieces → merge excess into the last slot (joined with '。').
    - Empty translation → returns list of empty strings.
    """
    if expected_count <= 0:
        return []
    parts = _split_by_periods(translation.strip())
    if not parts:
        return [""] * expected_count
    if len(parts) == expected_count:
        return parts
    if len(parts) < expected_count:
        return parts + [""] * (expected_count - len(parts))
    merged_last = "".join(parts[expected_count - 1:])
    return parts[:expected_count - 1] + [merged_last]


def _split_by_periods(text: str, delimiters: str = "。！？") -> list[str]:
    """Split text by given delimiters, keeping delimiters with preceding sentence."""
    if not text:
        return []
    result = []
    buf = []
    for ch in text:
        buf.append(ch)
        if ch in delimiters:
            result.append("".join(buf))
            buf.clear()
    if buf:
        result.append("".join(buf))
    return result
