"""User interface language (Russian or English).

Texts are written in place as pairs, ``T("Готово", "Done")``, so each message keeps both
versions side by side.  The language is chosen, in order, from: ``set_language()``, the
CCDEC_LANG environment variable, the choice saved by the window, the Windows display
language, a Cyrillic Windows code page (people who switched "Language for non-Unicode
programs" to Russian, as Construct Classic needs), the LANG/LC_* variables, else English.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Optional

LANGUAGES = ("ru", "en")
LANGUAGE_NAMES = {"ru": "Русский", "en": "English"}
_current: Optional[str] = None


def settings_path() -> str:
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        return os.path.join(os.environ["APPDATA"], "ccdec", "settings.json")
    return os.path.join(os.path.expanduser("~"), ".config", "ccdec", "settings.json")


def load_settings() -> dict:
    """The window's saved settings ({} if none or unreadable)."""
    try:
        with open(settings_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_setting(key: str, value: str) -> None:
    """Store one setting, keeping the others (best effort: failures are ignored)."""
    data = load_settings()
    data[key] = value
    try:
        path = settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def _saved() -> Optional[str]:
    value = load_settings().get("language")
    return value if value in LANGUAGES else None


def save_language(lang: str) -> None:
    """Remember the window's language choice."""
    save_setting("language", lang)


def detect() -> str:
    env = os.environ.get("CCDEC_LANG", "").lower()[:2]
    if env in LANGUAGES:
        return env
    saved = _saved()
    if saved:
        return saved
    if sys.platform == "win32":
        try:
            import ctypes
            k = ctypes.windll.kernel32
            if k.GetUserDefaultUILanguage() & 0x3FF == 0x19:      # LANG_RUSSIAN
                return "ru"
            if k.GetACP() == 1251:
                return "ru"
        except Exception:
            pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        if os.environ.get(var, "").lower().startswith("ru"):
            return "ru"
    return "en"


def get_language() -> str:
    global _current
    if _current is None:
        _current = detect()
    return _current


def set_language(lang: str) -> None:
    global _current
    if lang not in LANGUAGES:
        raise ValueError(f"unsupported language {lang!r}; use one of {LANGUAGES}")
    _current = lang


def T(ru: str, en: str) -> str:
    """The text in the current language."""
    return ru if get_language() == "ru" else en


def plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Russian plural form: 1 уровень, 2 уровня, 5 уровней (also 11-14 -> many)."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many