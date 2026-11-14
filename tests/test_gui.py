"""Tests for the one-call pipeline and the tkinter window.

The window tests are skipped when no display is available.
"""

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

from samples import BIG, GAME, GAME_CAP, MESH, MESH_CAP, has_cyrillic  # noqa: E402,F401
from ccdec import i18n  # noqa: E402

i18n.set_language("ru")            # these tests check Russian texts; English is tested separately

from ccdec import pipeline  # noqa: E402
from ccdec.cap_format import read_cap  # noqa: E402



class Warnings(unittest.TestCase):
    def test_path_problem(self) -> None:
        self.assertIsNone(pipeline.path_problem(r"C:\cc\game.cap", acp=1252))
        self.assertIsNone(pipeline.path_problem(r"C:\Игры\game.cap", acp=1251))
        self.assertIn("И г р ы", pipeline.path_problem(r"C:\Игры\game.cap", acp=1252) or "")
        self.assertIsNotNone(pipeline.path_problem(r"C:\cc\game.cap", acp=65001))

    def test_text_problem(self) -> None:
        self.assertIsNone(pipeline.text_problem("cp1251", True, acp=1251))
        self.assertIsNone(pipeline.text_problem("cp1251", False, acp=1252))      # nothing to garble
        self.assertIn("кракозябрами", pipeline.text_problem("cp1251", True, acp=1252) or "")
        self.assertIsNotNone(pipeline.text_problem("cp1251", True, acp=65001))

    def test_default_output_dir(self) -> None:
        out = pipeline.default_output_dir(os.path.join("x", "My Game.exe"))
        self.assertTrue(out.endswith("My Game_recovered"))
        self.assertTrue(os.path.isabs(out))


