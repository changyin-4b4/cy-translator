"""
Evaluation harness: DocBank → XY-Cut → Kendall's Tau.

Pipeline:
  1. Load DocBank .txt  →  tokens with (text, x0, y0, x1, y1, label)
  2. Filter figure/table, group tokens into blocks, assign GT_ID per block
  3. Shuffle blocks (simulates unordered parser output)
  4. Normalise coords → run XY-Cut → extract GT_ID sequence
  5. Kendall's Tau against ideal [0, 1, 2, ...]
"""

import os
import sys
import random
import math
from typing import List, Dict, Tuple, Any

from scipy.stats import kendalltau

# ─────────────────────────────────────────────────────────
# Threshold adaptation
#
# The XY-Cut sorters were ported from Java where MIN_GAP_THRESHOLD is
# in absolute PDF points (typically ~0.5–5.0 for a letter-size page).
# With normalised coordinates (0–1 range) the threshold must be
# scaled down.  We set it to ~0.5 % of page width, which for a
# typical 1000 px page equates to ~5 px — matching the original intent.
# ─────────────────────────────────────────────────────────

_NORM_GAP_THRESHOLD = 0.005

import xy_cut_sorter
xy_cut_sorter.MIN_GAP_THRESHOLD = _NORM_GAP_THRESHOLD

from xy_cut_sorter import CleanedXYCutSorter
SORTER_CLASS = CleanedXYCutSorter
SORTER_NAME = "XY-Cut++ (isolation detection)"

# ─────────────────────────────────────────────────────────
# DocBank loader
# ─────────────────────────────────────────────────────────

# Labels classified as "noise" — these block types are filtered out
# before assigning ground-truth IDs because they are not text flow.
NOISE_LABELS = {"figure", "table"}

# Special DocBank tokens to skip
SKIP_TOKENS = {"##LTLine##", "##LTLine##\n"}


def parse_docbank_txt(filepath: str) -> List[dict]:
    """
    Parse a single DocBank .txt file into a list of token dicts.

    DocBank format (tab-separated):
        token  x0  y0  x1  y1  R  G  B  font  label

    Coordinates are absolute pixel values (typically 0–1000 range).
    The file order IS the correct reading order (top-to-bottom, left-to-right).
    """
    tokens: List[dict] = []
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 10:
                continue
            text = parts[0].strip()
            if not text or text in SKIP_TOKENS:
                continue
            try:
                x0, y0, x1, y1 = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
            except ValueError:
                continue
            label = parts[-1].strip().lower()

            tokens.append({
                "text": text,
                "x0": x0,
                "y0": y0,
                "x1": x1,
                "y1": y1,
                "label": label,
            })
    return tokens


# ─────────────────────────────────────────────────────────
# Block grouping
# ─────────────────────────────────────────────────────────

def group_tokens_into_blocks(tokens: List[dict]) -> List[dict]:
    """
    Group consecutive same-label tokens into semantic blocks.

    A block has:
        "label"  : str             — semantic label (paragraph, title, ...)
        "words"  : List[dict]      — tokens in original order
        "bbox"   : (x0, y0, x1, y1) — union of all word bboxes (raw coords)
        "gt_id"  : int             — assigned after filtering (see assign_gt_ids)
    """
    blocks: List[dict] = []
    current_label = None
    current_words: List[dict] = []

    def _flush():
        nonlocal current_label, current_words
        if current_words:
            # Compute block bbox as union of word bboxes
            min_x = min(w["x0"] for w in current_words)
            min_y = min(w["y0"] for w in current_words)
            max_x = max(w["x1"] for w in current_words)
            max_y = max(w["y1"] for w in current_words)
            blocks.append({
                "label": current_label,
                "words": current_words,
                "bbox": (min_x, min_y, max_x, max_y),
            })
        current_words = []

    for tok in tokens:
        lbl = tok["label"]
        if lbl != current_label:
            _flush()
            current_label = lbl
        current_words.append(tok)

    _flush()
    return blocks


def assign_gt_ids(blocks: List[dict]) -> int:
    """
    Assign sequential GT_ID to each block (in-place).

    Blocks whose label is in NOISE_LABELS are removed from the list.
    Returns the number of text blocks retained.
    """
    filtered: List[dict] = []
    for b in blocks:
        if b["label"] not in NOISE_LABELS:
            filtered.append(b)

    blocks.clear()
    blocks.extend(filtered)

    for i, b in enumerate(blocks):
        b["gt_id"] = i
        for w in b["words"]:
            w["gt_id"] = i

    return len(blocks)


