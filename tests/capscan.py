"""Test helper: pull ground-truth records out of a reference .cap by signature.

This is *not* the .cap reader (that is Phase 4).  It locates self-describing
records - PNG files and ``CEditorParam`` objects, whose class header is a unique
string (IDE/Structure/ObjectFunctions.cpp :: SerializeClassHeader) - and decodes
only those, following IDE/Editors/EditorParam.cpp :: CEditorParam::Serialize.
"""

from __future__ import annotations

import struct
import sys
import os
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ccdec import model as m  # noqa: E402
from ccdec.expressions import token_text  # noqa: E402


def squash(text: str) -> str:
    """Comparison key: no whitespace, case-insensitive (Construct names are)."""
    return "".join(text.split()).lower()

T_WHITESPACE, T_COLOR = 52, 53            # IDE-only token types (IDE/Editors/Expressions/CExpression.h)


def mfc_cstring(b: bytes, p: int, encoding: str) -> tuple[str, int]:
    """MFC CArchive CString (ANSI build): BYTE len, or 0xFF + WORD, or 0xFF 0xFFFF + DWORD."""
    n = b[p]
    p += 1
    if n == 0xFF:
        (n,) = struct.unpack_from("<H", b, p)
        p += 2
        if n == 0xFFFF:
            (n,) = struct.unpack_from("<I", b, p)
            p += 4
    return b[p:p + n].decode(encoding, "surrogateescape"), p + n


def pngs(cap: bytes) -> list[bytes]:
    out, i = [], 0
    while True:
        i = cap.find(b"\x89PNG\r\n\x1a\n", i)
        if i < 0:
            return out
        j = cap.find(b"IEND", i)
        out.append(cap[i:j + 8])
        i = j + 8


@dataclass
class CapParam:
    type: int
    oid: int
    text: str            # canonical source text, see editor_params()


def editor_params(cap: bytes, encoding: str) -> list[CapParam]:
    sig = b"\x0cCEditorParam" + struct.pack("<i", 1)
    out, i = [], 0
    while True:
        i = cap.find(sig, i)
        if i < 0:
            return out
        p = i + len(sig)
        ptype, = struct.unpack_from("<i", cap, p)
        _, p = mfc_cstring(cap, p + 4, encoding)
        oid, size = struct.unpack_from("<ii", cap, p)
        p += 8
        # Editor-mode tokens are not the exporter's tokens (whitespace tokens change how
        # a "-" is classified, colours are T_COLOR, ...), so rebuild the source text and
        # spell operators/functions canonically; callers compare without whitespace.
        parts: list[str] = []
        for _ in range(size):
            t, _length, _owner = struct.unpack_from("<iHi", cap, p)
            s, p = mfc_cstring(cap, p + 10, encoding)
            ident, _tsub = struct.unpack_from("<ii", cap, p)
            p += 8
            if t == T_WHITESPACE:
                parts.append(" ")
            elif t == T_COLOR:
                parts.append(str(ident))
            elif t in (m.T_INTEGER, m.T_FLOAT, m.T_IDENTIFIER):
                parts.append(s)
            elif t == m.T_STRINGLITERAL:
                parts.append('"' + s.replace("\n", "$n").replace("\t", "$t") + '"')   # "" escapes kept as typed
            elif t == m.T_VARIABLENAME:
                parts.append("'" + s + "'")
            else:
                parts.append(f" {token_text(m.Token(t))} ")
        out.append(CapParam(ptype, oid, "".join(parts)))
        i = p
