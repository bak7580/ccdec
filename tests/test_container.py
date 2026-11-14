"""Phase 1 tests: container extraction.

Run from the project root:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import struct
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from ccdec import cli  # noqa: E402
from ccdec.container import (ContainerError, load_container, parse_container,  # noqa: E402
                             parse_string_block)
import pe_builder as pb  # noqa: E402

SAMPLE = os.path.join(HERE, "samples", "mesh_editor", "Mesh Editor.exe")


@unittest.skipUnless(os.path.exists(SAMPLE), "sample exe not present")
class MeshEditorSample(unittest.TestCase):
    """Ground truth: the exe/cap pair shipped in the Construct Classic source tree
    (runtime built 2009-08, i.e. a pre-r2 0.99.x era build)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.c = load_container(SAMPLE)

    def test_identity(self) -> None:
        c = self.c
        self.assertTrue(c.is_construct_export)
        self.assertFalse(c.is_preview)
        self.assertFalse(c.from_installer)
        self.assertFalse(c.has_python)
        self.assertEqual(c.size, 2941440)
        self.assertEqual(c.pe_timestamp, 1251065644)
        self.assertEqual(c.overlay_size, 0)
        self.assertEqual(len(c.resources), 30)
        self.assertEqual(c.warnings, [])

    def test_blocks(self) -> None:
        expected = {  # name: (size, sha1 prefix)
            "APPBLOCK": (633, "db815f6d078d"),
            "LEVELBLOCK": (103345, "adf7440848bd"),
            "EVENTBLOCK": (19759, "8960803a3f24"),
            "IMAGEBLOCK": (42628, "03f2859af575"),
            "HLSL": (3357, "0f23244e5575"),
            "MENUBLOCK": (4, "9069ca78e745"),
        }
        self.assertEqual(set(self.c.blocks), set(expected))
        for name, (size, sha) in expected.items():
            r = self.c.blocks[name]
            self.assertEqual(r.size, size, name)
            self.assertEqual(len(r.data), size, name)
            self.assertTrue(r.sha1.startswith(sha), name)

    def test_plugins(self) -> None:
        c = self.c
        # Count comes from the string table, exactly as CRuntime::UnpackPlugins reads it.
        self.assertEqual(c.declared_plugin_count, 13)
        self.assertEqual([p.index for p in c.plugins], list(range(13)))
        self.assertEqual([p.file_name for p in c.plugins], [
            "Sprite.csx", "TileBackground.csx", "Box.csx", "Text.csx", "MouseKeyb.csx",
            "Edit.csx", "Button.csx", "Function.csx", "CommonDialog.csx", "Line.csx",
            "File.csx", "DragDropMov.csx", "SelectionBoxMov.csx"])
        for p in c.plugins:
            self.assertEqual(p.resource.data[:2], b"MZ")
        self.assertEqual(c.plugins[0].resource.size, 205824)
        self.assertEqual(c.plugins[0].timestamp, 1251063685)

    def test_no_files_or_dependencies(self) -> None:
        self.assertEqual(self.c.files, [])
        self.assertEqual(self.c.dependencies, [])
        self.assertEqual(len(self.c.icons), 2)

    def test_cli_summary_and_raw_dump(self) -> None:
        text = cli.format_summary(self.c)
        self.assertIn("Sprite.csx", text)
        self.assertIn("exported exe", text)
        with tempfile.TemporaryDirectory() as tmp:
            written = cli.dump_raw(self.c, tmp)
            self.assertEqual(len(written), 6 + 13)
            total = sum(os.path.getsize(p) for p in written)
            self.assertEqual(total, sum(r.size for r in self.c.blocks.values())
                             + sum(p.resource.size for p in self.c.plugins))