# ─────────────────────────────────────────────────────────
# Coordinate normalisation
# ─────────────────────────────────────────────────────────

def normalise_blocks(blocks: List[dict]) -> None:
    """
    Normalise all word/block coordinates to 0–1 (in-place).

    DocBank uses image coordinates (origin TOP-LEFT, Y increases downward).
    The XY-Cut pipeline expects PDF coordinates (origin BOTTOM-LEFT, Y
    increases upward).  We therefore:
      1. Compute the page extent from max(x1) and max(y1).
      2. Flip Y:  new_y0 = (page_h - old_y1) / page_h
                  new_y1 = (page_h - old_y0) / page_h
      3. Round to 8 decimal places.
    """
    if not blocks:
        return

    # Determine page extent
    max_x = max_y = 1.0
    for b in blocks:
        max_x = max(max_x, b["bbox"][2])
        max_y = max(max_y, b["bbox"][3])
        for w in b["words"]:
            max_x = max(max_x, w["x1"])
            max_y = max(max_y, w["y1"])

    margin = 1.01
    pw = max_x * margin
    ph = max_y * margin

    for b in blocks:
        bx0, by0, bx1, by1 = b["bbox"]
        b["bbox"] = (
            round(bx0 / pw, 8),
            round(1.0 - by1 / ph, 8),   # flip: old top → new bottom
            round(bx1 / pw, 8),
            round(1.0 - by0 / ph, 8),   # flip: old bottom → new top
        )
        for w in b["words"]:
            old_top = w["y0"]     # DocBank y0 = visual top
            old_bot = w["y1"]     # DocBank y1 = visual bottom
            w["x0"] = round(w["x0"] / pw, 8)
            w["y0"] = round(1.0 - old_bot / ph, 8)  # PDF bottom
            w["x1"] = round(w["x1"] / pw, 8)
            w["y1"] = round(1.0 - old_top / ph, 8)  # PDF top

    # Store for reference
    blocks[0].setdefault("_page_w", pw)
    blocks[0].setdefault("_page_h", ph)


# ─────────────────────────────────────────────────────────
# Flatten & shuffle
# ─────────────────────────────────────────────────────────

def flatten_blocks(blocks: List[dict]) -> List[dict]:
    """Flatten block list into a single word list (preserving block-internal order)."""
    words: List[dict] = []
    for b in blocks:
        words.extend(b["words"])
    return words


def shuffle_blocks(blocks: List[dict], seed: int = 42) -> List[dict]:
    """Return a shuffled copy of the block list."""
    shuffled = list(blocks)
    random.seed(seed)
    random.shuffle(shuffled)
    return shuffled


# ─────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────

def score_kendall_tau(sorted_words: List[dict]) -> Dict[str, float]:
    """
    Compute Kendall's Tau between the predicted GT_ID sequence and the
    ideal ascending sequence.

    Returns dict with tau, p_value, n_blocks, n_words.
    """
    pred = [w["gt_id"] for w in sorted_words]
    ideal = sorted(pred)   # [0, 0, 0, 1, 1, 2, 2, 2, ...] ascending

    tau, p = kendalltau(ideal, pred, variant="b")

    unique_blocks = len(set(pred))
    return {
        "tau": tau,
        "p_value": p,
        "n_blocks": unique_blocks,
        "n_words": len(pred),
    }


def count_inversions(sorted_words: List[dict]) -> Tuple[int, int]:
    """
    Count block-level inversions in the sorted output.

    An inversion occurs when a word with a higher gt_id appears before
    a word with a lower gt_id.  We only count transitions between
    different blocks.
    """
    pred = [w["gt_id"] for w in sorted_words]
    inversions = 0
    total_pairs = 0
    n = len(pred)
    # Count inversions (O(n²) worst-case but readable; DocBank pages are ~500 tokens)
    for i in range(n):
        for j in range(i + 1, n):
            if pred[i] == pred[j]:
                continue
            total_pairs += 1
            if pred[i] > pred[j]:
                inversions += 1
    return inversions, total_pairs


# ─────────────────────────────────────────────────────────
# Single-document evaluation
# ─────────────────────────────────────────────────────────

