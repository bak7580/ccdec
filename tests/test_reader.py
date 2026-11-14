"""Phase 2 tests: block parsers.

Run from the project root:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import random
import re
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from samples import BIG, GAME, GAME_CAP, MESH, MESH_CAP, has_cyrillic  # noqa: E402,F401

from ccdec import model as m  # noqa: E402
from ccdec.container import load_container, parse_container  # noqa: E402
from ccdec.reader import (ParseError, Reader, guess_encoding, iter_strings,  # noqa: E402
                          parse_block, parse_project)
import pe_builder as pb  # noqa: E402



def count_events(lines: list) -> tuple[int, int, int, int]:
    """(events, groups, conditions, actions) in an event tree."""
    e = g = c = a = 0
    for x in lines:
        if isinstance(x, m.Event):
            e, c, a = e + 1, c + len(x.conditions), a + len(x.actions)
        else:
            g += 1
        se, sg, sc, sa = count_events(x.children)
        e, g, c, a = e + se, g + sg, c + sc, a + sa
    return e, g, c, a


def cstr(s: str) -> bytes:
    b = s.encode("cp1252") + b"\0"
    return struct.pack("<i", len(b)) + b


def app_block(eye: bool) -> bytes:
    """A synthetic APPBLOCK in either known layout."""
    out = cstr("Demo") + struct.pack("<ii", 640, 480)
    if eye:
        out += struct.pack("<f", 500.0)
    out += struct.pack("<iBBiBi", 0, 0, 1, 60, 0, 1)
    out += struct.pack("<i", 1) + cstr("score") + struct.pack("<i", 0) + cstr("10")
    out += struct.pack("<i", 1) + cstr("Jump") + struct.pack("<ii", 16, 0)
    out += b"\0"
    out += struct.pack("<i", 2) + cstr("k1") + struct.pack("<ii", 0, 7) + cstr("k2") + struct.pack("<i", 1) + cstr("v")
    out += struct.pack("<i", 0) + cstr("C:\\x.cap")
    out += struct.pack("<iBiiBfBBiBfiii", 0, 0, 0, 1, 0, 60.0, 1, 1, 0, 1, 0.0, -1, 0, 0)
    return out


@unittest.skipUnless(os.path.exists(MESH), "sample exe not present")
class MeshEditor2009(unittest.TestCase):
    """Runtime built 2009-08: the older layout profile."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.c = load_container(MESH)
        cls.p = parse_project(cls.c)

    def test_everything_parsed_to_the_last_byte(self) -> None:
        self.assertEqual(self.p.raw, {})
        self.assertEqual(self.p.issues, [])
        self.assertEqual(self.p.profiles, {"APPBLOCK": "0.99-2009", "LEVELBLOCK": "0.99-2009",
                                           "EVENTBLOCK": "-", "IMAGEBLOCK": "-", "HLSL": "-"})
        self.assertEqual(self.p.encoding, "cp1252")

    def test_app(self) -> None:
        a = self.p.app
        self.assertEqual((a.name, a.window_width, a.window_height), ("Mesh Editor 1.0", 800, 600))
        self.assertIsNone(a.eye_distance)
        self.assertEqual((a.fps, a.sampler, a.preview_layout), (60, 1, -1))
        self.assertEqual(len(a.global_variables), 12)
        self.assertEqual(a.global_variables[0], m.GlobalVariable("targetzoom", 0, "100"))
        self.assertEqual([c.name for c in a.controls][:2], ["Move Left", "Move Right"])
        self.assertTrue(a.cap_path.endswith("Mesh Editor.cap"))

    def test_level(self) -> None:
        lv = self.p.level
        self.assertEqual(len(lv.object_types), 27)
        self.assertEqual(lv.object_types[0].name, "MeshObj")
        self.assertEqual(len(lv.object_types[0].private_variables), 4)
        self.assertIsNotNone(lv.object_types[0].ace)
        self.assertEqual([(b.name, b.owner_oid, len(b.data)) for b in lv.behaviors],
                         [("DragDrop", 13, 20), ("SelectionBox", 15, 8)])
        self.assertEqual([(t.name, t.oids) for t in lv.traits], [("Destroy on Startup", [10])])
        self.assertEqual(lv.containers, [[10, 23]])
        (layout,) = lv.layouts
        self.assertEqual([(y.name, len(y.instances)) for y in layout.layers],
                         [("Background", 1), ("Grid", 1), ("Main", 4), ("Manipulation", 1),
                          ("UI", 25), ("Non-layout", 4)])
        self.assertTrue(layout.layers[2].enable_3d)
        self.assertIsNone(layout.layers[0].clear_depth_buffer)
        self.assertEqual(len(layout.image_ids), 7)

    def test_plugin_of_each_type_is_named(self) -> None:
        names = {t.name: self.c.plugins[t.plugin_index].file_name for t in self.p.level.object_types}
        self.assertEqual(names["MeshObj"], "Sprite.csx")
        self.assertEqual(names["Function"], "Function.csx")
        self.assertEqual(names["CommonDialog"], "CommonDialog.csx")

    def test_events(self) -> None:
        ev = self.p.events
        self.assertEqual(ev.sheet_names, ["Layout 1 events"])
        self.assertEqual(count_events(ev.layout_events[0]), (95, 30, 84, 124))

    def test_images_and_effects(self) -> None:
        self.assertEqual([i.id for i in self.p.images], [0, 1, 2, 3, 4, 6, 9])
        self.assertTrue(all(i.format == "png" for i in self.p.images))
        first = self.p.images[0]
        self.assertEqual((first.hotspot_x, first.hotspot_y, len(first.data)), (79, 66, 32148))
        self.assertEqual(len(first.mask), first.mask_pitch * first.mask_height)
        self.assertEqual([s.path for s in self.p.hlsl.shaders], ["Effects\\Smart Text.fx", "Effects\\Outline.fx"])
        self.assertEqual(len(self.p.hlsl.object_effects), 5)


