"""Phase 4 tests: the .cap format (read/write), the editor tokenizer port and Level 3.

Run from the project root:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import random
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from samples import BIG, GAME, GAME_CAP, MESH, MESH_CAP, has_cyrillic  # noqa: E402,F401

from ccdec import cap_tokens as ct, cli  # noqa: E402
from ccdec.cap_format import (LATEST, SIGNATURE, Archive, CapError, WideString, read_cap,  # noqa: E402
                              write_cap)
from ccdec.cap_writer import CONTROL_VKS, build_cap  # noqa: E402
from ccdec.compare import Canon, compare_trees  # noqa: E402
from ccdec.container import load_container  # noqa: E402
from ccdec.index import build_index  # noqa: E402
from ccdec.reader import parse_project  # noqa: E402

REFERENCE_CAPS = [(MESH_CAP, "cp1252", 17), (GAME_CAP, "cp1251", 18)]


def rebuild(exe: str):
    c = load_container(exe)
    p = parse_project(c)
    r = build_cap(build_index(c, p))
    data = write_cap(r.app, r.encoding)
    return r, data, read_cap(data, r.encoding)


def walk_params(app: dict):
    def visit(events):
        for e in events:
            for kind in ("conditions", "actions"):
                for a in e.get(kind, []):
                    for p in a["params"]:
                        yield p
            yield from visit(e.get("events", []))
    for sheet in app["event_sheets"]:
        yield from visit(sheet["events"])


def names_of(app: dict) -> tuple[ct.Names, dict]:
    names, type_names = ct.Names(), {}
    for t in app["object_types"]:
        ty = t["type"]
        names.object_types[ty["name"].lower()] = ty["id"]
        type_names[ty["id"]] = ty["name"]
        for b in ty["behaviors"]:
            names.behaviors[(ty["id"], b["name"].lower())] = b["id"]
        for v in ty["private_variables"]:
            names.private_variables[(ty["id"], v["name"].lower())] = v["id"]
    for g in app["global_variables"]:
        names.global_variables[g["name"].lower()] = g["id"]
    return names, type_names


class ReferenceCaps(unittest.TestCase):
    """Real files written by the editor (class versions 17 and 18)."""

    def test_read_and_rewrite_byte_identical(self) -> None:
        for path, enc, version in REFERENCE_CAPS:
            if not os.path.exists(path):
                self.skipTest("reference .cap not present")
            with open(path, "rb") as f:
                data = f.read()
            app = read_cap(data, enc)
            self.assertEqual(app["_version"], version, path)
            self.assertEqual(write_cap(app, enc), data, path)

    def test_contents(self) -> None:
        if not os.path.exists(GAME_CAP):
            self.skipTest("reference .cap not present")
        with open(GAME_CAP, "rb") as f:
            app = read_cap(f.read(), "cp1251")
        self.assertEqual(app["created_with"], 10102)
        self.assertEqual([l["name"] for l in app["layouts"]], ["Layout 1", "Layout 2", "Layout 3", "Layout 4"])
        self.assertTrue(has_cyrillic(app["file_path"]))
        self.assertEqual(len(app["object_types"]), 70)
        self.assertIn(("Sprite", "Sprite.csx"), [(p["name"], p["file"]) for p in app["plugins"]])
        self.assertEqual(app["controls"][0], {"name": "Move Left", "combo_index": 21, "vk": 37, "player": 0})

    def test_tokenizer_reproduces_every_stored_parameter(self) -> None:
        """The port of CExpression::ParseUserString(editorMode) + ProcessEdittimeExpressionInformation
        must give exactly the tokens the editor stored - types, lengths, owners, ids, sub-types."""
        checked = 0
        for path, enc, _ in REFERENCE_CAPS:
            if not os.path.exists(path):
                continue
            with open(path, "rb") as f:
                app = read_cap(f.read(), enc)
            names, type_names = names_of(app)
            for p in walk_params(app):
                if p["type"] in (3, 6, 9, 13, 14):          # stored as a single number, not as an expression
                    continue
                toks = ct.tokenize(ct.source_text(p["tokens"]), type_names.get(p["oid"]))
                ct.resolve(toks, names, p["oid"])
                self.assertEqual([t.as_cap() for t in toks], p["tokens"])
                checked += 1
        if not checked:
            self.skipTest("reference .cap not present")
        self.assertGreaterEqual(checked, 199)             # 199 parameters in the public Mesh Editor sample alone

    def test_control_table(self) -> None:
        if not os.path.exists(GAME_CAP):
            self.skipTest("reference .cap not present")
        with open(GAME_CAP, "rb") as f:
            app = read_cap(f.read(), "cp1251")
        for c in app["controls"]:
            self.assertEqual(CONTROL_VKS.index(c["vk"]), c["combo_index"], c["name"])


@unittest.skipUnless(os.path.exists(MESH) and os.path.exists(MESH_CAP), "sample pair not present")
class RebuildMeshEditor(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.r, cls.data, cls.app = rebuild(MESH)

    def test_written_file_round_trips(self) -> None:
        self.assertTrue(self.data.startswith(SIGNATURE))
        self.assertEqual(write_cap(self.app, self.r.encoding), self.data)
        self.assertEqual(self.app["_version"], LATEST["CApplication"])

    def test_equivalent_to_the_original_cap(self) -> None:
        """Acceptance 2/3 as far as it can be checked without the editor: the project rebuilt
        from the exe has the same settings, object types, layouts, instances, animations and
        event tree as the original .cap.  (Parameter types of plugins without source code come
        from tools/learn_aces.py, which was fed this same .cap - see FORMAT.md 14.)"""
        with open(MESH_CAP, "rb") as f:
            ref = read_cap(f.read(), "cp1252")
        self.assertEqual(compare_trees(self.app, ref, "rebuilt", "reference", frozenset(self.r.trigger_heads)), [])

    def test_triggers_returned_to_groups(self) -> None:
        self.assertEqual(self.r.stats["triggers_returned_to_groups"], 24)
        top = self.app["event_sheets"][0]["events"]
        self.assertEqual([e["title"] for e in top],
                         ["Control", "Mesh update", "Drag points", "Edit Z", "Buttons", "Create Wireframe"])

    def test_ids_are_consistent(self) -> None:
        app = self.app
        type_ids = {t["key"] for t in app["object_types"]}
        self.assertEqual(len(type_ids), 27)
        for l in app["layouts"]:
            keys = [o["key"] for o in l["objects"]]
            self.assertEqual(len(keys), len(set(keys)))
            self.assertEqual(sorted(k for y in l["layers"] for k in y["z_order"]), sorted(keys))
            for o in l["objects"]:
                self.assertIn(o["object"]["object_type"], type_ids)
            self.assertFalse(type_ids & set(keys))
        image_ids = {i["id"] for i in app["images"]}

        def frames(a):
            yield from a["images"]
            for s in a["sub_animations"]:
                yield from frames(s)
        for a in app["animations"]:
            self.assertTrue(set(frames(a)) <= image_ids)
        self.assertGreater(app["next_object_id"], max(type_ids))

    def test_rows_have_positive_space(self) -> None:
        # The event editor deletes any event/condition/action whose Space is <= 0.
        def visit(events):
            for e in events:
                self.assertGreater(e["space"], 0)
                for a in e.get("conditions", []) + e.get("actions", []):
                    self.assertGreater(a["space"], 0)
                visit(e.get("events", []))
        visit(self.app["event_sheets"][0]["events"])


@unittest.skipUnless(os.path.exists(GAME) and os.path.exists(GAME_CAP), "sample pair not present")
class RebuildGame(unittest.TestCase):
    def test_differences_are_only_the_later_edits(self) -> None:
        """This .cap was saved after the exe was exported (FORMAT.md 4.1): the player sprites
        were replaced, one behavior value and a few events were edited.  Everything else matches."""
        r, data, app = rebuild(GAME)
        with open(GAME_CAP, "rb") as f:
            ref = read_cap(f.read(), "cp1251")
        diffs = compare_trees(app, ref, "rebuilt", "reference", frozenset(r.trigger_heads))
        top = [d for d in diffs if not d.startswith("    ")]
        for d in top:
            self.assertTrue(d.startswith("events of layout") or "Sprite" in d, d)
        self.assertFalse(any(d.startswith(("application.", "layout count")) for d in top))
        a, b = Canon(app), Canon(ref)
        self.assertEqual(set(a.object_types()), set(b.object_types()))
        same = sum(1 for n, t in a.object_types().items() if b.object_types()[n] == t)
        self.assertEqual(same, 69)                       # all but the edited 'Sprite' behavior
        self.assertEqual(app["layouts"][1]["name"], "Layout 2")
        self.assertTrue(has_cyrillic(self.p_cap_path(GAME)))

    @staticmethod
    def p_cap_path(exe: str) -> str:
        return parse_project(load_container(exe)).app.cap_path


@unittest.skipUnless(os.path.exists(BIG), "large sample exe not present")
class RebuildBigGame(unittest.TestCase):
    def test_builds_and_rereads(self) -> None:
        r, data, app = rebuild(BIG)
        self.assertEqual(len(app["layouts"]), 75)
        self.assertEqual(len(app["files"]), 41)
        self.assertTrue(app["files"][0]["name"].startswith("file_0001."))
        self.assertEqual(write_cap(app, r.encoding), data)


class Units(unittest.TestCase):
    def test_tokenizer_quirks(self) -> None:
        kinds = lambda s: [(t.type, t.text) for t in ct.tokenize(s)]
        T = ct.T
        self.assertEqual(kinds("a-5"), [(T["IDENTIFIER"], "a"), (T["SUBTRACT"], "-"), (T["INTEGER"], "5")])
        # after whitespace the editor classifies '-' as the start of a number
        self.assertEqual(kinds("a - 5"), [(T["IDENTIFIER"], "a"), (52, " "), (T["INTEGER"], "-"), (52, " "),
                                          (T["INTEGER"], "5")])
        self.assertEqual(kinds("(-5.5)"), [(T["LEFTBRACKET"], "("), (T["FLOAT"], "-5.5"), (T["RIGHTBRACKET"], ")")])
        self.assertEqual(kinds('"a""b$n"'), [(T["STRINGLITERAL"], 'a""b\n')])
        self.assertEqual(kinds("Floor(x) and y"), [(T["FUNC_INT"], "Floor"), (T["LEFTBRACKET"], "("),
                                                   (T["IDENTIFIER"], "x"), (T["RIGHTBRACKET"], ")"), (52, " "),
                                                   (T["AND"], "and"), (52, " "), (T["IDENTIFIER"], "y")])
        self.assertEqual(kinds("a<>b!=c<=d"), [(7, "a"), (T["NOTEQUAL"], "<>"), (7, "b"), (T["NOTEQUAL"], "!="),
                                              (7, "c"), (T["LESSEQUAL"], "<="), (7, "d")])
        self.assertEqual(kinds("Obj[Mov].Speed"), [(7, "Obj[Mov]"), (T["DOT"], "."), (7, "Speed")])
        self.assertEqual(ct.tokenize("Self.X", "Hero")[0].text, "Hero")
        with self.assertRaises(ct.TokenizeError):
            ct.tokenize('"open')
        with self.assertRaises(ct.TokenizeError):
            ct.tokenize("a # b")

    def test_resolve(self) -> None:
        names = ct.Names(object_types={"hero": 7}, behaviors={(7, "platform"): 9},
                         private_variables={(7, "hp"): 2, (3, "hp"): 5}, global_variables={"score": 4})
        toks = ct.tokenize("Hero('hp') + global('Score') + 'hp' + Hero[Platform].Speed")
        ct.resolve(toks, names, 3)
        by_text = {(t.text, t.type): t for t in toks}
        self.assertEqual((by_text[("Hero", 7)].owner, by_text[("Hero", 7)].subtype), (7, ct.TT_OBJECTNAME))
        hp = [t for t in toks if t.text == "hp"]
        self.assertEqual([(t.owner, t.id) for t in hp], [(7, 2), (3, 5)])      # object's, then the caller's
        score = next(t for t in toks if t.text == "Score")
        self.assertEqual((score.owner, score.id), (-1, 4))
        mov = next(t for t in toks if t.text == "Hero[Platform]")
        self.assertEqual((mov.subtype, mov.id, mov.owner), (ct.TT_MOVEMENTNAME, 9, 7))

    def test_cstring_forms(self) -> None:
        for s in ("", "abc", "x" * 254, "y" * 255, "z" * 70000, WideString("шир")):
            ar = Archive(None, "cp1251")
            ar.cstr({"k": s}, "k")
            back: dict = {}
            rd = Archive(bytes(ar.out), "cp1251")
            rd.cstr(back, "k")
            self.assertEqual(back["k"], s)
            self.assertEqual(type(back["k"]), type(s))
            self.assertEqual(rd.pos, len(ar.out))
        ar = Archive(None, "cp1251")
        ar.cstr({"k": "я"}, "k")
        self.assertEqual(bytes(ar.out), b"\x01\xff")

    def test_float_bits_survive(self) -> None:
        raw = struct.pack("<I", 0x7FA00001)            # a signalling NaN
        d: dict = {}
        Archive(raw).f32(d, "x")
        out = Archive(None)
        out.f32(d, "x")
        self.assertEqual(bytes(out.out), raw)


class Malformed(unittest.TestCase):
    def test_not_a_cap(self) -> None:
        for blob in (b"", b"MZ\x90", b"Construct1.6\0junk", SIGNATURE, SIGNATURE + b"\x05Wrong\x01\0\0\0"):
            with self.assertRaises(CapError):
                read_cap(blob)

    @unittest.skipUnless(os.path.exists(MESH_CAP), "reference .cap not present")
    def test_truncated_and_corrupted_files_fail_cleanly(self) -> None:
        with open(MESH_CAP, "rb") as f:
            good = f.read()
        rng = random.Random(3)
        for _ in range(25):
            with self.assertRaises(CapError):
                read_cap(good[:rng.randrange(len(SIGNATURE), len(good) - 1)], "cp1252")
        for _ in range(40):
            bad = bytearray(good)
            for _ in range(rng.randint(1, 3)):
                bad[rng.randrange(13, 60000)] = rng.randrange(256)
            try:
                read_cap(bytes(bad), "cp1252")
            except CapError:
                pass

    def test_huge_count_rejected(self) -> None:
        with self.assertRaises(CapError):
            read_cap(SIGNATURE + b"\x0cCApplication" + struct.pack("<i", 18) + b"\xff\xff\xff\xff\xff\xff\x7f")

    def test_missing_field_on_write(self) -> None:
        with self.assertRaises(CapError):
            write_cap({"name": "x"})


@unittest.skipUnless(os.path.exists(MESH), "sample exe not present")
class CommandLine(unittest.TestCase):
    def test_level_3_and_comparisons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(cli.main([MESH, "-o", tmp, "--level", "3", "--report"]), 0)
            cap = os.path.join(tmp, "Mesh Editor.cap")
            with open(cap, "rb") as f:
                app = read_cap(f.read(), "cp1252")
            self.assertEqual(app["name"], "Mesh Editor 1.0")
            with open(os.path.join(tmp, "report.md"), encoding="utf-8") as f:
                self.assertIn("Level 3", f.read())
            self.assertEqual(cli.main([MESH, "--compare-cap", cap]), 0)          # equivalent to itself
            self.assertEqual(cli.main([MESH, "--compare-exe", MESH]), 0)
            if os.path.exists(GAME):
                self.assertEqual(cli.main([MESH, "--compare-exe", GAME]), 3)     # different projects


if __name__ == "__main__":
    unittest.main()