def evaluate_single(
    txt_path: str,
    seed: int = 42,
) -> Dict[str, Any]:
    """
    Run the full evaluation pipeline on one DocBank .txt file.

    Returns a dict with scores and metadata.
    """
    # 1. Load & parse
    tokens = parse_docbank_txt(txt_path)
    if len(tokens) < 3:
        return {"error": "Too few tokens", "path": txt_path}

    # 2. Group into blocks
    blocks = group_tokens_into_blocks(tokens)

    # 3. Filter noise & assign GT_IDs
    n_blocks = assign_gt_ids(blocks)
    if n_blocks < 2:
        return {"error": "Too few blocks after filtering", "path": txt_path}

    # 4. Normalise coordinates
    normalise_blocks(blocks)

    # 5. Shuffle blocks
    shuffled = shuffle_blocks(blocks, seed=seed)

    # 6. Flatten to words
    words = flatten_blocks(shuffled)
    if len(words) < 3:
        return {"error": "Too few words", "path": txt_path}

    # 7. Run XY-Cut (both implementations if available)
    results: Dict[str, Any] = {
        "path": os.path.basename(txt_path),
        "n_blocks": n_blocks,
        "n_words": len(words),
        "n_tokens_raw": len(tokens),
        "seed": seed,
    }

    # Enhanced sorter (with isolation detection)
    if SORTER_CLASS is not None:
        sorter = SORTER_CLASS()
        sorted_words = sorter.sort(words)
        results["xycut_plus"] = score_kendall_tau(sorted_words)
        if sorter.discarded_noise:
            results["xycut_plus"]["discarded"] = len(sorter.discarded_noise)

    return results


# ═══════════════════════════════════════════════════════════
# End-to-end evaluation (real PDF → parser → XY-Cut → score)
# ═══════════════════════════════════════════════════════════

def _compute_iou(a: dict, b: dict) -> float:
    """Intersection over Union for two bbox dicts with x0/y0/x1/y1 keys."""
    ix0 = max(a["x0"], b["x0"])
    iy0 = max(a["y0"], b["y0"])
    ix1 = min(a["x1"], b["x1"])
    iy1 = min(a["y1"], b["y1"])
    if ix0 >= ix1 or iy0 >= iy1:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    area_a = (a["x1"] - a["x0"]) * (a["y1"] - a["y0"])
    area_b = (b["x1"] - b["x0"]) * (b["y1"] - b["y0"])
    return inter / (area_a + area_b - inter) if (area_a + area_b - inter) > 0 else 0.0


def _normalize_tokens_to_pdf(
    tokens: List[dict],
    parser_words: List[dict],
) -> List[dict]:
    """
    Convert DocBank tokens (image coords, Y-down) to the same normalised
    coordinate space as parser words.

    Strategy: normalise BOTH sources relative to their own text extent
    (min/max of all tokens or words).  This aligns the two coordinate
    systems even though one comes from PDF points and the other from
    rendered image pixels.

    DocBank Y is flipped during this process so the output uses the
    PDF convention (Y increases upward).
    """
    if not tokens:
        return []

    # --- DocBank extent (image coords, Y-down) ---
    gt_x0 = min(t["x0"] for t in tokens)
    gt_x1 = max(t["x1"] for t in tokens)
    gt_y0 = min(t["y0"] for t in tokens)  # visual top
    gt_y1 = max(t["y1"] for t in tokens)  # visual bottom
    gt_w = gt_x1 - gt_x0 or 1.0
    gt_h = gt_y1 - gt_y0 or 1.0

    # --- Parser extent (PDF coords, Y-up) ---
    pw_min_x = min(w["x0"] for w in parser_words)
    pw_max_x = max(w["x1"] for w in parser_words)
    pw_min_y = min(w["y0"] for w in parser_words)  # PDF bottom
    pw_max_y = max(w["y1"] for w in parser_words)  # PDF top
    pw_w = pw_max_x - pw_min_x or 1.0
    pw_h = pw_max_y - pw_min_y or 1.0

    out: List[dict] = []
    for t in tokens:
        # DocBank → relative [0,1] within text extent
        rx = (t["x0"] - gt_x0) / gt_w
        ry = (t["y0"] - gt_y0) / gt_h  # 0 = visual top

        # Map relative → parser coordinate space
        out.append({
            "text": t["text"],
            "x0": round(pw_min_x + rx * pw_w, 8),
            "x1": round(pw_min_x + (t["x1"] - gt_x0) / gt_w * pw_w, 8),
            # Y-flip: DocBank ry=0 (top) → parser y1=max (top)
            "y0": round(pw_min_y + (1.0 - (t["y1"] - gt_y0) / gt_h) * pw_h, 8),
            "y1": round(pw_min_y + (1.0 - ry) * pw_h, 8),
            "label": t["label"],
        })
    return out