@unittest.skipUnless(os.path.exists(MESH), "sample exe not present")
class Pipeline(unittest.TestCase):
    def test_levels(self) -> None:
        for level, expect in ((1, {"images", "assets.json", "report.md"}),
                              (2, {"images", "assets.json", "project.json", "report.md"}),
                              (3, {"images", "assets.json", "project.json", "report.md", "Mesh Editor.cap"})):
            with tempfile.TemporaryDirectory() as tmp:
                out = os.path.join(tmp, "результат")                     # created on demand, Cyrillic is fine
                steps: list[float] = []
                logs: list[str] = []
                r = pipeline.run(MESH, out, pipeline.Options(level=level), log=logs.append,
                                 progress=lambda f, s: steps.append(f))
                self.assertLessEqual(expect, set(os.listdir(out)), level)
                self.assertEqual(steps[-1], 1.0)
                self.assertEqual(steps, sorted(steps))                    # progress never goes backwards
                self.assertTrue(r.summary and logs)
                self.assertEqual(r.cap_path is not None, level == 3)
                if level == 3:
                    with open(r.cap_path, "rb") as f:
                        self.assertEqual(read_cap(f.read(), "cp1252")["name"], "Mesh Editor 1.0")

    def test_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(pipeline.Cancelled):
                pipeline.run(MESH, tmp, cancelled=lambda: True)
            calls = []
            with self.assertRaises(pipeline.Cancelled):                  # cancel while images are written
                pipeline.run(MESH, tmp, pipeline.Options(level=1),
                             cancelled=lambda: len(calls.append(1) or calls) > 6)
            self.assertFalse(os.path.exists(os.path.join(tmp, "report.md")))

    def test_bad_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            junk = os.path.join(tmp, "junk.exe")
            with open(junk, "wb") as f:
                f.write(b"this is not a program" * 50)
            with self.assertRaises(pipeline.RecoveryError) as cm:
                pipeline.run(junk, os.path.join(tmp, "o"))
            self.assertIn("программ", str(cm.exception))
            with self.assertRaises(pipeline.RecoveryError):              # a file where the folder should be
                pipeline.run(MESH, junk)
            with self.assertRaises(OSError):
                pipeline.run(os.path.join(tmp, "missing.exe"), os.path.join(tmp, "o"))

    @unittest.skipUnless(os.path.exists(GAME), "sample exe not present")
    def test_cyrillic_game_warnings_depend_on_the_system(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for acp, expect in ((1251, False), (1252, True)):
                with mock.patch.object(pipeline, "system_codepage", return_value=acp):
                    r = pipeline.run(GAME, os.path.join(tmp, str(acp)), pipeline.Options(level=2))
                self.assertEqual(any("кракозябр" in w for w in r.warnings), expect, acp)


def make_root():
    try:
        import tkinter as tk
    except ImportError:                       # e.g. the Windows "embeddable" Python has no Tk
        raise unittest.SkipTest("tkinter is not available")
    try:
        return tk.Tk()
    except tk.TclError:
        raise unittest.SkipTest("no display available")


@unittest.skipUnless(os.path.exists(MESH), "sample exe not present")
class Window(unittest.TestCase):
    def setUp(self) -> None:
        from ccdec import gui
        self.gui = gui
        self.root = make_root()
        self.root.withdraw()

    def tearDown(self) -> None:
        self.root.destroy()

    def wait(self, app, timeout: float = 120.0) -> None:
        end = time.time() + timeout
        while not app.finished.is_set() and time.time() < end:
            self.root.update()
            time.sleep(0.02)
        self.root.update()
        self.assertTrue(app.finished.is_set(), "the worker did not finish")

    def test_form_and_defaults(self) -> None:
        app = self.gui.App(self.root)
        app.exe_var.set(MESH)
        self.assertTrue(app.out_var.get().endswith("Mesh Editor_recovered"))
        self.assertEqual(app.level_var.get(), 3)
        self.assertEqual(str(app.cancel_btn["state"]), "disabled")
        self.assertEqual(str(app.report_btn["state"]), "disabled")
        app.out_edited = True
        app.exe_var.set(GAME)
        self.assertTrue(app.out_var.get().endswith("Mesh Editor_recovered"))   # a manual choice is kept

    def test_run_to_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = self.gui.App(self.root)
            app.exe_var.set(MESH)
            app.out_var.set(os.path.join(tmp, "out"))
            self.assertTrue(app.start(confirm=False))
            self.assertEqual(str(app.start_btn["state"]), "disabled")           # busy: cannot start twice
            self.assertFalse(app.start(confirm=False))
            self.wait(app)
            self.assertIsNotNone(app.result)
            self.assertTrue(os.path.exists(app.result.cap_path))
            self.assertEqual(str(app.start_btn["state"]), "normal")
            self.assertEqual(str(app.report_btn["state"]), "normal")
            self.assertEqual(str(app.folder_btn["state"]), "normal")
            self.assertEqual(float(app.progress["value"]), 1000.0)
            text = app.log.get("1.0", "end")
            self.assertIn("Готово", app.status_var.get())
            self.assertIn("Mesh Editor 1.0", text)
            with mock.patch.object(self.gui, "open_path") as op:
                app._open_folder()
                app._open_report()
            self.assertEqual([c.args[0] for c in op.call_args_list],
                             [app.result.out_dir, app.result.report_path])

    def test_error_is_shown_not_raised(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            junk = os.path.join(tmp, "junk.exe")
            with open(junk, "wb") as f:
                f.write(b"nope" * 100)
            app = self.gui.App(self.root)
            app.exe_var.set(junk)
            app.out_var.set(os.path.join(tmp, "out"))
            with mock.patch.object(self.gui.messagebox, "showerror") as err:
                self.assertTrue(app.start(confirm=False))
                self.wait(app)
            err.assert_called_once()
            self.assertIn("программ", err.call_args.args[1])
            self.assertEqual(str(app.start_btn["state"]), "normal")
            self.assertIsNone(app.result)

    def test_missing_file_does_not_start(self) -> None:
        app = self.gui.App(self.root)
        app.exe_var.set(os.path.join(tempfile.gettempdir(), "no_such_game.exe"))
        self.assertFalse(app.start(confirm=False))
        self.assertIn("не выбран", app.status_var.get())

    def test_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app = self.gui.App(self.root)
            app.exe_var.set(MESH)
            app.out_var.set(os.path.join(tmp, "out"))
            app.start(confirm=False)
            app._cancel()
            self.wait(app)
            self.assertIsNone(app.result)
            self.assertIn("Остановлено", app.status_var.get())
            self.assertEqual(str(app.start_btn["state"]), "normal")


if __name__ == "__main__":
    unittest.main()