@unittest.skipUnless(os.path.exists(GAME), "sample exe not present")
class Game2011(unittest.TestCase):
    """Private sample A: exe/cap pair (runtime built 2011-05, Cyrillic strings)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.c = load_container(GAME)
        cls.p = parse_project(cls.c)

    def test_everything_parsed_to_the_last_byte(self) -> None:
        self.assertEqual(self.p.raw, {})
        self.assertEqual(self.p.issues, [])
        self.assertEqual(self.p.profiles["APPBLOCK"], "r2")
        self.assertEqual(self.p.profiles["LEVELBLOCK"], "r2")

    def test_cyrillic_detected(self) -> None:
        self.assertEqual(self.p.encoding, "cp1251")
        self.assertTrue(has_cyrillic(self.p.app.cap_path))
        self.assertEqual(self.p.app.eye_distance, 500.0)

    def test_structure(self) -> None:
        lv = self.p.level
        self.assertEqual((len(lv.object_types), len(lv.behaviors)), (70, 3))
        self.assertEqual([(l.name, l.width, l.height) for l in lv.layouts],
                         [("Layout 1", 640, 480), ("Layout 2", 5000, 480),
                          ("Layout 3", 3000, 480), ("Layout 4", 10000, 5555)])
        self.assertEqual([t.name for t in lv.traits], ["Platform", "Solid", "Center view on me"])
        self.assertEqual(len(self.p.images), 95)
        self.assertEqual(len(self.p.events.sheet_names), 4)
        self.assertEqual([count_events(l)[0] for l in self.p.events.layout_events], [2, 2, 1, 3])
        self.assertEqual([r.name for r in self.c.dependencies], ["MIKMOD.DLL"])

    @unittest.skipUnless(os.path.exists(GAME_CAP), "paired .cap not present")
    def test_names_agree_with_the_original_cap(self) -> None:
        """Ground truth without a .cap parser yet: every name recovered from the exe
        must literally occur in the original .cap."""
        with open(GAME_CAP, "rb") as f:
            cap = f.read()
        lv = self.p.level
        names = ([t.name for t in lv.object_types] + [l.name for l in lv.layouts]
                 + [y.name for l in lv.layouts for y in l.layers]
                 + [b.name for b in lv.behaviors] + [t.name for t in lv.traits]
                 + self.p.events.sheet_names
                 + [v.name for t in lv.object_types for v in t.private_variables])
        self.assertGreater(len(names), 80)
        missing = [n for n in names if n.encode("cp1251") not in cap]
        self.assertEqual(missing, [])

    @unittest.skipUnless(os.path.exists(GAME_CAP), "paired .cap not present")
    def test_instance_data_is_stored_verbatim_in_the_cap(self) -> None:
        """FORMAT.md 4.1: the plugin property blob of an instance is the same bytes in both files."""
        with open(GAME_CAP, "rb") as f:
            cap = f.read()
        blobs = [i.data for l in self.p.level.layouts for y in l.layers for i in y.instances if len(i.data) >= 16]
        self.assertGreater(len(blobs), 20)
        different = [b for b in blobs if struct.pack("<i", len(b)) + b not in cap]
        # This .cap was saved after the exe was exported: three player sprites got new
        # animation trees (ids 399/405/411, above the exe's maximum of 383).  Those blobs
        # differ from the .cap in the 4-byte animation root id only; all others are identical.
        self.assertEqual(len(different), 3)
        for b in different:
            self.assertEqual(len(b), 54)
            pattern = re.escape(struct.pack("<i", 54) + b[:25]) + b"...." + re.escape(b[29:])
            self.assertIsNotNone(re.search(pattern, cap, re.S))


@unittest.skipUnless(os.path.exists(BIG), "large sample exe not present")
class BigGame2011(unittest.TestCase):
    def test_parses_cleanly(self) -> None:
        c = load_container(BIG)
        p = parse_project(c)
        self.assertEqual(p.raw, {})
        self.assertEqual(p.issues, [])
        self.assertEqual(p.encoding, "cp1251")
        self.assertEqual((len(p.level.object_types), len(p.level.layouts), len(p.images)), (433, 75, 176))
        self.assertEqual(len(c.files), 41)


class Synthetic(unittest.TestCase):
    def test_app_profiles(self) -> None:
        app, profile = parse_block("APPBLOCK", app_block(eye=True))
        self.assertEqual(profile.name, "r2")
        self.assertEqual(app.eye_distance, 500.0)
        self.assertEqual(app.global_keys, [m.KeyEntry("k1", 0, 7), m.KeyEntry("k2", 1, "v")])
        app, profile = parse_block("APPBLOCK", app_block(eye=False))
        self.assertEqual(profile.name, "0.99-2009")
        self.assertIsNone(app.eye_distance)
        self.assertEqual(app.global_variables, [m.GlobalVariable("score", 0, "10")])

    def test_events_grammar(self) -> None:
        tok = struct.pack("<iq", 4, 42) + struct.pack("<id", 5, 1.5) + struct.pack("<i", 6) + cstr("hi") \
            + struct.pack("<i", 17)
        param = struct.pack("<i", 4) + tok
        cond = bytes([4]) + struct.pack("<iiBii", -1, 3, 1, -1, 1) + param + bytes([5])
        act = bytes([8]) + struct.pack("<iiii", 2, 7, 0, 0) + bytes([9])
        event = bytes([2]) + struct.pack("<ii", 5, 0) + bytes([3]) + cond + bytes([6, 7]) + act + bytes([10])
        sub = bytes([2]) + struct.pack("<ii", 6, 0) + bytes([3, 6, 7, 10, 13])
        group = bytes([15, 1]) + cstr("G") + event + sub + bytes([13]) + bytes([16])
        block = struct.pack("<i", 1) + cstr("Sheet") + struct.pack("<i", 1) + bytes([1]) + group + bytes([14])
        ev, _ = parse_block("EVENTBLOCK", block)
        self.assertEqual(ev.sheet_names, ["Sheet"])
        (g,) = ev.layout_events[0]
        self.assertEqual((g.active, g.name), (True, "G"))
        (e,) = g.children
        self.assertEqual((e.line_number, len(e.children)), (5, 1))
        c = e.conditions[0]
        self.assertEqual((c.oid, c.cnd_id, c.negated), (-1, 3, True))
        self.assertEqual([(t.name, t.value) for t in c.params[0]],
                         [("INTEGER", 42), ("FLOAT", 1.5), ("STRINGLITERAL", "hi"), ("ADD", None)])
        self.assertEqual((e.actions[0].oid, e.actions[0].act_id, e.actions[0].movement_id), (2, 7, 0))

    def test_hlsl_empty_and_failed_compile(self) -> None:
        h, _ = parse_block("HLSL", struct.pack("<5i", 1, 0, 0, 0, 0))
        self.assertEqual((h.run_without_ps, h.shaders), (1, []))
        h, _ = parse_block("HLSL", b"\0" * 400)      # editor's fallback when an .fx fails to compile
        self.assertEqual(h.shaders, [])

    def test_unparseable_block_is_kept_raw(self) -> None:
        res = [r for r in pb.construct_resources(0) if r[0] != "APPBLOCK"] + [("APPBLOCK", 997, 0, app_block(True))]
        p = parse_project(parse_container(pb.build_pe(res)))
        self.assertIsNotNone(p.app)                    # good block still parsed
        self.assertEqual(set(p.raw), {"LEVELBLOCK", "EVENTBLOCK", "HLSL"})
        self.assertEqual(p.raw["LEVELBLOCK"], b"level")
        self.assertEqual(len(p.issues), 3)
        self.assertTrue(all("kept as raw bytes" in i for i in p.issues))
        self.assertEqual(p.images, [])                 # 4 zero bytes = zero images

    def test_undecodable_bytes_round_trip(self) -> None:
        r = Reader(struct.pack("<i", 3) + b"\x98A\0", "X", "cp1251")   # 0x98 is undefined in cp1251
        s = r.cstr("s")
        self.assertEqual(s.encode("cp1251", "surrogateescape"), b"\x98A")

    def test_iter_strings(self) -> None:
        app, _ = parse_block("APPBLOCK", app_block(True))
        self.assertIn("score", set(iter_strings(app)))


class Malformed(unittest.TestCase):
    """Bad block content must surface as ParseError - no crashes, hangs or huge allocations."""

    def test_error_carries_block_and_offset(self) -> None:
        with self.assertRaises(ParseError) as cm:
            parse_block("APPBLOCK", app_block(True)[:30])
        self.assertEqual(cm.exception.block, "APPBLOCK")
        self.assertGreater(cm.exception.offset, 0)
        self.assertIn("APPBLOCK", str(cm.exception))

    def test_every_truncation_of_app_block_fails_cleanly(self) -> None:
        good = app_block(True)
        for cut in range(len(good)):
            with self.assertRaises(ParseError):
                parse_block("APPBLOCK", good[:cut])
        with self.assertRaises(ParseError):
            parse_block("APPBLOCK", good + b"\0")      # trailing garbage is an error too

    def test_huge_counts_are_rejected_before_allocation(self) -> None:
        for name in ("LEVELBLOCK", "EVENTBLOCK", "IMAGEBLOCK", "HLSL"):
            with self.assertRaises(ParseError):
                parse_block(name, struct.pack("<ii", 0x7FFFFFFF, 0x7FFFFFFF) + b"\0" * 64)
        with self.assertRaises(ParseError):
            parse_block("APPBLOCK", struct.pack("<i", 0x7FFFFFFF) + b"abc")

    def test_deep_nesting_is_capped(self) -> None:
        depth = 5000
        block = struct.pack("<i", 0) + struct.pack("<i", 1) + bytes([1]) \
            + (bytes([15, 1]) + cstr("g")) * depth + bytes([16]) * depth + bytes([14])
        with self.assertRaisesRegex(ParseError, "nested too deeply"):
            parse_block("EVENTBLOCK", block)

    @unittest.skipUnless(os.path.exists(MESH), "sample exe not present")
    def test_fuzzed_real_blocks_never_crash(self) -> None:
        c = load_container(MESH)
        rng = random.Random(7)
        for name in ("APPBLOCK", "LEVELBLOCK", "EVENTBLOCK", "HLSL"):
            good = c.blocks[name].data
            for _ in range(60):
                bad = bytearray(good)
                for _ in range(rng.randint(1, 3)):
                    bad[rng.randrange(len(bad))] = rng.randrange(256)
                if rng.random() < 0.3:
                    bad = bad[:rng.randrange(len(bad))]
                try:
                    parse_block(name, bytes(bad))
                except ParseError:
                    pass

    def test_guess_encoding_on_garbage(self) -> None:
        c = parse_container(pb.build_pe(pb.construct_resources(0)))
        self.assertEqual(guess_encoding(c), "cp1252")


if __name__ == "__main__":
    unittest.main()
