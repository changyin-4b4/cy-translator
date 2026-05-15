import json
import os
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CACHE_PATH = Path(BASE_DIR) / "cache_store.json"
DATA_DIR = os.path.join(BASE_DIR, "data")


# ── Load / save ─────────────────────────────────────────────────────

def load_cache(path: str | None = None) -> dict:
    """Load cache from path, or global CACHE_PATH if path is None."""
    p = Path(path) if path else CACHE_PATH
    if not p.exists():
        return _empty_cache()
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return _empty_cache()


def save_cache(cache: dict, path: str | None = None) -> None:
    p = Path(path) if path else CACHE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _empty_cache() -> dict:
    return {"format_version": 2}


# ── Helpers ─────────────────────────────────────────────────────────

def _ensure_file(cache: dict, file_path: str) -> dict:
    if file_path not in cache:
        cache[file_path] = {
            "single": {"phrases": [], "sentences": []},
            "dual": {"phrases": [], "sentences": []},
        }
    entry = cache[file_path]
    if isinstance(entry, list):
        cache[file_path] = {
            "single": {"phrases": [], "sentences": []},
            "dual": {"phrases": [], "sentences": []},
        }
        return cache[file_path]
    for group_key in ("single", "dual"):
        group = entry.setdefault(group_key, {"phrases": [], "sentences": []})
        if not isinstance(group, dict):
            entry[group_key] = {"phrases": [], "sentences": []}
            group = entry[group_key]
        group.setdefault("phrases", [])
        group.setdefault("sentences", [])
    return entry


def _get_group(cache: dict, file_path: str, is_dual: bool) -> dict:
    """Return the cache group dict ('single' or 'dual') for the given file."""
    fe = _ensure_file(cache, file_path)
    key = "dual" if is_dual else "single"
    return fe[key]


def _sub_first(sub: dict) -> tuple:
    return (sub["start_page"], sub["start_y_pct"])


def _sub_last(sub: dict) -> tuple:
    return (sub["end_page"], sub["end_y_pct"])


def _coord_lt(ap, ay, bp, by) -> bool:
    return (ap, ay) < (bp, by)


def _coord_le(ap, ay, bp, by) -> bool:
    return (ap, ay) <= (bp, by)


COORD_TOLERANCE = 0.001


def _coord_eq(ap, ax, ay, bp, bx, by) -> bool:
    return ap == bp and abs(ax - bx) < COORD_TOLERANCE and abs(ay - by) < COORD_TOLERANCE


def coord_le_tolerant(ap, ay, ax, bp, by, bx) -> bool:
    """Return True if (ap, ay, ax) <= (bp, by, bx) with tolerance.
    Priority: page > y_pct > x_pct. Values within COORD_TOLERANCE are equal."""
    if ap < bp:
        return True
    if ap > bp:
        return False
    if ay < by - COORD_TOLERANCE:
        return True
    if ay > by + COORD_TOLERANCE:
        return False
    return ax <= bx + COORD_TOLERANCE


def _ranges_overlap(s1p, s1y, e1p, e1y, s2p, s2y, e2p, e2y) -> bool:
    return not (
        _coord_lt(e1p, e1y, s2p, s2y) or
        _coord_lt(e2p, e2y, s1p, s1y)
    )


# ── Phrase cache ────────────────────────────────────────────────────

def lookup_phrase(cache: dict, file_path: str, src: str,
                  is_dual: bool = False) -> str | None:
    """Exact match on src. Returns tgt or None."""
    group = _get_group(cache, file_path, is_dual)
    for p in group["phrases"]:
        if p["src"] == src:
            return p["tgt"]
    return None


def add_phrase_entry(cache: dict, file_path: str, src: str, tgt: str,
                     is_dual: bool = False) -> None:
    """Add or replace a phrase cache entry (keyed by src)."""
    group = _get_group(cache, file_path, is_dual)
    for p in group["phrases"]:
        if p["src"] == src:
            p["tgt"] = tgt
            return
    group["phrases"].append({"src": src, "tgt": tgt})


