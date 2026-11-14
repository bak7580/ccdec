"""Russian/English texts: language choice, English output end to end, window switching."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from samples import MESH  # noqa: E402
from ccdec import i18n, pipeline  # noqa: E402


class Language(unittest.TestCase):
    def tearDown(self) -> None:
        i18n.set_language("ru")

    def test_T_and_set(self) -> None:
        i18n.set_language("en")
        self.assertEqual(i18n.T("Готово", "Done"), "Done")
        i18n.set_language("ru")
        self.assertEqual(i18n.T("Готово", "Done"), "Готово")
        with self.assertRaises(ValueError):
            i18n.set_language("de")

    def test_detect_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = os.path.join(tmp, "ccdec", "settings.json")
            with mock.patch.object(i18n, "settings_path", return_value=settings):
                with mock.patch.dict(os.environ, {"CCDEC_LANG": "en"}):
                    self.assertEqual(i18n.detect(), "en")
                i18n.save_language("ru")
                self.assertTrue(os.path.exists(settings))
                with mock.patch.dict(os.environ, {"CCDEC_LANG": ""}):
                    self.assertEqual(i18n.detect(), "ru")          # the saved choice wins over the system
                with open(settings, "w") as f:
                    f.write("not json")
                with mock.patch.dict(os.environ, {"CCDEC_LANG": ""}):
                    self.assertIn(i18n.detect(), i18n.LANGUAGES)   # a broken file is ignored


@unittest.skipUnless(os.path.exists(MESH), "sample exe not present")
class EnglishOutput(unittest.TestCase):
    def setUp(self) -> None:
        i18n.set_language("en")

    def tearDown(self) -> None:
        i18n.set_language("ru")

    def test_pipeline_and_report_in_english(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs: list[str] = []
            r = pipeline.run(MESH, tmp, pipeline.Options(level=3), log=logs.append)
            with open(r.report_path, encoding="utf-8") as f:
                report = f.read()
        self.assertIn("# ccdec report", report)
        self.assertIn("## Lost at export", report)
        self.assertIn("Decisions made for you while rebuilding", report)
        self.assertTrue(r.summary[0].startswith('Game "Mesh Editor 1.0"'))
        self.assertIn("Done.", logs)
        report = report.replace(os.path.abspath(MESH), "").replace(MESH, "")   # the path itself may be Cyrillic
        cyrillic = [line for line in report.splitlines() if any("Ѐ" <= ch <= "ӿ" for ch in line)]
        self.assertEqual(cyrillic, [])                             # nothing left untranslated

    def test_cli_lang_switch(self) -> None:
        from ccdec import cli
        with tempfile.TemporaryDirectory() as tmp, mock.patch("sys.stdout"):
            self.assertEqual(cli.main([MESH, "-o", tmp, "--level", "1", "--report", "--lang", "ru"]), 0)
            with open(os.path.join(tmp, "report.md"), encoding="utf-8") as f:
                self.assertIn("# Отчёт ccdec", f.read())


@unittest.skipUnless(os.path.exists(MESH), "sample exe not present")
class WindowLanguage(unittest.TestCase):
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
        i18n.set_language("ru")

    def test_switch_keeps_the_form_and_the_log(self) -> None:
        from ccdec import gui
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(gui, "save_language") as saved:
            app = gui.App(self.root)
            app.exe_var.set(MESH)
            app.out_var.set(os.path.join(tmp, "out"))
            app.level_var.set(1)
            self.assertTrue(app.start(confirm=False))
            end = time.time() + 120
            while not app.finished.is_set() and time.time() < end:
                self.root.update()
                time.sleep(0.02)
            self.root.update()
            self.assertIn("Готово", app.status_var.get())
            app.lang_var.set("English")
            app._change_language()
            saved.assert_called_once_with("en")
            self.assertEqual(str(app.start_btn["text"]), "Recover")
            self.assertIn("Done", app.status_var.get())
            self.assertEqual(app.exe_var.get(), MESH)            # the form survives the rebuild
            self.assertEqual(app.level_var.get(), 1)
            self.assertIn("Mesh Editor 1.0", app.log.get("1.0", "end"))
            self.assertEqual(str(app.folder_btn["state"]), "normal")
            self.assertEqual(app.enc_var.get(), "Detect automatically")


if __name__ == "__main__":
    unittest.main()
