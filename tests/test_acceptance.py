"""Phase 5: acceptance criteria end to end, format-version coverage, robustness.

Run from the project root:  python -m unittest discover -s tests -v

Optional manual check (criterion 3): export the rebuilt .cap from Construct Classic and
point CCDEC_REEXPORT at the new exe, CCDEC_REEXPORT_SOURCE at the original one:
    set CCDEC_REEXPORT=C:\\games\\reexported.exe
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import os
import random
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from samples import BIG, GAME, GAME_CAP, MESH, MESH_CAP, has_cyrillic  # noqa: E402,F401
from ccdec import i18n  # noqa: E402

i18n.set_language("ru")            # these tests check Russian texts; English is tested separately

from ccdec import cli  # noqa: E402
from ccdec.cap_format import read_cap, write_cap  # noqa: E402
from ccdec.cap_writer import build_cap  # noqa: E402
from ccdec.container import load_container  # noqa: E402
from ccdec.export_assets import export_assets  # noqa: E402
from ccdec.export_json import Dumper  # noqa: E402
from ccdec.index import build_index  # noqa: E402
from ccdec.reader import PROFILES, parse_project  # noqa: E402
from ccdec.report import build_report  # noqa: E402

CAPS = {17: MESH_CAP, 18: GAME_CAP}

#: exe layout profile -> samples that must parse with it (FORMAT.md 9)
EXE_COVERAGE = {"r2": [GAME, BIG], "0.99-2009": [MESH]}


def quiet(fn, *args):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        return fn(*args), out.getvalue()


class FormatVersions(unittest.TestCase):
    """Criterion 5: a test for every format version that is covered."""

    def test_every_exe_profile_has_a_sample(self) -> None:
        self.assertEqual(set(EXE_COVERAGE), {p.name for p in PROFILES})
        tested = 0
        for profile, samples in EXE_COVERAGE.items():
            for path in samples:
                if not os.path.exists(path):
                    continue
                p = parse_project(load_container(path))
                self.assertEqual(p.raw, {}, path)
                self.assertEqual(p.profiles["APPBLOCK"], profile, path)
                self.assertEqual(p.profiles["LEVELBLOCK"], profile, path)
                tested += 1
        if not tested:
            self.skipTest("no samples present")

    def test_every_cap_version_with_a_sample_round_trips(self) -> None:
        tested = 0
        for version, path in CAPS.items():
            if not os.path.exists(path):
                continue
            with open(path, "rb") as f:
                data = f.read()
            app = read_cap(data, "cp1251")
            self.assertEqual(app["_version"], version)
            self.assertEqual(write_cap(app, "cp1251"), data)
            tested += 1
        if not tested:
            self.skipTest("no reference .cap present")


@unittest.skipUnless(os.path.exists(MESH), "sample exe not present")
class UnknownPlugins(unittest.TestCase):
    """Criterion 4: an unknown plugin or version does not crash anything and is listed in the report."""

    def test_full_pipeline_with_unknown_plugins(self) -> None:
        c = load_container(MESH)
        # Pretend every plugin is a third-party one nobody has sources or samples for.
        c.plugins = [dataclasses.replace(p, file_name=f"Mystery{p.index}.csx", display_name=None)
                     for p in c.plugins]
        p = parse_project(c)
        # ...with ACE names nothing in the source database knows (ccdec matches plugins by
        # their ACE tables too, so renaming the files alone would still be recognised).
        for owner in [*p.level.object_types, *p.level.behaviors]:
            if owner.ace:
                for table in (owner.ace.actions, owner.ace.conditions, owner.ace.expressions):
                    for k, e in enumerate(table):
                        if e.script_name:
                            table[k] = dataclasses.replace(e, script_name=f"Unknown{k}")
        ix = build_index(c, p)
        self.assertTrue(all(pa.source is None for pa in ix.plugin_aces))
        with tempfile.TemporaryDirectory() as tmp:
            assets = export_assets(ix, tmp)
            dumper = Dumper(ix)
            dump = dumper.project()
            self.assertEqual(len(dump["object_types"]), 27)
            rebuilt = build_cap(ix)
            data = write_cap(rebuilt.app, rebuilt.encoding)
            self.assertEqual(write_cap(read_cap(data, rebuilt.encoding), rebuilt.encoding), data)
            report = build_report(ix, assets, dumper, 3, rebuilt)
        self.assertIn("Mystery0.csx", report)
        self.assertIn("нет исходников", report)
        self.assertGreater(rebuilt.stats.get("aces_without_metadata", 0), 100)
        self.assertTrue(any("без метаданных" in n for n in rebuilt.notes))


@unittest.skipUnless(os.path.exists(MESH), "sample exe not present")
class CorruptedExe(unittest.TestCase):
    """Every level on damaged input either works (with reported issues) or fails with a message."""

    def test_damaged_blocks_end_to_end(self) -> None:
        with open(MESH, "rb") as f:
            good = f.read()
        c = load_container(MESH)
        targets = [c.blocks[n] for n in ("APPBLOCK", "LEVELBLOCK", "EVENTBLOCK", "IMAGEBLOCK")]
        rng = random.Random(11)
        codes = []
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, "damaged.exe")
            for k in range(12):
                bad = bytearray(good)
                block = targets[k % len(targets)]
                for _ in range(rng.randint(1, 4)):
                    bad[block.offset + rng.randrange(min(block.size, 4000))] = rng.randrange(256)
                with open(exe, "wb") as f:
                    f.write(bad)
                code, output = quiet(cli.main, [exe, "-o", os.path.join(tmp, f"o{k}"), "--level", "3", "--report"])
                self.assertIn(code, (0, 1), output[-500:])
                if code == 1:
                    self.assertIn("ccdec:", output)
                else:
                    self.assertIn("Отчёт ccdec", output)
                codes.append(code)
        self.assertIn(0, codes)          # most damage is survivable: the block is kept raw and reported


@unittest.skipUnless(os.environ.get("CCDEC_REEXPORT"), "set CCDEC_REEXPORT to an exe re-exported from the rebuilt .cap")
class ReExport(unittest.TestCase):
    """Criterion 3: the exe exported again from the rebuilt .cap is equivalent to the original."""

    def test_reexport_equivalent(self) -> None:
        source = os.environ.get("CCDEC_REEXPORT_SOURCE", BIG)
        code, output = quiet(cli.main, [source, "--compare-exe", os.environ["CCDEC_REEXPORT"]])
        self.assertEqual(code, 0, output[-2000:])
        self.assertIn("equivalent", output)


if __name__ == "__main__":
    unittest.main()