# ── Sentence cache ──────────────────────────────────────────────────

def add_sentence_entry(cache: dict, file_path: str, entry: dict,
                       is_dual: bool = False) -> None:
    """Add or replace a sentence cache entry (keyed by coordinate range).
    If entry has no sub-sentences, falls back to src string comparison."""
    group = _get_group(cache, file_path, is_dual)
    subs = entry.get("sentences", [])
    if subs:
        fs = subs[0]
        ls = subs[-1]
        for i, existing in enumerate(group["sentences"]):
            existing_subs = existing.get("sentences", [])
            if existing_subs:
                efs = existing_subs[0]
                els = existing_subs[-1]
                if (
                    coord_le_tolerant(
                        fs["start_page"], fs["start_y_pct"], fs["start_x_pct"],
                        efs["start_page"], efs["start_y_pct"], efs["start_x_pct"],
                    ) and coord_le_tolerant(
                        efs["start_page"], efs["start_y_pct"], efs["start_x_pct"],
                        fs["start_page"], fs["start_y_pct"], fs["start_x_pct"],
                    ) and coord_le_tolerant(
                        ls["end_page"], ls["end_y_pct"], ls["end_x_pct"],
                        els["end_page"], els["end_y_pct"], els["end_x_pct"],
                    ) and coord_le_tolerant(
                        els["end_page"], els["end_y_pct"], els["end_x_pct"],
                        ls["end_page"], ls["end_y_pct"], ls["end_x_pct"],
                    )
                ):
                    group["sentences"][i] = entry
                    return
    else:
        for i, existing in enumerate(group["sentences"]):
            if existing["src"] == entry["src"]:
                group["sentences"][i] = entry
                return
    group["sentences"].append(entry)


def find_overlapping_entries(cache: dict, file_path: str,
                              sp: int, sy: float, ep: int, ey: float,
                              is_dual: bool = False) -> list[int]:
    """Return indices of sentence entries whose sub-sentences overlap the
    document range from (sp, sy) to (ep, ey). Sorted by first-sub coordinate."""
    group = _get_group(cache, file_path, is_dual)
    results = []
    for idx, entry in enumerate(group["sentences"]):
        for sub in entry.get("sentences", []):
            if _ranges_overlap(
                sub["start_page"], sub["start_y_pct"],
                sub["end_page"], sub["end_y_pct"],
                sp, sy, ep, ey,
            ):
                results.append(idx)
                break
    results.sort(key=lambda i: _sub_first(group["sentences"][i]["sentences"][0]))
    return results


def find_containing_entries(cache: dict, file_path: str,
                              sp: int, sy: float, sx: float,
                              ep: int, ey: float, ex: float,
                              is_dual: bool = False) -> list[int]:
    """Return indices of sentence entries whose total range (first sub start
    to last sub end) CONTAINS the query range. Query is a subset of entry.
    Coordinate order: page > y_pct > x_pct, with COORD_TOLERANCE."""
    group = _get_group(cache, file_path, is_dual)
    results = []
    for idx, entry in enumerate(group["sentences"]):
        subs = entry.get("sentences", [])
        if not subs:
            continue
        fs = subs[0]
        ls = subs[-1]
        # entry start <= query start
        if not coord_le_tolerant(
            fs["start_page"], fs["start_y_pct"], fs["start_x_pct"],
            sp, sy, sx,
        ):
            continue
        # query end <= entry end
        if not coord_le_tolerant(
            ep, ey, ex,
            ls["end_page"], ls["end_y_pct"], ls["end_x_pct"],
        ):
            continue
        results.append(idx)
    results.sort(key=lambda i: _sub_first(group["sentences"][i]["sentences"][0]))
    return results