def _match_words_to_gt(
    parser_words: List[dict],
    gt_tokens: List[dict],
) -> List[int]:
    """
    Match each parser word to a ground-truth token.

    Stage 1 — exact text match + highest IoU (preferred).
    Stage 2 — for remaining unmatched parser words, match by IoU alone.

    Returns a list `gt_ids` of the same length as parser_words, where
    gt_ids[i] is the position (in the GT file) of the best-matching
    ground-truth token, or -1 if no match found.
    """
    pool: List[Tuple[int, dict]] = list(enumerate(gt_tokens))
    gt_ids: List[int] = [-1] * len(parser_words)

    # Stage 1: exact text match + IoU
    for pi, pw in enumerate(parser_words):
        pw_text = pw["text"].strip().lower()
        best_idx = -1
        best_iou = 0.15   # minimum IoU threshold

        for gi, gt in pool:
            if gt["text"].strip().lower() != pw_text:
                continue
            iou = _compute_iou(pw, gt)
            if iou > best_iou:
                best_iou = iou
                best_idx = gi

        if best_idx >= 0:
            gt_ids[pi] = best_idx
            pool = [(gi, gt) for gi, gt in pool if gi != best_idx]

    # Stage 2: for unmatched parser words, match by IoU alone
    for pi, pw in enumerate(parser_words):
        if gt_ids[pi] >= 0:
            continue
        best_idx = -1
        best_iou = 0.25   # stricter threshold for text-less matching

        for gi, gt in pool:
            iou = _compute_iou(pw, gt)
            if iou > best_iou:
                best_iou = iou
                best_idx = gi

        if best_idx >= 0:
            gt_ids[pi] = best_idx
            pool = [(gi, gt) for gi, gt in pool if gi != best_idx]

    return gt_ids


def evaluate_end_to_end(
    txt_path: str,
    pdf_path: str,
    page_number: int,
    seed: int = 42,
) -> Dict[str, Any]:
    """
    Full end-to-end evaluation:
      1. Parse the real PDF with pdf_parser  →  words from parser
      2. Parse the .txt  →  ground truth token order
      3. Match parser words ↔ GT tokens (text + IoU)
      4. Shuffle parser words, run XY-Cut, score with Kendall's Tau

    Args:
        txt_path: Path to the DocBank .txt annotation file.
        pdf_path: Path to the matching PDF.
        page_number: Which page in the PDF corresponds to this .txt (1-indexed).

    Returns:
        Dict with tau scores, match rate, and metadata.
    """
    from pdf_parser import parse_pdf

    # 1. Parse the PDF
    all_pages = parse_pdf(pdf_path, text_min_words=0)
    if page_number > len(all_pages):
        return {"error": f"PDF has only {len(all_pages)} pages, wanted page {page_number}"}
    page = all_pages[page_number - 1]
    parser_words = page["words"]

    if len(parser_words) < 3:
        return {"error": f"Parser extracted only {len(parser_words)} words"}

    # 2. Parse ground truth
    gt_tokens_raw = parse_docbank_txt(txt_path)
    # Filter noise labels from GT
    gt_tokens_raw = [t for t in gt_tokens_raw if t["label"] not in NOISE_LABELS]
    if len(gt_tokens_raw) < 3:
        return {"error": f"Too few GT tokens after filtering: {len(gt_tokens_raw)}"}

    # Normalise GT coords to match parser's coordinate space
    gt_tokens = _normalize_tokens_to_pdf(gt_tokens_raw, parser_words)

    # 3. Match parser words → GT positions
    gt_ids = _match_words_to_gt(parser_words, gt_tokens)
    matched_count = sum(1 for g in gt_ids if g >= 0)
    match_rate = matched_count / len(parser_words) if parser_words else 0

    # Keep only matched words, inject _gt_id directly onto the word dict
    matched_words: List[dict] = []
    for w, g in zip(parser_words, gt_ids):
        if g >= 0:
            w["_gt_id"] = g
            matched_words.append(w)

    if len(matched_words) < 3:
        return {"error": f"Only {len(matched_words)} words matched ({match_rate:.1%})"}

    # 4. Shuffle (simulate unordered parser output)
    shuffled = shuffle_blocks(matched_words, seed=seed)  # works on word list too

    # 5. Run XY-Cut
    results: Dict[str, Any] = {
        "path": os.path.basename(txt_path),
        "pdf": os.path.basename(pdf_path),
        "parser_words_total": len(parser_words),
        "matched_words": matched_count,
        "match_rate": round(match_rate, 4),
        "gt_tokens": len(gt_tokens),
        "seed": seed,
    }

    # XY-Cut++ (enhanced)
    if SORTER_CLASS is not None:
        sorter = SORTER_CLASS()
        sorted_plus = sorter.sort(list(shuffled))  # copy to avoid mutation issues
        sorted_gt_plus = [w.get("_gt_id", -1) for w in sorted_plus]
        ideal = sorted(sorted_gt_plus)
        tau, p = kendalltau(ideal, sorted_gt_plus, variant="b")
        results["xycut_plus"] = {"tau": tau, "p_value": p, "n_words": len(sorted_gt_plus)}
        if sorter.discarded_noise:
            results["xycut_plus"]["discarded"] = len(sorter.discarded_noise)

    # Clean up injected keys
    for w in parser_words:
        w.pop("_gt_id", None)

    return results