class Synthetic(unittest.TestCase):
    def test_roundtrip_export(self) -> None:
        res = pb.construct_resources(3) + [
            ("FILES", 1, 0, b"RIFFdata"), ("FILES", 2, 0, b"second"),
            ("DEPENDENCIES", "FOO.DLL", 0, b"dep"), ("PYTHONLIBS", 992, 0, b"PK\x03\x04"),
        ]
        c = parse_container(pb.build_pe(res))
        self.assertTrue(c.is_construct_export)
        self.assertEqual(c.blocks["APPBLOCK"].data, b"app")
        self.assertEqual(c.blocks["EVENTBLOCK"].data, b"events")
        self.assertTrue(c.has_python)
        self.assertEqual(c.declared_plugin_count, 3)
        self.assertEqual([p.resource.data for p in c.plugins], [b"not a dll %d" % i for i in range(3)])
        self.assertEqual([r.data for r in c.files], [b"RIFFdata", b"second"])
        self.assertEqual([r.name for r in c.dependencies], ["FOO.DLL"])
        # The fake plugins are not PE files: reported, not fatal.
        self.assertEqual(len(c.warnings), 3)
        self.assertTrue(all("not a valid PE" in w for w in c.warnings))
        self.assertEqual([p.file_name for p in c.plugins], [None] * 3)

    def test_preview_build(self) -> None:
        c = parse_container(pb.build_pe(pb.construct_resources(2, preview=True)))
        self.assertTrue(c.is_preview)
        self.assertEqual(c.preview_plugin_names, ["Plugin0.csx", "Plugin1.csx"])
        self.assertEqual(c.plugins, [])
        self.assertTrue(any("preview build" in w for w in c.warnings))
        self.assertFalse(any("plugin count" in w for w in c.warnings))

    def test_plugin_count_mismatch(self) -> None:
        c = parse_container(pb.build_pe(pb.construct_resources(2, declared=5)))
        self.assertTrue(any("says 5" in w for w in c.warnings))

    def test_missing_block_and_gap(self) -> None:
        res = [r for r in pb.construct_resources(3)
               if r[0] != "EVENTBLOCK" and r[:2] != ("DLLBLOCK", 1001)]
        c = parse_container(pb.build_pe(res))
        self.assertTrue(any("EVENTBLOCK" in w and "missing" in w for w in c.warnings))
        self.assertTrue(any("not contiguous" in w for w in c.warnings))

    def test_not_construct(self) -> None:
        c = parse_container(pb.build_pe([(3, 1, 1033, b"icon")]))
        self.assertFalse(c.is_construct_export)
        self.assertTrue(any("does not look like" in w for w in c.warnings))

    def test_installer_wrapper(self) -> None:
        inner = pb.build_pe(pb.construct_resources(1))
        c = parse_container(pb.build_pe([("RUNTIME", 131, 0, inner)]))
        self.assertTrue(c.from_installer)
        self.assertTrue(c.is_construct_export)
        self.assertEqual(c.blocks["LEVELBLOCK"].data, b"level")

    def test_string_block(self) -> None:
        self.assertEqual(parse_string_block(pb.string_block("13")), ["", "13"])
        self.assertEqual(parse_string_block(b"\x05\x00a"), [])  # truncated: ignored


class Malformed(unittest.TestCase):
    """Untrusted input must fail with ContainerError, never crash or loop."""

    def setUp(self) -> None:
        self.good = pb.build_pe(pb.construct_resources(1))

    def test_not_pe(self) -> None:
        for blob in (b"", b"MZ", b"hello world" * 100, b"MZ" + b"\0" * 100):
            with self.assertRaises(ContainerError):
                parse_container(blob)

    def test_truncations(self) -> None:
        # Every prefix either parses or raises ContainerError - nothing else.
        for cut in list(range(0, 0x200, 7)) + list(range(0x200, len(self.good), 5)):
            try:
                parse_container(self.good[:cut])
            except ContainerError:
                pass

    def test_directory_cycle(self) -> None:
        bad = bytearray(self.good)
        # First root entry -> subdirectory at offset 0, i.e. the root itself.
        struct.pack_into("<I", bad, pb.RSRC_FILE_OFFSET + 16 + 4, 0x80000000)
        with self.assertRaisesRegex(ContainerError, "cycle"):
            parse_container(bytes(bad))

    def test_huge_entry_count(self) -> None:
        bad = bytearray(self.good)
        struct.pack_into("<HH", bad, pb.RSRC_FILE_OFFSET + 12, 0xFFFF, 0xFFFF)
        with self.assertRaisesRegex(ContainerError, "out of bounds"):
            parse_container(bytes(bad))

    def test_data_out_of_bounds(self) -> None:
        bad = bytearray(self.good)
        c = parse_container(self.good)
        app = c.blocks["APPBLOCK"]
        # Find APPBLOCK's data entry (rva, size) and inflate the size.
        needle = struct.pack("<II", pb.RSRC_RVA + app.offset - pb.RSRC_FILE_OFFSET, app.size)
        pos = bytes(bad).index(needle)
        struct.pack_into("<I", bad, pos + 4, 0x7FFFFFFF)
        with self.assertRaisesRegex(ContainerError, "not backed by file data"):
            parse_container(bytes(bad))

    def test_bit_flips_never_crash(self) -> None:
        import random
        rng = random.Random(1)
        for _ in range(400):
            bad = bytearray(self.good)
            for _ in range(rng.randint(1, 4)):
                bad[rng.randrange(len(bad))] = rng.randrange(256)
            try:
                parse_container(bytes(bad))
            except ContainerError:
                pass

    def test_size_limit(self) -> None:
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(self.good)
        try:
            with self.assertRaisesRegex(ContainerError, "limit"):
                load_container(f.name, max_file_size=100)
        finally:
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