def merge_entries(cache: dict, file_path: str, indices: list[int],
                  is_dual: bool = False) -> dict:
    """Merge sentence entries at given indices into one. Deletes originals.
    Returns the merged entry dict."""
    group = _get_group(cache, file_path, is_dual)
    entries = [group["sentences"][i] for i in indices]
    entries.sort(key=lambda e: _sub_first(e["sentences"][0]))

    all_subs = []
    for e in entries:
        all_subs.extend(e.get("sentences", []))
    all_subs.sort(key=_sub_first)

    if all_subs:
        all_subs[0]["is_head_fragment"] = (
            entries[0].get("head_fragment", False) or
            entries[0]["sentences"][0].get("is_head_fragment", False)
        )
        all_subs[-1]["is_tail_fragment"] = (
            entries[-1].get("tail_fragment", False) or
            entries[-1]["sentences"][-1].get("is_tail_fragment", False)
        )

    merged_src = " ".join(s["src"] for s in all_subs)
    merged_tgt = "".join(s["tgt"] for s in all_subs if s["tgt"])

    merged = {
        "src": merged_src,
        "tgt": merged_tgt,
        "head_fragment": all_subs[0]["is_head_fragment"] if all_subs else False,
        "tail_fragment": all_subs[-1]["is_tail_fragment"] if all_subs else False,
        "sentences": all_subs,
    }

    for i in sorted(indices, reverse=True):
        group["sentences"].pop(i)
    group["sentences"].append(merged)
    return merged


def find_mergeable_fragments(cache: dict, file_path: str,
                             is_dual: bool = False) -> tuple | None:
    """Find one mergeable fragment pair. Returns (idx_a, idx_b, direction) or None.
    direction='append' means B's sentences go after A's. Returns None if no pair found."""
    group = _get_group(cache, file_path, is_dual)
    entries = group.get("sentences", [])

    for i, entry in enumerate(entries):
        subs = entry.get("sentences", [])
        if not subs:
            continue

        if entry.get("head_fragment"):
            first_sub = subs[0]
            for j, other in enumerate(entries):
                if i == j:
                    continue
                for other_sub in other.get("sentences", []):
                    if _coord_eq(
                        other_sub["end_page"], other_sub["end_x_pct"], other_sub["end_y_pct"],
                        first_sub["end_page"], first_sub["end_x_pct"], first_sub["end_y_pct"],
                    ):
                        return (j, i, "append")

        if entry.get("tail_fragment"):
            last_sub = subs[-1]
            for j, other in enumerate(entries):
                if i == j:
                    continue
                for other_sub in other.get("sentences", []):
                    if _coord_eq(
                        other_sub["start_page"], other_sub["start_x_pct"], other_sub["start_y_pct"],
                        last_sub["start_page"], last_sub["start_x_pct"], last_sub["start_y_pct"],
                    ):
                        return (i, j, "append")

    return None


# ── Management ──────────────────────────────────────────────────────

def remove_file(cache: dict, file_path: str) -> None:
    cache.pop(file_path, None)


def get_cache_summary(cache: dict) -> list[dict]:
    summaries = []
    for fp, entry in cache.items():
        if fp == "format_version":
            continue
        if not isinstance(entry, dict):
            continue
        phrase_count = 0
        sent_count = 0
        for group_key in ("single", "dual"):
            group = entry.get(group_key, {}) if isinstance(entry, dict) else {}
            if isinstance(group, dict):
                phrase_count += len(group.get("phrases", []))
                sent_count += len(group.get("sentences", []))
        summaries.append({
            "file_path": fp,
            "filename": Path(fp).name,
            "entry_count": phrase_count + sent_count,
        })
    summaries.sort(key=lambda x: x["filename"].lower())
    return summaries


# ── Path utilities ─────────────────────────────────────────────────

def auto_generate_per_pdf_path(pdf_path: str, suffix: str) -> str:
    """Generate default per-PDF path in DATA_DIR with timestamp.
    Example: /path/doc.pdf + '_cache' → {DATA_DIR}/doc_cache_250115143022.json"""
    os.makedirs(DATA_DIR, exist_ok=True)
    p = Path(pdf_path)
    ts = datetime.now().strftime("%y%m%d%H%M%S")
    return str(Path(DATA_DIR) / f"{p.stem}{suffix}_{ts}.json")
