"""Light and dark appearance for the tkinter window.

"system" follows the Windows setting "Choose your app mode" (Windows 10/11); where that does
not exist (Windows 7, other systems) it means light.  The dark look uses the portable "clam"
ttk theme recoloured; the light look keeps the native Windows theme.  On Windows 10/11 the
title bar is switched too (DWMWA_USE_IMMERSIVE_DARK_MODE); elsewhere that step is skipped.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional

MODES = ("system", "light", "dark")


@dataclass(frozen=True)
class Palette:
    dark: bool
    bg: str
    surface: str
    field: str
    fg: str
    muted: str
    border: str
    accent: str
    select_fg: str
    warn: str
    error: str
    ok: str


LIGHT = Palette(False, bg="#f0f0f0", surface="#ffffff", field="#ffffff", fg="#000000", muted="#6d6d6d",
                border="#a0a0a0", accent="#0a64c8", select_fg="#ffffff",
                warn="#a15c00", error="#b00020", ok="#1b7f3b")
DARK = Palette(True, bg="#1f2023", surface="#2b2d31", field="#26282c", fg="#e6e6e6", muted="#8d939b",
               border="#44474e", accent="#3d7fe0", select_fg="#ffffff",
               warn="#f0b45b", error="#ff7070", ok="#62d391")


def system_prefers_dark() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return int(value) == 0
    except (OSError, ImportError, ValueError):
        return False                      # Windows 7 and older have no app mode: light


def resolve(mode: str) -> Palette:
    if mode == "dark" or (mode == "system" and system_prefers_dark()):
        return DARK
    return LIGHT


def _dark_title_bar(root, dark: bool) -> None:
    """Ask Windows 10/11 for a dark (or light) title bar; silently does nothing elsewhere."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id()) or root.winfo_id()
        value = ctypes.c_int(1 if dark else 0)
        for attribute in (20, 19):            # 19 on Windows 10 builds before 20H1
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value),
                                                          ctypes.sizeof(value)) == 0:
                break
    except Exception:
        pass


def apply(root, palette: Palette, native_theme: Optional[str] = None) -> None:
    """Style every ttk widget class used by the window (call before or after building it)."""
    from tkinter import ttk
    style = ttk.Style(root)
    if not palette.dark:
        names = style.theme_names()
        style.theme_use(native_theme if native_theme in names else ("vista" if "vista" in names else "clam"))
        root.configure(bg=palette.bg)
        root.option_clear()                   # drop the dark drop-down colours, if any were set
        _dark_title_bar(root, False)
        return

    p = palette
    style.theme_use("clam")
    style.configure(".", background=p.bg, foreground=p.fg, fieldbackground=p.field, bordercolor=p.border,
                    lightcolor=p.surface, darkcolor=p.bg, troughcolor=p.surface, focuscolor=p.accent,
                    selectbackground=p.accent, selectforeground=p.select_fg, insertcolor=p.fg,
                    arrowcolor=p.fg)
    style.map(".", foreground=[("disabled", p.muted)])
    style.configure("TFrame", background=p.bg)
    style.configure("TLabel", background=p.bg, foreground=p.fg)
    style.configure("TLabelframe", background=p.bg, bordercolor=p.border)
    style.configure("TLabelframe.Label", background=p.bg, foreground=p.fg)
    style.configure("TButton", background=p.surface, foreground=p.fg, bordercolor=p.border, padding=(10, 3))
    style.map("TButton", background=[("disabled", p.bg), ("pressed", p.accent), ("active", "#3a3d43")],
              foreground=[("disabled", p.muted)])
    style.configure("TEntry", fieldbackground=p.field, foreground=p.fg, insertcolor=p.fg)
    style.map("TEntry", fieldbackground=[("disabled", p.bg)], foreground=[("disabled", p.muted)])
    style.configure("TCombobox", fieldbackground=p.field, background=p.surface, foreground=p.fg,
                    arrowcolor=p.fg)
    style.map("TCombobox", fieldbackground=[("readonly", p.field), ("disabled", p.bg)],
              foreground=[("readonly", p.fg), ("disabled", p.muted)],
              selectbackground=[("readonly", p.field)], selectforeground=[("readonly", p.fg)],
              background=[("active", "#3a3d43")])
    for cls in ("TRadiobutton", "TCheckbutton"):
        style.configure(cls, background=p.bg, foreground=p.fg, indicatorbackground=p.field,
                        indicatorforeground=p.fg, upperbordercolor=p.border, lowerbordercolor=p.border)
        style.map(cls, background=[("active", p.bg)], indicatorbackground=[("selected", p.accent),
                                                                          ("disabled", p.bg)],
                  foreground=[("disabled", p.muted)])
    style.configure("Horizontal.TProgressbar", background=p.accent, troughcolor=p.surface, bordercolor=p.border,
                    lightcolor=p.accent, darkcolor=p.accent)
    style.configure("Vertical.TScrollbar", background=p.surface, troughcolor=p.bg, bordercolor=p.border,
                    arrowcolor=p.fg)
    style.map("Vertical.TScrollbar", background=[("active", "#3a3d43")])
    root.configure(bg=p.bg)
    # The drop-down list of a combobox is a classic Tk widget: colour it through the option database.
    root.option_add("*TCombobox*Listbox.background", p.field)
    root.option_add("*TCombobox*Listbox.foreground", p.fg)
    root.option_add("*TCombobox*Listbox.selectBackground", p.accent)
    root.option_add("*TCombobox*Listbox.selectForeground", p.select_fg)
    _dark_title_bar(root, True)


def style_text(widget, palette: Palette) -> None:
    """Colours of the classic Tk Text log, which ttk styles do not reach."""
    p = palette
    widget.configure(background=p.surface if p.dark else "#ffffff", foreground=p.fg, insertbackground=p.fg,
                     selectbackground=p.accent, selectforeground=p.select_fg,
                     highlightbackground=p.border, highlightcolor=p.border)
    widget.tag_configure("warn", foreground=p.warn)
    widget.tag_configure("error", foreground=p.error)
    widget.tag_configure("ok", foreground=p.ok)