# ─────────────────────────────────────────────────────────
# Batch evaluation
# ─────────────────────────────────────────────────────────

def evaluate_dataset(
    data_dir: str,
    max_files: int = 100,
    seed: int = 42,
    verbose: bool = True,
) -> Dict[str, Any]:
    """
    Evaluate all .txt files in a directory.

    Returns aggregate statistics.
    """
    txt_files = sorted([
        f for f in os.listdir(data_dir) if f.endswith(".txt")
    ])[:max_files]

    if not txt_files:
        print(f"No .txt files found in {data_dir}")
        return {}

    tau_scores_plus: List[float] = []
    perfect_count_plus = 0
    errors = 0

    for i, fname in enumerate(txt_files):
        fpath = os.path.join(data_dir, fname)
        result = evaluate_single(fpath, seed=seed)

        if "error" in result:
            errors += 1
            if verbose:
                print(f"  [{i+1:3d}/{len(txt_files)}] {fname[:50]:50s}  SKIP: {result['error']}")
            continue

        t_plus = result.get("xycut_plus", {}).get("tau")
        if t_plus is not None:
            tau_scores_plus.append(t_plus)
            if t_plus >= 0.9999:
                perfect_count_plus += 1

        if verbose:
            n_blocks = result["n_blocks"]
            status_plus = f"τ={t_plus:.4f}" if t_plus is not None else "N/A"
            print(f"  [{i+1:3d}/{len(txt_files)}] {fname[:45]:45s}  "
                  f"blocks={n_blocks:3d}  {status_plus}")

    # Aggregate
    summary = {
        "total_files": len(txt_files),
        "evaluated": len(tau_scores_plus),
        "errors": errors,
    }
    if tau_scores_plus:
        summary["xycut_plus"] = {
            "mean_tau": sum(tau_scores_plus) / len(tau_scores_plus),
            "min_tau": min(tau_scores_plus),
            "max_tau": max(tau_scores_plus),
            "perfect": perfect_count_plus,
            "perfect_pct": 100.0 * perfect_count_plus / len(tau_scores_plus),
            "n": len(tau_scores_plus),
        }

    return summary


# ─────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────

