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
    """Load cache from path, or global CACHE_PATH if path is None.
    Old format_version 2 caches are treated as empty (format changed)."""
    p = Path(path) if path else CACHE_PATH
    if not p.exists():
        return _empty_cache()
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return _empty_cache()
    if data.get("format_version") != 3:
        return _empty_cache()
    return data


def save_cache(cache: dict, path: str | None = None) -> None:
    p = Path(path) if path else CACHE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _empty_cache() -> dict:
    return {"format_version": 3}


# ── Helpers ─────────────────────────────────────────────────────────

def _ensure_file(cache: dict, file_path: str) -> dict:
    """Return cache[file_path], creating flat phrases/sentences structure if missing."""
    if file_path not in cache:
        cache[file_path] = {"phrases": [], "sentences": []}
    entry = cache[file_path]
    # Migrate old format: if entry has "single"/"dual" keys, convert to flat
    if "single" in entry or "dual" in entry:
        phrases = []
        sentences = []
        for gk in ("single", "dual"):
            g = entry.get(gk, {})
            if isinstance(g, dict):
                phrases.extend(g.get("phrases", []))
                sentences.extend(g.get("sentences", []))
        cache[file_path] = {"phrases": phrases, "sentences": sentences}
        return cache[file_path]
    entry.setdefault("phrases", [])
    entry.setdefault("sentences", [])
    return entry


# ── Phrase cache ────────────────────────────────────────────────────

def lookup_phrase(cache: dict, file_path: str, src: str) -> str | None:
    """Exact match on src. Returns tgt or None."""
    fe = _ensure_file(cache, file_path)
    for p in fe["phrases"]:
        if p["src"] == src:
            return p["tgt"]
    return None


def add_phrase_entry(cache: dict, file_path: str, src: str, tgt: str) -> None:
    """Add or replace a phrase cache entry (keyed by src)."""
    fe = _ensure_file(cache, file_path)
    for p in fe["phrases"]:
        if p["src"] == src:
            p["tgt"] = tgt
            return
    fe["phrases"].append({"src": src, "tgt": tgt})


# ── Sentence cache ──────────────────────────────────────────────────

def add_sentence_entry(cache: dict, file_path: str, entry: dict) -> None:
    """Add or replace a sentence cache entry (keyed by idx range).
    If entry has no sub-sentences, falls back to src string comparison."""
    fe = _ensure_file(cache, file_path)
    subs = entry.get("sentences", [])
    if subs:
        fs = subs[0]
        ls = subs[-1]
        for i, existing in enumerate(fe["sentences"]):
            existing_subs = existing.get("sentences", [])
            if existing_subs:
                efs = existing_subs[0]
                els = existing_subs[-1]
                if (fs["start_idx"] == efs["start_idx"] and
                        ls["end_idx"] == els["end_idx"]):
                    fe["sentences"][i] = entry
                    return
    else:
        for i, existing in enumerate(fe["sentences"]):
            if existing["src"] == entry["src"]:
                fe["sentences"][i] = entry
                return
    fe["sentences"].append(entry)


def _sub_first(sub: dict) -> int:
    return sub["start_idx"]


def find_overlapping_entries(cache: dict, file_path: str,
                              start_idx: int, end_idx: int) -> list[int]:
    """Return indices of sentence entries whose sub-sentence idx ranges
    overlap [start_idx, end_idx]. Sorted by first-sub start_idx."""
    fe = _ensure_file(cache, file_path)
    results = []
    for idx, entry in enumerate(fe["sentences"]):
        for sub in entry.get("sentences", []):
            if sub["start_idx"] <= end_idx and sub["end_idx"] >= start_idx:
                results.append(idx)
                break
    results.sort(key=lambda i: _sub_first(fe["sentences"][i]["sentences"][0]))
    return results


def find_containing_entries(cache: dict, file_path: str,
                             start_idx: int, end_idx: int) -> list[int]:
    """Return indices of sentence entries whose total idx range
    (first sub start to last sub end) CONTAINS [start_idx, end_idx].
    Sorted by first-sub start_idx."""
    fe = _ensure_file(cache, file_path)
    results = []
    for idx, entry in enumerate(fe["sentences"]):
        subs = entry.get("sentences", [])
        if not subs:
            continue
        if subs[0]["start_idx"] <= start_idx and end_idx <= subs[-1]["end_idx"]:
            results.append(idx)
    results.sort(key=lambda i: _sub_first(fe["sentences"][i]["sentences"][0]))
    return results


def merge_entries(cache: dict, file_path: str, indices: list[int]) -> dict:
    """Merge sentence entries at given indices into one. Deletes originals.
    Returns the merged entry dict."""
    fe = _ensure_file(cache, file_path)
    entries = [fe["sentences"][i] for i in indices]
    entries.sort(key=lambda e: _sub_first(e["sentences"][0]))

    all_subs = []
    for e in entries:
        all_subs.extend(e.get("sentences", []))
    all_subs.sort(key=_sub_first)

    # Deduplicate by start_idx, keep non-empty tgt
    seen: dict[int, int] = {}
    deduped: list[dict] = []
    for sub in all_subs:
        key = sub["start_idx"]
        if key not in seen:
            seen[key] = len(deduped)
            deduped.append(sub)
        elif sub["tgt"]:
            deduped[seen[key]] = sub
    all_subs = deduped

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
        fe["sentences"].pop(i)
    fe["sentences"].append(merged)
    return merged


def find_mergeable_fragments(cache: dict, file_path: str) -> tuple | None:
    """Find one mergeable fragment pair by idx adjacency.
    Returns (idx_a, idx_b, direction) or None. direction='append' means
    B's sentences go after A's."""
    fe = _ensure_file(cache, file_path)
    entries = fe.get("sentences", [])

    for i, entry in enumerate(entries):
        subs = entry.get("sentences", [])
        if not subs:
            continue

        if entry.get("head_fragment"):
            first_sub_start = subs[0]["start_idx"]
            for j, other in enumerate(entries):
                if i == j:
                    continue
                other_subs = other.get("sentences", [])
                if other_subs and other_subs[-1]["end_idx"] + 1 == first_sub_start:
                    return (j, i, "append")

        if entry.get("tail_fragment"):
            last_sub_end = subs[-1]["end_idx"]
            for j, other in enumerate(entries):
                if i == j:
                    continue
                other_subs = other.get("sentences", [])
                if other_subs and last_sub_end + 1 == other_subs[0]["start_idx"]:
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
        phrase_count = len(entry.get("phrases", []))
        sent_count = len(entry.get("sentences", []))
        summaries.append({
            "file_path": fp,
            "filename": Path(fp).name,
            "entry_count": phrase_count + sent_count,
        })
    summaries.sort(key=lambda x: x["filename"].lower())
    return summaries


# ── Path utilities ─────────────────────────────────────────────────

def auto_generate_per_pdf_path(pdf_path: str, suffix: str) -> str:
    """Generate default per-PDF path in DATA_DIR with timestamp."""
    os.makedirs(DATA_DIR, exist_ok=True)
    p = Path(pdf_path)
    ts = datetime.now().strftime("%y%m%d%H%M%S")
    return str(Path(DATA_DIR) / f"{p.stem}{suffix}_{ts}.json")
