"""Phase 3 tests: Level 1 (assets) and Level 2 (project.json), checked against the
reference .cap files where they exist.

Run from the project root:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import collections
import filecmp
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from samples import BIG, GAME, GAME_CAP, MESH, MESH_CAP, has_cyrillic  # noqa: E402,F401

import capscan  # noqa: E402
from ccdec import cli, model as m, png  # noqa: E402
from ccdec.container import load_container  # noqa: E402
from ccdec.export_assets import build_ico, export_assets, sniff  # noqa: E402
from ccdec.export_json import Dumper, export_json  # noqa: E402
from ccdec.expressions import render  # noqa: E402
from ccdec.index import build_index, safe_component  # noqa: E402
from ccdec.reader import parse_project  # noqa: E402

VENDOR = os.path.join(ROOT, "vendor", "construct-src")

PLAIN_PARAM_TYPES = {"value", "string", "any", "array", "layer"}


def load(path: str):
    c = load_container(path)
    p = parse_project(c)
    return c, p, build_index(c, p)


def walk_aces(events: list):
    """(ace dict, owner object of the ACE) for every condition/action in a dumped event list."""
    for e in events:
        if "group" in e:
            yield from walk_aces(e["events"])
            continue
        for x in e["conditions"] + e["actions"]:
            yield x
        yield from walk_aces(e.get("sub_events", []))


def exe_param_keys(dump: dict) -> set[str]:
    """Plain-typed parameter texts of the exe, normalised like capscan.squash.

    The editor accepts ``.X`` for the owning object's own X; the exporter writes the
    object name instead, so the owner-prefixed form is also accepted as ``.X``.
    """
    keys = set()
    for layout in dump["events"]["layouts"]:
        for ace in walk_aces(layout["events"]):
            owner = ace["object"].split("[")[0]
            for q in ace["params"]:
                if q.get("type") in PLAIN_PARAM_TYPES:
                    text = q["expression"]
                    keys.add((capscan.squash(text),
                              capscan.squash(re.sub(rf"(?<![\w.]){re.escape(owner)}\.", ".", text))))
    return keys


class Level1MeshEditor(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(MESH):
            raise unittest.SkipTest("sample exe not present")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.c, cls.p, cls.ix = load(MESH)
        cls.assets = export_assets(cls.ix, cls.tmp.name, analyse_masks=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_images_named_after_objects(self) -> None:
        self.assertEqual([i["file"] for i in self.assets["images"]], [
            "images/MeshObj/Default/frame_000.png", "images/TiledBackground/texture.png",
            "images/Vertex/Default/frame_000.png", "images/TiledBackground2/texture.png",
            "images/DragMask/Default/frame_000.png", "images/SliderBg/Default/frame_000.png",
            "images/ZSlider/Default/frame_000.png"])

    def test_images_are_byte_identical_to_the_cap(self) -> None:
        """Acceptance 1: same count, same hashes as the images in the reference .cap."""
        with open(MESH_CAP, "rb") as f:
            cap = {hashlib.sha1(x).hexdigest() for x in capscan.pngs(f.read())}
        exe = [i["sha1"] for i in self.assets["images"]]
        self.assertEqual(len(exe), 7)
        self.assertTrue(set(exe) <= cap)
        self.assertEqual(len(cap - set(exe)), 1)       # the remaining .cap PNG is the application icon
        for i in self.assets["images"]:
            with open(os.path.join(self.tmp.name, i["file"]), "rb") as f:
                self.assertEqual(hashlib.sha1(f.read()).hexdigest(), i["sha1"])

    def test_masks_match_alpha(self) -> None:
        self.assertEqual({i["collision_mask"]["kind"] for i in self.assets["images"]}, {"alpha"})
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "masks")))

    def test_effects_and_icon(self) -> None:
        self.assertEqual([(e["source_path"], e["file"]) for e in self.assets["effects"]],
                         [("Effects\\Smart Text.fx", "effects/Smart Text.fxo"),
                          ("Effects\\Outline.fx", "effects/Outline.fxo")])
        with open(os.path.join(self.tmp.name, self.assets["icon"]), "rb") as f:
            ico = f.read()
        count, = struct.unpack_from("<H", ico, 4)
        size, offset = struct.unpack_from("<II", ico, 14)
        self.assertEqual((count, offset, offset + size), (1, 22, len(ico)))

    def test_index_file(self) -> None:
        with open(os.path.join(self.tmp.name, "assets.json"), encoding="utf-8") as f:
            index = json.load(f)
        first = index["images"][0]
        self.assertEqual((first["hotspot"], first["width"], first["height"]), ([79, 66], 158, 132))
        self.assertEqual(first["used_by"][0]["object"], "MeshObj")


class Level1Game(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(GAME):
            raise unittest.SkipTest("sample exe not present")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.c, cls.p, cls.ix = load(GAME)
        cls.assets = export_assets(cls.ix, cls.tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_counts_and_naming(self) -> None:
        imgs = self.assets["images"]
        self.assertEqual(len(imgs), 95)
        self.assertEqual(sum(i["file"].startswith("images/_orphaned_animation_") for i in imgs), 35)
        self.assertEqual(sum(i["file"].startswith("images/_unreferenced/") for i in imgs), 1)
        self.assertEqual(len({i["file"].lower() for i in imgs}), 95)

    @unittest.skipUnless(os.path.exists(GAME_CAP), "paired .cap not present")
    def test_images_against_the_later_cap(self) -> None:
        """The .cap was saved after this export (see FORMAT.md 4.1): images that still exist
        are byte-identical, the rest were changed or removed afterwards."""
        with open(GAME_CAP, "rb") as f:
            cap = collections.Counter(hashlib.sha1(x).hexdigest()[:8] for x in capscan.pngs(f.read()))
        exe = collections.Counter(i["sha1"][:8] for i in self.assets["images"])
        self.assertEqual(len(set(exe) & set(cap)), 6)
        self.assertEqual(set(exe) - set(cap), {"79c4acab", "25f7c2fc"})
        self.assertEqual(set(cap) - set(exe), {"e80a2614", "d9d4aaaf"})   # 3 new frames + the icon


@unittest.skipUnless(os.path.exists(BIG), "large sample exe not present")
class Level1BigGame(unittest.TestCase):
    def test_files_and_images(self) -> None:
        c, p, ix = load(BIG)
        with tempfile.TemporaryDirectory() as tmp:
            a = export_assets(ix, tmp)
            self.assertEqual(collections.Counter(f["type"] for f in a["files"]), {"mp3": 37, "wav": 4})
            self.assertEqual(len(a["images"]), 176)
            for f in a["files"][:3]:
                self.assertEqual(os.path.getsize(os.path.join(tmp, f["file"])), f["size"])


class Level2MeshEditor(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(MESH):
            raise unittest.SkipTest("sample exe not present")
        cls.tmp = tempfile.TemporaryDirectory()
        cls.c, cls.p, cls.ix = load(MESH)
        export_assets(cls.ix, cls.tmp.name)
        cls.dump, cls.dumper = export_json(cls.ix, cls.tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_json_file_round_trips(self) -> None:
        with open(os.path.join(self.tmp.name, "project.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["application"]["name"], "Mesh Editor 1.0")

    def test_every_ace_has_metadata(self) -> None:
        self.assertEqual(self.dumper.unknown_aces, {})
        self.assertEqual(self.dumper.property_problems, {})

    def test_event_texts(self) -> None:
        texts = [a["text"] for l in self.dump["events"]["layouts"] for a in walk_aces(l["events"])]
        self.assertEqual(len(texts), 208)
        for expected in ('Zoom: Set text to "Zoom: " & round(ZoomX) & "%"',
                         "On key Num pad 0 pressed",
                         "Set global variable targetzoom to 100",
                         "Vertex: Value selected Equal to 1",
                         "ZSlider: Set Y to max(63, min(475, MouseY(\"UI\")))"):
            self.assertIn(expected, texts)

    def test_groups_and_nesting(self) -> None:
        top = self.dump["events"]["layouts"][0]["events"]
        groups = [e["group"] for e in top if "group" in e]
        self.assertEqual(groups[:5], ["Control", "Mesh update", "Drag points", "Edit Z", "Buttons"])

    def test_objects(self) -> None:
        types = {t["name"]: t for t in self.dump["object_types"]}
        self.assertEqual([b["name"] for b in types["DragMask"]["behaviors"]], ["DragDrop"])
        self.assertEqual(types["MeshObj"]["animations"][0]["animations"][0]["name"], "Default")
        self.assertEqual(types["Vertex"]["container"], ["Vertex", "VerticalLine"])
        self.assertEqual([e["name"] for e in types["Text"]["effects"]], ["Smart Text"])
        layout = self.dump["layouts"][0]
        self.assertEqual([y["name"] for y in sorted(layout["layers"], key=lambda y: y["editor_index"])],
                         ["Non-layout", "Background", "Grid", "Main", "Manipulation", "UI"])
        texts = [i for y in layout["layers"] for i in y["instances"] if i["object"] == "StaticText"]
        self.assertTrue(texts and "text" in texts[0]["properties"])

    @unittest.skipUnless(os.path.exists(MESH_CAP), "paired .cap not present")
    def test_parameters_agree_with_the_cap(self) -> None:
        """Acceptance 2 (partial, until the Phase 4 .cap reader): every plain value/string
        parameter recovered from the exe occurs among the .cap's CEditorParam records."""
        with open(MESH_CAP, "rb") as f:
            cap = {capscan.squash(q.text) for q in capscan.editor_params(f.read(), self.p.encoding)
                   if q.type in (1, 2, 10, 11, 12)}
        keys = exe_param_keys(self.dump)
        self.assertGreater(len(keys), 70)
        missing = [k for k in keys if not (k[0] in cap or k[1] in cap)]
        self.assertEqual(missing, [])