def _run_e2e_batch(args):
    """End-to-end evaluation: parse real PDFs with pdf_parser, then XY-Cut, then score."""
    import re

    data_dir = args.data_dir
    txt_files = sorted([
        f for f in os.listdir(data_dir) if f.endswith(".txt")
    ])[:args.max_files]

    # Build mapping: base name → list of (page_number, txt_file)
    pdf_map: Dict[str, List[Tuple[int, str]]] = {}
    for tf in txt_files:
        m = re.search(r'_(\d+)\.txt$', tf)
        if not m:
            continue
        page_num = int(m.group(1))
        base = tf[:m.start()]
        pdf_map.setdefault(base, []).append((page_num, tf))

    tau_scores_plus: List[float] = []
    perfect_plus = 0
    errors = 0
    total = 0

    for base, entries in pdf_map.items():
        pdf_path = os.path.join(data_dir, base + "_black.pdf")
        if not os.path.exists(pdf_path):
            pdf_path = os.path.join(data_dir, base + "_color.pdf")
        if not os.path.exists(pdf_path):
            if args.verbose:
                print(f"  SKIP {base}: PDF not found")
            errors += 1
            continue

        for page_num, txt_file in entries:
            total += 1
            txt_path = os.path.join(data_dir, txt_file)
            result = evaluate_end_to_end(
                txt_path=txt_path,
                pdf_path=pdf_path,
                page_number=page_num,
                seed=args.seed,
            )

            if "error" in result:
                errors += 1
                if args.verbose:
                    print(f"  [{total:3d}] {txt_file[:50]:50s}  SKIP: {result['error']}")
                continue

            t_plus = result.get("xycut_plus", {}).get("tau")
            if t_plus is not None:
                tau_scores_plus.append(t_plus)
                if t_plus >= 0.9999:
                    perfect_plus += 1

            if args.verbose:
                mr = result.get("match_rate", 0)
                nw = result.get("matched_words", 0)
                s_plus = f"τ={t_plus:.4f}" if t_plus is not None else "N/A"
                print(f"  [{total:3d}] {txt_file[:40]:40s}  "
                      f"match={mr:.1%} n={nw:3d}  τ={s_plus}")

    print("\n" + "=" * 60)
    print("END-TO-END SUMMARY (real PDF → parser → XY-Cut → Kendall's Tau)")
    print("=" * 60)
    if tau_scores_plus:
        print(f"  XY-Cut++ (isolation):  mean τ={sum(tau_scores_plus)/len(tau_scores_plus):.4f}  "
              f"perfect={perfect_plus}/{len(tau_scores_plus)} "
              f"({100.0*perfect_plus/len(tau_scores_plus):.1f}%)  "
              f"min={min(tau_scores_plus):.4f}")
    evaluated = len(tau_scores_plus)
    print(f"  Files: {evaluated} evaluated, {errors} errors/skipped, {total} total pages")


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="DocBank XY-Cut Evaluation")
    ap.add_argument("--data-dir", default="/tmp/DocBank/DocBank_samples/DocBank_samples",
                    help="Directory containing DocBank .txt files")
    ap.add_argument("--max-files", type=int, default=100,
                    help="Max files to evaluate (default 100)")
    ap.add_argument("--seed", type=int, default=42,
                    help="Random seed for shuffle (default 42)")
    ap.add_argument("--single", type=str, default=None,
                    help="Evaluate a single .txt file instead of batch")
    ap.add_argument("--verbose", action="store_true", default=True,
                    help="Print per-file scores")
    ap.add_argument("--quiet", dest="verbose", action="store_false",
                    help="Only print summary")
    ap.add_argument("--end-to-end", action="store_true",
                    help="Run end-to-end: parse real PDFs (not just .txt annotations)")
    args = ap.parse_args()

    print(f"Sorter: {SORTER_NAME}")
    print()

    if args.end_to_end:
        # ── End-to-end mode: parse real PDFs ──
        _run_e2e_batch(args)
    elif args.single:
        path = args.single
        if not os.path.isabs(path):
            path = os.path.join(args.data_dir, path)
        result = evaluate_single(path, seed=args.seed)
        import json
        print(json.dumps(result, indent=2, default=str))
    else:
        summary = evaluate_dataset(
            args.data_dir,
            max_files=args.max_files,
            seed=args.seed,
            verbose=args.verbose,
        )

        print("\n" + "=" * 60)
        print("AGGREGATE SUMMARY")
        print("=" * 60)
        if "xycut_plus" in summary:
            s = summary["xycut_plus"]
            print(f"  XY-Cut++ (isolation):  mean τ={s['mean_tau']:.4f}  "
                  f"perfect={s['perfect']}/{s['n']} ({s['perfect_pct']:.1f}%)  "
                  f"min={s['min_tau']:.4f}")
        print(f"  Files: {summary['evaluated']} evaluated, {summary['errors']} skipped")
