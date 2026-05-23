from dataclasses import dataclass

SENTENCE_ENDS = {'.', '。', '!', '？', '?'}
CHINESE_SPLITTERS = {'。', '！', '？', '.', '!', '?'}


@dataclass
class ExpansionResult:
    new_lo: int
    new_hi: int
    head_fragment: bool
    tail_fragment: bool


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

    page_start = lo
    while page_start > 0 and words[page_start - 1].page_idx == page:
        page_start -= 1

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
    if gaps:
        median_line_gap = sorted(gaps)[len(gaps) // 2]
    else:
        # Single-line selection (e.g. heading) — estimate from word heights
        heights = [w.y1_pct - w.y0_pct for w in words[lo:hi + 1] if w.y1_pct > w.y0_pct]
        if heights:
            char_h = sorted(heights)[len(heights) // 2]
            median_line_gap = char_h * 1.2  # line spacing ≈ 1.2× character height
        else:
            median_line_gap = 0.015  # hard fallback: ~1.5% page height

    x0_vals = [w.x0_pct for w in words[lo:hi + 1]]
    x0_median = sorted(x0_vals)[len(x0_vals) // 2] if x0_vals else 0.0

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
            if _is_para_boundary(
                words, i - 1, median_size, bold_ratio, median_line_gap, x0_median,
            ):
                new_hi = i - 1
                break
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
    w = words[i]
    if i + 1 < len(words) and words[i].page_idx == words[i + 1].page_idx and median_line_gap > 0:
        gap = abs(words[i + 1].y0_pct - words[i].y0_pct)
        if gap > median_line_gap * 1.1:
            return True
    return False


def split_sentences(words, lo: int, hi: int) -> list[dict]:
    """Split words[lo..hi] into individual sentences by period boundaries.

    `words` is a list of objects with .text, .page_idx, .x0_pct, .y0_pct, .x1_pct, .y1_pct, .idx.
    Returns list of sentence dicts with start_idx, end_idx, src, and fragment flags.
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


def _build_sub_entry(words, start: int, end: int) -> dict:
    return {
        "start_idx": words[start].idx,
        "end_idx": words[end].idx,
        "src": " ".join(words[i].text for i in range(start, end + 1)),
        "tgt": "",
        "is_head_fragment": False,
        "is_tail_fragment": False,
    }


def join_subs_for_llm(sub_sentences: list[dict]) -> str:
    """Join sub-sentence src texts, inserting <br> at non-punctuation
    boundaries. The LLM preserves <br> markers, giving split_translation
    matching split points when English and Chinese sentence counts differ."""
    parts = []
    for i, sub in enumerate(sub_sentences):
        src = sub["src"]
        parts.append(src)
        if i < len(sub_sentences) - 1:
            stripped = src.rstrip()
            if stripped and stripped[-1] not in SENTENCE_ENDS:
                parts.append("<br>")
    return " ".join(parts)


def split_translation(translation: str, expected_count: int) -> list[str]:
    """Split translation by <br> markers first, then by sentence delimiters."""
    if expected_count <= 0:
        return []
    # Phase 1: split by <br> markers (preserved from LLM input)
    translation = translation.replace("<br>", "\n<br>\n")
    br_segments = [seg.strip() for seg in translation.split("\n<br>\n")]
    # Phase 2: split each segment by periods
    parts = []
    for seg in br_segments:
        if not seg:
            continue
        parts.extend(_split_by_periods(seg))
    parts = [p.strip() for p in parts if p.strip()]
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
