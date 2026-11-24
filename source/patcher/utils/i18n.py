"""
RU / EN localization helpers for Antigravity Patcher CLI.

Persists the selected language under <AG_TOOLS_DIR>/config.json ("lang": "ru" | "en").
"""
from __future__ import annotations

import json
import locale
import os
from pathlib import Path

from patcher.utils.atomic import write_atomic

SUPPORTED_LANGS = ("en", "ru")


def _tools_dir() -> Path:
    env_dir = os.environ.get("AG_TOOLS_DIR", "").strip()
    return Path(env_dir) if env_dir else (Path.home() / ".antigravity_tools")


def _config_path() -> Path:
    return _tools_dir() / "config.json"


def _detect_system_lang() -> str:
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var, "").strip().lower()
        if val.startswith("ru"):
            return "ru"
    if os.name == "nt":
        try:
            import ctypes
            lang_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            # Primary language ID 0x19 = Russian
            if (lang_id & 0x3FF) == 0x19:
                return "ru"
        except Exception:
            pass
    try:
        loc = locale.getlocale()[0] or locale.getdefaultlocale()[0] or ""
        if str(loc).strip().lower().startswith("ru"):
            return "ru"
    except Exception:
        pass
    return "en"


def get_lang() -> str:
    """Return active UI language code ('ru' or 'en')."""
    env_lang = os.environ.get("AG_LANG", "").strip().lower()
    if env_lang in SUPPORTED_LANGS:
        return env_lang
    cfg_file = _config_path()
    if cfg_file.is_file():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(data, dict):
                saved = str(data.get("lang") or "").strip().lower()
                if saved in SUPPORTED_LANGS:
                    return saved
        except Exception:
            pass
    return _detect_system_lang()


def set_lang(lang: str) -> str:
    """Persist UI language preference ('ru' or 'en') to config.json."""
    normalized = str(lang or "").strip().lower()
    if normalized not in SUPPORTED_LANGS:
        raise ValueError(f"Unsupported language: {lang!r}")
    cfg_file = _config_path()
    data: dict = {}
    if cfg_file.is_file():
        try:
            raw = json.loads(cfg_file.read_text(encoding="utf-8", errors="ignore"))
            if isinstance(raw, dict):
                data = raw
        except Exception:
            pass
    data["lang"] = normalized
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(str(cfg_file), json.dumps(data, ensure_ascii=False, indent=2) + "\n", kill_on_lock=False)
    return normalized


def toggle_lang() -> str:
    """Switch active language between 'ru' and 'en' and persist it."""
    next_lang = "en" if get_lang() == "ru" else "ru"
    if os.environ.get("AG_LANG", "").strip():
        os.environ["AG_LANG"] = next_lang
    return set_lang(next_lang)


def is_ru() -> bool:
    return get_lang() == "ru"


def tr(en: str, ru: str) -> str:
    """Return the localized string for the active language."""
    return ru if get_lang() == "ru" else en
