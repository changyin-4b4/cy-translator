import json
import os
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CONFIG_PATH = Path(BASE_DIR) / "config.json"

DEFAULTS = {
    "base_urls": {},
    "current_url": "",
    "current_model": "",
    "output_dirs": [],
    "current_output_dir": "",
    "append_files": [],
    "current_append_file": "",
    "prompt_files": [],
    "current_prompt_file": "",
    "output_mode": "new",
    "paste_clean_enabled": False,
    "mode_b_current_url": "",
    "mode_b_current_model": "",
    "mode_b_prompt": "",
    "mode_b_auto_translate": True,
    "window_size": "1920x1080",
    "auto_complete_enabled": False,
    "toc_collapsed": False,
    "pdf_history": [],
}


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        return dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)
    merged = dict(DEFAULTS)
    merged.update(data)
    return merged


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


# ── URL helpers ───────────────────────────────────────────────────────

def add_base_url(config: dict, url: str) -> None:
    url = url.strip()
    if not url:
        return
    if url not in config["base_urls"]:
        config["base_urls"][url] = {"key": "", "models": []}


def remove_base_url(config: dict, url: str) -> None:
    config["base_urls"].pop(url, None)
    if config["current_url"] == url:
        config["current_url"] = ""
        config["current_model"] = ""
    if config.get("mode_b_current_url") == url:
        config["mode_b_current_url"] = ""
        config["mode_b_current_model"] = ""


def set_url_key(config: dict, url: str, key: str) -> None:
    url = url.strip()
    if url and url in config["base_urls"]:
        config["base_urls"][url]["key"] = key


def get_url_key(config: dict, url: str) -> str:
    url = url.strip()
    return config.get("base_urls", {}).get(url, {}).get("key", "")


def get_url_models(config: dict, url: str) -> list:
    url = url.strip()
    return config.get("base_urls", {}).get(url, {}).get("models", [])


def add_model_to_url(config: dict, url: str, model: str) -> None:
    url, model = url.strip(), model.strip()
    if not url or not model:
        return
    if url not in config["base_urls"]:
        config["base_urls"][url] = {"key": "", "models": []}
    models = config["base_urls"][url]["models"]
    if model not in models:
        models.append(model)


def remove_model_from_url(config: dict, url: str, model: str) -> None:
    url, model = url.strip(), model.strip()
    if url in config.get("base_urls", {}):
        models = config["base_urls"][url].get("models", [])
        if model in models:
            models.remove(model)


def set_models_for_url(config: dict, url: str, models: list) -> None:
    url = url.strip()
    if not url:
        return
    if url not in config["base_urls"]:
        config["base_urls"][url] = {"key": "", "models": []}
    seen = set()
    deduped = []
    for m in models:
        if m and m not in seen:
            deduped.append(m)
            seen.add(m)
    config["base_urls"][url]["models"] = deduped


# ── Path list helpers ─────────────────────────────────────────────────

def _insert_at_front(lst: list, item: str) -> None:
    item = item.strip()
    if not item:
        return
    if item in lst:
        lst.remove(item)
    lst.insert(0, item)


def _remove_from_list(lst: list, item: str) -> None:
    item = item.strip()
    if item in lst:
        lst.remove(item)


def add_output_dir(config: dict, d: str) -> None:
    _insert_at_front(config.setdefault("output_dirs", []), d)


def remove_output_dir(config: dict, d: str) -> None:
    _remove_from_list(config.setdefault("output_dirs", []), d)


def add_append_file(config: dict, f: str) -> None:
    _insert_at_front(config.setdefault("append_files", []), f)


def remove_append_file(config: dict, f: str) -> None:
    _remove_from_list(config.setdefault("append_files", []), f)


def add_prompt_file(config: dict, f: str) -> None:
    _insert_at_front(config.setdefault("prompt_files", []), f)


def remove_prompt_file(config: dict, f: str) -> None:
    _remove_from_list(config.setdefault("prompt_files", []), f)


# ── PDF history helpers ──────────────────────────────────────────────

def get_or_create_pdf_history_entry(config: dict, pdf_path: str) -> dict:
    """Return the pdf_history entry for pdf_path, creating one if needed."""
    history: list = config.setdefault("pdf_history", [])
    for entry in history:
        if isinstance(entry, dict) and entry.get("path") == pdf_path:
            entry.setdefault("config", {"cache_file": None, "isolate_file": None, "layout_file": None, "note_file": None})
            return entry
    entry = {
        "path": pdf_path,
        "date": datetime.now().strftime("%Y-%m-%d-%H-%M-%S"),
        "config": {"cache_file": None, "isolate_file": None, "layout_file": None, "note_file": None},
    }
    history.insert(0, entry)
    return entry


def set_pdf_config_path(config: dict, pdf_path: str, key: str,
                        value: str | None) -> None:
    entry = get_or_create_pdf_history_entry(config, pdf_path)
    entry.setdefault("config", {})[key] = value
