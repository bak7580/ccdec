"""Test helper: build a tiny PE32 image whose only content is a resource section."""

from __future__ import annotations

import struct
from typing import Iterable, Union

ResId = Union[int, str]
RSRC_RVA = 0x1000
RSRC_FILE_OFFSET = 0x200


def _dir_size(n: int) -> int:
    return 16 + 8 * n


def build_rsrc(resources: Iterable[tuple[ResId, ResId, int, bytes]], rva: int = RSRC_RVA) -> bytes:
    tree: dict = {}
    for t, n, lang, data in resources:
        tree.setdefault(t, {}).setdefault(n, {})[lang] = data

    off = _dir_size(len(tree))
    type_off, name_off, entry_off, str_off, data_off = {}, {}, {}, {}, {}
    for t, names in tree.items():
        type_off[t] = off
        off += _dir_size(len(names))
    for t, names in tree.items():
        for n, langs in names.items():
            name_off[t, n] = off
            off += _dir_size(len(langs))
    for t, names in tree.items():
        for n, langs in names.items():
            for lang in langs:
                entry_off[t, n, lang] = off
                off += 16
    for t, names in tree.items():
        for s in (t, *names):
            if isinstance(s, str) and s not in str_off:
                str_off[s] = off
                off += 2 + 2 * len(s)
    off = (off + 3) & ~3
    for t, names in tree.items():
        for n, langs in names.items():
            for lang, data in langs.items():
                data_off[t, n, lang] = off
                off = (off + len(data) + 3) & ~3

    buf = bytearray(off)

    def put_dir(o: int, items: list[tuple[ResId, int, bool]]) -> None:
        items = sorted(items, key=lambda x: not isinstance(x[0], str))  # named entries first
        named = sum(isinstance(i, str) for i, _, _ in items)
        struct.pack_into("<IIHHHH", buf, o, 0, 0, 0, 0, named, len(items) - named)
        for k, (ident, target, is_dir) in enumerate(items):
            idv = (0x80000000 | str_off[ident]) if isinstance(ident, str) else ident
            struct.pack_into("<II", buf, o + 16 + 8 * k, idv, target | (0x80000000 if is_dir else 0))

    put_dir(0, [(t, type_off[t], True) for t in tree])
    for t, names in tree.items():
        put_dir(type_off[t], [(n, name_off[t, n], True) for n in names])
        for n, langs in names.items():
            put_dir(name_off[t, n], [(lang, entry_off[t, n, lang], False) for lang in langs])
            for lang, data in langs.items():
                struct.pack_into("<IIII", buf, entry_off[t, n, lang], rva + data_off[t, n, lang], len(data), 0, 0)
                buf[data_off[t, n, lang]:data_off[t, n, lang] + len(data)] = data
    for s, o in str_off.items():
        struct.pack_into("<H", buf, o, len(s))
        buf[o + 2:o + 2 + 2 * len(s)] = s.encode("utf-16-le")
    return bytes(buf)


def build_pe(resources: Iterable[tuple[ResId, ResId, int, bytes]], timestamp: int = 1234567890) -> bytes:
    rsrc = build_rsrc(resources)
    out = bytearray(RSRC_FILE_OFFSET)
    out[0:2] = b"MZ"
    struct.pack_into("<I", out, 0x3C, 0x40)
    out[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<HHIIIHH", out, 0x44, 0x14C, 1, timestamp, 0, 0, 224, 0x0102)
    opt = 0x58
    struct.pack_into("<H", out, opt, 0x10B)
    struct.pack_into("<I", out, opt + 92, 16)
    struct.pack_into("<II", out, opt + 96 + 8 * 2, RSRC_RVA, len(rsrc))
    struct.pack_into("<8sIIII", out, opt + 224, b".rsrc", len(rsrc), RSRC_RVA, len(rsrc), RSRC_FILE_OFFSET)
    return bytes(out) + rsrc


def string_block(text: str, slot: int = 1) -> bytes:
    """An RT_STRING block the way AllocStringResourceFormat(text, slot) lays it out."""
    return b"\0\0" * slot + struct.pack("<H", len(text)) + text.encode("utf-16-le")


def construct_resources(n_plugins: int = 2, preview: bool = False,
                        declared: int | None = None) -> list[tuple[ResId, ResId, int, bytes]]:
    """A minimal resource set shaped like a Construct Classic export."""
    res: list[tuple[ResId, ResId, int, bytes]] = [
        ("APPBLOCK", 997, 0, b"app"),
        ("LEVELBLOCK", 998, 0, b"level"),
        ("EVENTBLOCK", 999, 0, b"events"),
        ("IMAGEBLOCK", 995, 0, b"\0\0\0\0"),
        ("HLSL", 994, 0, b"hlsl"),
        ("MENUBLOCK", 993, 0, b"\0\0\0\0"),
        (6, 1, 0, string_block(str(n_plugins if declared is None else declared))),
    ]
    for i in range(n_plugins):
        if preview:
            res.append((6, 1000 + i, 0, string_block(f"Plugin{i}.csx")))
        else:
            res.append(("DLLBLOCK", 1000 + i, 0, b"not a dll %d" % i))
    return res
