"""Light/dark appearance and the shared settings file."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from samples import MESH  # noqa: E402
from ccdec import i18n, theme  # noqa: E402


class Resolve(unittest.TestCase):
    def test_modes(self) -> None:
        with mock.patch.object(theme, "system_prefers_dark", return_value=True):
            self.assertTrue(theme.resolve("system").dark)
            self.assertFalse(theme.resolve("light").dark)
        with mock.patch.object(theme, "system_prefers_dark", return_value=False):
            self.assertFalse(theme.resolve("system").dark)
            self.assertTrue(theme.resolve("dark").dark)

    def test_system_query_never_raises(self) -> None:
        self.assertIn(theme.system_prefers_dark(), (True, False))

    def test_readable_contrast(self) -> None:
        def luminance(color: str) -> float:
            r, g, b = (int(color[i:i + 2], 16) / 255 for i in (1, 3, 5))
            lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in (r, g, b)]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]

        def contrast(a: str, b: str) -> float:
            la, lb = sorted((luminance(a), luminance(b)), reverse=True)
            return (la + 0.05) / (lb + 0.05)

        for p in (theme.LIGHT, theme.DARK):
            log_bg = p.surface if p.dark else "#ffffff"
            for name in ("fg", "warn", "error", "ok"):
                self.assertGreaterEqual(contrast(getattr(p, name), log_bg), 4.5, (p.dark, name))   # WCAG AA
            self.assertGreaterEqual(contrast(p.fg, p.bg), 4.5)
            self.assertGreaterEqual(contrast(p.warn, p.bg), 4.5)


class Settings(unittest.TestCase):
    def test_settings_are_merged_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ccdec", "settings.json")
            with mock.patch.object(i18n, "settings_path", return_value=path):
                i18n.save_language("en")
                i18n.save_setting("theme", "dark")
                with open(path, encoding="utf-8") as f:
                    self.assertEqual(json.load(f), {"language": "en", "theme": "dark"})
                with open(path, "w") as f:
                    f.write("[1, 2]")                       # valid JSON, wrong shape
                self.assertEqual(i18n.load_settings(), {})


class Window(unittest.TestCase):
    def setUp(self) -> None:
        try:
            import tkinter as tk
            self.root = tk.Tk()
        except Exception:
            raise unittest.SkipTest("no tkinter / display")
        self.root.withdraw()
        i18n.set_language("ru")

    def tearDown(self) -> None:
        self.root.destroy()

    def test_saved_theme_and_switching(self) -> None:
        from ccdec import gui
        with mock.patch.object(gui, "load_settings", return_value={"theme": "dark"}), \
                mock.patch.object(gui, "save_setting") as saved:
            app = gui.App(self.root, MESH)
            self.assertEqual(app.theme_mode, "dark")
            self.assertEqual(app.theme_var.get(), "Тёмная")
            self.assertEqual(str(app.log["background"]), theme.DARK.surface)
            self.assertEqual(gui.ttk.Style(self.root).theme_use(), "clam")

            app.theme_var.set("Светлая")
            app._change_theme()
            saved.assert_called_once_with("theme", "light")
            self.assertEqual(str(app.log["background"]), "#ffffff")
            self.assertNotEqual(gui.ttk.Style(self.root).theme_use(), "clam" if sys.platform == "win32" else "")
            self.assertEqual(app.exe_var.get(), MESH)               # nothing else changed

            app.lang_var.set("English")                              # a language rebuild keeps the theme
            with mock.patch.object(gui, "save_language"):
                app._change_language()
            self.assertEqual(app.theme_var.get(), "Light")
            self.assertEqual(str(app.log["background"]), "#ffffff")
        i18n.set_language("ru")

    def test_bad_saved_theme_falls_back_to_system(self) -> None:
        from ccdec import gui
        with mock.patch.object(gui, "load_settings", return_value={"theme": "purple"}):
            app = gui.App(self.root)
        self.assertEqual(app.theme_mode, "system")


if __name__ == "__main__":
    unittest.main()