class Level2Game(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not os.path.exists(GAME):
            raise unittest.SkipTest("sample exe not present")
        cls.c, cls.p, cls.ix = load(GAME)
        cls.dump = Dumper(cls.ix).project()

    def test_cyrillic_and_controls(self) -> None:
        self.assertTrue(has_cyrillic(self.dump["application"]["original_cap_path"]))
        self.assertEqual(self.dump["application"]["controls"][0]["key"], "Left arrow")

    def test_events(self) -> None:
        texts = [a["text"] for l in self.dump["events"]["layouts"] for a in walk_aces(l["events"])]
        self.assertIn("On collision between Sprite and Sprite2212", texts)
        self.assertIn("Go to layout 3 with transition \"None\" lasting 0 MS", texts)

    @unittest.skipUnless(os.path.exists(GAME_CAP), "paired .cap not present")
    def test_parameters_agree_with_the_cap(self) -> None:
        with open(GAME_CAP, "rb") as f:
            cap = {capscan.squash(q.text) for q in capscan.editor_params(f.read(), self.p.encoding)
                   if q.type in (1, 2, 10, 11, 12)}
        missing = [k for k in exe_param_keys(self.dump) if not (k[0] in cap or k[1] in cap)]
        self.assertEqual(missing, [])


class Units(unittest.TestCase):
    def test_render(self) -> None:
        T = m.Token
        toks = [T(7, "Sprite"), T(13), T(7, "X"), T(17), T(4, -5), T(19), T(10), T(5, 0.5), T(18), T(4, 2), T(11)]
        self.assertEqual(render(toks), "Sprite.X + -5 * (0.5 - 2)")
        self.assertEqual(render([T(6, 'say "hi"\n'), T(38), T(28), T(10), T(9, "v"), T(11)]),
                         '"say ""hi""$n" & int(\'v\')')
        self.assertEqual(render([T(50), T(10), T(4, 10), T(11)]), "random(10)")
        self.assertEqual(render([T(5, 1.0)]), "1.0")

    def test_sniff(self) -> None:
        self.assertEqual(sniff(b"RIFF\0\0\0\0WAVEfmt "), "wav")
        self.assertEqual(sniff(b"ID3\x03\0"), "mp3")
        self.assertEqual(sniff(b"\0" * 417 + b"\xff\xfb\x90\0"), "mp3")
        self.assertEqual(sniff(b"OggS\0"), "ogg")
        self.assertEqual(sniff(bytes(range(256))), "bin")

    def test_safe_component(self) -> None:
        self.assertEqual(safe_component("a/b\\c:d"), "a_b_c_d")
        self.assertEqual(safe_component("CON"), "CON_")
        self.assertEqual(safe_component(".."), "_")
        self.assertEqual(safe_component("Игрок"), "Игрок")
        self.assertEqual(safe_component("x\udc98y"), "x_y")

    def test_png_round_trip(self) -> None:
        rows = [bytes([1, 0, 1]), bytes([0, 1, 0])]
        img = png.decode(png.encode_bilevel(rows, 3))
        self.assertEqual((img.width, img.height), (3, 2))
        self.assertEqual(img.alpha_mask(), [b"\x01\x01\x01", b"\x01\x01\x01"])   # opaque gray
        self.assertEqual([bytes(1 if img.rgba[(y * 3 + x) * 4] else 0 for x in range(3)) for y in range(2)], rows)
        with self.assertRaises(png.PNGError):
            png.decode(b"\x89PNG\r\n\x1a\n" + b"\0" * 30)

    def test_png_bomb_is_bounded(self) -> None:
        import zlib
        ihdr = struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0)
        bomb = zlib.compress(b"\0" * (50 << 20))
        data = png.SIGNATURE + png._chunk(b"IHDR", ihdr) + png._chunk(b"IDAT", bomb) + png._chunk(b"IEND", b"")
        self.assertEqual(png.decode(data).width, 4)        # inflates only what the header announces

    @unittest.skipUnless(os.path.exists(MESH), "sample exe not present")
    def test_ico_rebuilt(self) -> None:
        ico = build_ico(load_container(MESH))
        self.assertEqual(ico[:6], b"\0\0\1\0\1\0")

    @unittest.skipUnless(os.path.exists(MESH), "sample exe not present")
    def test_cli_levels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cli.main([MESH, "-o", tmp, "--level", "2", "--report"]), 0)
            for name in ("project.json", "assets.json", "report.md"):
                self.assertTrue(os.path.exists(os.path.join(tmp, name)), name)
            self.assertEqual(cli.main([MESH, "-o", tmp, "--level", "3"]), 0)
            self.assertTrue(os.path.exists(os.path.join(tmp, "Mesh Editor.cap")))


@unittest.skipUnless(os.path.isdir(VENDOR), "vendor/construct-src not present")
class AceDatabase(unittest.TestCase):
    def test_database_is_reproducible_from_the_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "aces.json")
            subprocess.run([sys.executable, "-I", os.path.join(ROOT, "tools", "gen_aces.py"), VENDOR, out],
                           check=True, capture_output=True)
            self.assertTrue(filecmp.cmp(out, os.path.join(ROOT, "ccdec", "data", "aces.json"), shallow=False))


if __name__ == "__main__":
    unittest.main()
