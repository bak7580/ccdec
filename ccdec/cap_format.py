"""The .cap project file format: one bidirectional description for reading and writing.

A .cap is the 13-byte signature ``"Construct1.7\\0"`` followed by an MFC ``CArchive``
stream written by ``CApplication::Serialize`` and the ``Serialize`` methods it calls
(FORMAT.md section 10).  Like the MFC code it mirrors, every function here takes an
:class:`Archive` that is either loading or storing and a plain ``dict`` node, and
names each field exactly once - so the reader and the writer cannot drift apart.

Nodes are dicts so that files written by older editors keep their class versions:
``_version`` is stored per node and honoured when writing, which makes
read -> write reproduce the original file byte for byte.

Source of every layout: vendor/construct-src/IDE (function named in each docstring).
Input is untrusted: reads are bounds-checked, counts are validated against the bytes
left, nesting is capped.
"""

from __future__ import annotations

import struct
from typing import Callable, Optional

SIGNATURE = b"Construct1.7\0"
MAX_DEPTH = 200

EVENT, COMMENT, GROUP, INCLUDE, SCRIPT = 0, 1, 2, 3, 4      # CEditorEvent::m_type

#: class versions written by Construct Classic r2 (source r321)
LATEST = {
    "CApplication": 18, "CLayout": 5, "CLayer": 7, "CObj": 3, "CObjType": 4, "CAnimation": 3,
    "CImageResource": 2, "CBinaryResource": 2, "CIconResource": 1, "CMenuResource": 1,
    "CMenuResourceItem": 1, "CFamily": 1, "CFamilyShare": 1, "CEffect": 1, "CEditorEvent": 2,
    "CEditorCondition": 2, "CEditorAction": 2, "CEditorParam": 1,
}


class CapError(Exception):
    def __init__(self, message: str, offset: Optional[int] = None) -> None:
        self.offset = offset
        super().__init__(message if offset is None else f"{message} (at .cap offset {offset:#x})")


class WideString(str):
    """A CString that was stored as UTF-16 (MFC's 0xFFFE marker); kept so it is rewritten the same way."""


class Archive:
    """MFC CArchive look-alike: the same calls load from ``data`` or append to ``out``."""

    def __init__(self, data: Optional[bytes] = None, encoding: str = "cp1252") -> None:
        self.loading = data is not None
        self.data = memoryview(data) if data is not None else memoryview(b"")
        self.pos = 0
        self.out = bytearray()
        self.encoding = encoding
        self.depth = 0

    # -- primitives ---------------------------------------------------------------
    def _need(self, n: int, what: str) -> None:
        if n < 0 or self.pos + n > len(self.data):
            raise CapError(f"unexpected end of file while reading {what}", self.pos)

    def _prim(self, d: dict, key: str, fmt: str, size: int) -> None:
        if self.loading:
            self._need(size, key)
            (d[key],) = struct.unpack_from(fmt, self.data, self.pos)
            self.pos += size
        else:
            try:
                self.out += struct.pack(fmt, d[key])
            except (struct.error, KeyError, TypeError) as e:
                raise CapError(f"cannot write field {key!r}: {e}") from None

    def i32(self, d: dict, key: str) -> None:       # int, long, BOOL, enum
        self._prim(d, key, "<i", 4)

    def u32(self, d: dict, key: str) -> None:       # COLORREF, DWORD, size_t
        self._prim(d, key, "<I", 4)

    def u16(self, d: dict, key: str) -> None:
        self._prim(d, key, "<H", 2)

    def u8(self, d: dict, key: str) -> None:        # C++ bool: kept as the raw byte
        self._prim(d, key, "<B", 1)

    def f32(self, d: dict, key: str) -> None:
        # Kept as raw bits when they do not survive a float round trip (NaN payloads).
        if self.loading:
            self._need(4, key)
            raw = bytes(self.data[self.pos:self.pos + 4])
            (v,) = struct.unpack("<f", raw)
            d[key] = v if struct.pack("<f", v) == raw else raw
            self.pos += 4
        else:
            v = d[key]
            self.out += v if isinstance(v, bytes) else struct.pack("<f", v)

    def rect(self, d: dict, key: str) -> None:      # CRect: left, top, right, bottom
        if self.loading:
            self._need(16, key)
            d[key] = list(struct.unpack_from("<4i", self.data, self.pos))
            self.pos += 16
        else:
            self.out += struct.pack("<4i", *d[key])

    def raw(self, d: dict, key: str, size: int) -> None:
        if self.loading:
            self._need(size, key)
            d[key] = bytes(self.data[self.pos:self.pos + size])
            self.pos += size
        else:
            self.out += d[key]

    def blob(self, d: dict, key: str) -> None:
        """``int size`` + bytes (bitmaps, PNG files, plugin property data)."""
        if self.loading:
            n = self._count(key)
            self.raw(d, key, n)
        else:
            self.out += struct.pack("<i", len(d[key])) + d[key]

    def cstr(self, d: dict, key: str) -> None:
        """MFC CString: BYTE length, 0xFF + WORD, 0xFF 0xFFFF + DWORD; 0xFF 0xFFFE prefixes UTF-16."""
        if self.loading:
            d[key] = self._read_cstr(key)
        else:
            self._write_cstr(d[key], key)

    def _read_cstr(self, what: str) -> str:
        wide = False
        self._need(1, what)
        n = self.data[self.pos]
        self.pos += 1
        if n == 0xFF:
            self._need(2, what)
            (n,) = struct.unpack_from("<H", self.data, self.pos)
            self.pos += 2
            if n == 0xFFFE:
                wide = True
                self._need(1, what)
                n = self.data[self.pos]
                self.pos += 1
                if n == 0xFF:
                    self._need(2, what)
                    (n,) = struct.unpack_from("<H", self.data, self.pos)
                    self.pos += 2
            if n == 0xFFFF:
                self._need(4, what)
                (n,) = struct.unpack_from("<I", self.data, self.pos)
                self.pos += 4
        size = n * 2 if wide else n
        self._need(size, what)
        raw = bytes(self.data[self.pos:self.pos + size])
        self.pos += size
        if wide:
            return WideString(raw.decode("utf-16-le", "surrogatepass"))
        return raw.decode(self.encoding, "surrogateescape")

    def _write_cstr(self, s: str, what: str) -> None:
        if not isinstance(s, str):
            raise CapError(f"cannot write field {what!r}: expected a string, got {type(s).__name__}")
        if isinstance(s, WideString):
            raw = s.encode("utf-16-le", "surrogatepass")
            n = len(raw) // 2
            self.out += b"\xff\xfe\xff"
        else:
            raw = s.encode(self.encoding, "surrogateescape")
            n = len(raw)
        if n < 0xFF:
            self.out.append(n)
        elif n < 0xFFFE:
            self.out += b"\xff" + struct.pack("<H", n)
        else:
            self.out += b"\xff\xff\xff" + struct.pack("<I", n)
        self.out += raw

    # -- structure ----------------------------------------------------------------
    def _count(self, what: str, min_item: int = 1) -> int:
        self._need(4, what)
        (n,) = struct.unpack_from("<i", self.data, self.pos)
        if n < 0 or n * min_item > len(self.data) - self.pos - 4:
            raise CapError(f"implausible {what} count/size {n}", self.pos)
        self.pos += 4
        return n

    def seq(self, d: dict, key: str, item: Callable[["Archive", dict], None], min_item: int = 1) -> None:
        """``int count`` followed by that many items described by ``item``."""
        if self.loading:
            n = self._count(key, min_item)
            out = []
            for _ in range(n):
                node: dict = {}
                item(self, node)
                out.append(node)
            d[key] = out
        else:
            self.out += struct.pack("<i", len(d[key]))
            for node in d[key]:
                item(self, node)

    def values(self, d: dict, key: str, fmt: str) -> None:
        """``int count`` + plain values (``fmt`` is one struct code, or "s" for CStrings)."""
        if self.loading:
            n = self._count(key)
            if fmt == "s":
                d[key] = [self._read_cstr(key) for _ in range(n)]
            else:
                size = struct.calcsize("<" + fmt)
                self._need(n * size, key)
                d[key] = list(struct.unpack_from(f"<{n}{fmt}", self.data, self.pos))
                self.pos += n * size
        else:
            self.out += struct.pack("<i", len(d[key]))
            if fmt == "s":
                for s in d[key]:
                    self._write_cstr(s, key)
            else:
                self.out += struct.pack(f"<{len(d[key])}{fmt}", *d[key])

    def header(self, d: dict, name: str) -> int:
        """SerializeClassHeader: class name + version.  Returns the version in effect."""
        if self.loading:
            start = self.pos
            got = self._read_cstr(f"{name} class header")
            if got != name:
                raise CapError(f"expected class {name!r}, found {got[:40]!r}", start)
            self._need(4, "class version")
            (d["_version"],) = struct.unpack_from("<i", self.data, self.pos)
            self.pos += 4
            if not 0 < d["_version"] <= LATEST[name]:
                raise CapError(f"{name} version {d['_version']} is not supported "
                               f"(newest known: {LATEST[name]})", start)
        else:
            d.setdefault("_version", LATEST[name])
            self._write_cstr(name, name)
            self.out += struct.pack("<i", d["_version"])
        return d["_version"]

    def padding(self, d: dict, strings: int, ints: int) -> None:
        """SerializeClassPadding: reserved empty strings and zero ints."""
        if self.loading:
            s = [self._read_cstr("padding") for _ in range(strings)]
            self._need(4 * ints, "padding")
            i = list(struct.unpack_from(f"<{ints}i", self.data, self.pos))
            self.pos += 4 * ints
            if any(s) or any(i):
                d["_padding"] = [s, i]
        else:
            s, i = d.get("_padding") or ([""] * strings, [0] * ints)
            for x in s:
                self._write_cstr(x, "padding")
            self.out += struct.pack(f"<{ints}i", *i)

    def nested(self) -> "Archive":
        self.depth += 1
        if self.depth > MAX_DEPTH:
            raise CapError("structure nested too deeply", self.pos)
        return self

    def leave(self) -> None:
        self.depth -= 1


# --- small records -----------------------------------------------------------------
def effect_parameter(ar: Archive, d: dict) -> None:
    ar.i32(d, "type")
    ar.cstr(d, "variable_name")
    ar.cstr(d, "value")
    ar.cstr(d, "name")
    ar.cstr(d, "description")


def effect(ar: Archive, d: dict) -> None:
    """Structure/ceffect.h :: CEffect::Serialize"""
    ar.header(d, "CEffect")
    ar.i32(d, "id")
    ar.cstr(d, "file")
    ar.cstr(d, "name")
    ar.f32(d, "version")
    ar.i32(d, "disable_mode")
    ar.seq(d, "params", effect_parameter, 8)


def behavior(ar: Archive, d: dict) -> None:
    """Structure/CMovement.h :: CBehavior::Serialize (no class header)"""
    ar.cstr(d, "plugin")
    ar.i32(d, "id")
    ar.cstr(d, "name")
    ar.blob(d, "data")


def key_collection(ar: Archive, d: dict, key: str, typed: bool) -> None:
    """Structure/CLayout.cpp :: KeyCollection::Serialize(ar, bNew)"""
    def item(ar: Archive, k: dict) -> None:
        ar.cstr(k, "name")
        if typed:
            ar.i32(k, "type")
        if k.get("type", 0) == 0:
            ar.i32(k, "value")
        else:
            ar.cstr(k, "value")
    ar.seq(d, key, item, 5)


# --- objects -----------------------------------------------------------------------
def obj(ar: Archive, d: dict) -> None:
    """Structure/CObject.cpp :: CObj::Serialize"""
    ar.nested()
    v = ar.header(d, "CObj")
    for k in ("x", "y", "width", "height", "origin_x", "origin_y", "angle"):
        ar.f32(d, k)
    if v >= 2:
        ar.u32(d, "filter")
    if v >= 3:
        ar.i32(d, "is_group_flag")
    if not d.get("is_group_flag", 0):
        ar.cstr(d, "plugin")
    ar.i32(d, "instance_id")
    ar.i32(d, "object_type")
    for k in ("locked", "visible", "allow_size", "draw", "show_angles", "in_group", "is_group", "group_open"):
        ar.i32(d, k)
    ar.i32(d, "edit_pointer")           # a stale in-memory pointer the editor writes; ignored on load
    if d["in_group"]:
        ar.i32(d, "group_id")
    if d["is_group"]:
        def member(ar: Archive, g: dict) -> None:
            ar.i32(g, "id")
            g.setdefault("object", {})
            obj(ar, g["object"])
        ar.seq(d, "group_objects", member, 40)
    ar.cstr(d, "temp_name")
    if not d["is_group"]:
        ar.blob(d, "data")              # plugin EditExt::Serialize output, same bytes as in the exe

    def value(ar: Archive, x: dict) -> None:
        ar.i32(x, "id")
        ar.cstr(x, "value")
    ar.seq(d, "values", value, 5)

    def expand(ar: Archive, x: dict) -> None:
        ar.cstr(x, "name")
        ar.u8(x, "expanded")
    ar.seq(d, "expand_states", expand, 2)
    ar.padding(d, 5, 10)
    ar.leave()


def layer(ar: Archive, d: dict) -> None:
    """Structure/CLayer.cpp :: CLayer::Serialize"""
    v = ar.header(d, "CLayer")
    ar.i32(d, "id")
    ar.i32(d, "type")
    ar.cstr(d, "name")
    ar.i32(d, "state")
    ar.f32(d, "angle")
    ar.u32(d, "color")
    ar.f32(d, "scroll_x_rate")
    ar.f32(d, "scroll_y_rate")
    ar.f32(d, "opacity")
    ar.u8(d, "clear_background")
    ar.u32(d, "background_color")
    ar.f32(d, "zoom_x")
    ar.f32(d, "zoom_y")
    if v >= 2:
        ar.u8(d, "force_own_texture")
    if v >= 3:
        for k in ("zoom_x_rate", "zoom_y_rate", "scroll_x", "scroll_y"):
            ar.f32(d, k)
    if v >= 4:
        ar.i32(d, "inherit_layer_id")
    if v >= 5:
        ar.i32(d, "sampler")
    if v >= 6:
        ar.u8(d, "enable_3d")
    if v >= 7:
        ar.u8(d, "clear_depth_buffer")
    ar.seq(d, "effects", effect, 20)
    ar.values(d, "z_order", "i")


def layout(ar: Archive, d: dict) -> None:
    """Structure/CLayout.cpp :: CLayout::Serialize"""
    v = ar.header(d, "CLayout")
    ar.i32(d, "id")
    ar.i32(d, "width")
    ar.i32(d, "height")
    ar.cstr(d, "name")
    ar.u32(d, "color")
    ar.i32(d, "unbounded_scrolling")
    ar.u8(d, "application_background")

    def keyed_obj(ar: Archive, x: dict) -> None:
        ar.i32(x, "key")
        x.setdefault("object", {})
        obj(ar, x["object"])
    ar.seq(d, "objects", keyed_obj, 40)
    ar.seq(d, "layers", layer, 40)
    ar.i32(d, "event_sheet_id")
    key_collection(ar, d, "keys", typed=v >= 2)
    if v >= 3:
        for k in ("grid", "snap_movements", "snap_resize"):
            ar.u8(d, k)
        ar.i32(d, "grid_width")
        ar.i32(d, "grid_height")
    if v >= 5:
        ar.i32(d, "texture_loading")
    ar.padding(d, 10, 15)


def private_value(ar: Archive, d: dict) -> None:
    ar.cstr(d, "name")
    ar.i32(d, "type")
    ar.i32(d, "id")


def object_type(ar: Archive, d: dict) -> None:
    """Structure/cobjecttype.cpp :: CObjType::Serialize"""
    v = ar.header(d, "CObjType")
    ar.cstr(d, "name")
    ar.i32(d, "id")
    ar.cstr(d, "plugin")
    ar.blob(d, "image_bmp")             # SerializeBitmap: BMP file or empty
    ar.blob(d, "small_image_bmp")
    ar.seq(d, "behaviors", behavior, 13)
    ar.seq(d, "effects", effect, 20)
    ar.values(d, "traits", "s")
    # count, VariableID, then the entries
    if ar.loading:
        n = ar._count("private variables", 9)
        ar.i32(d, "variable_id_counter")
        d["private_variables"] = []
        for _ in range(n):
            x: dict = {}
            private_value(ar, x)
            d["private_variables"].append(x)
    else:
        ar.out += struct.pack("<i", len(d["private_variables"]))
        ar.i32(d, "variable_id_counter")
        for x in d["private_variables"]:
            private_value(ar, x)
    ar.u8(d, "global")
    ar.i32(d, "destroy_when")
    if v >= 2:
        ar.u8(d, "show_in_event_sheet_editor")
    if v >= 3:
        ar.cstr(d, "category")
    if v >= 4:
        ar.cstr(d, "folder")
    ar.padding(d, 5, 15)


def family_share(ar: Archive, d: dict) -> None:
    """Structure/CFamily.cpp :: FamilyShare::Serialize"""
    ar.header(d, "CFamilyShare")
    ar.i32(d, "id")
    ar.cstr(d, "name")

    def ref(ar: Archive, x: dict) -> None:
        ar.i32(x, "item_id")
        ar.i32(x, "object")
    ar.seq(d, "references", ref, 8)


def family(ar: Archive, d: dict) -> None:
    """Structure/CFamily.cpp :: Family::Serialize"""
    ar.header(d, "CFamily")
    ar.i32(d, "id")
    ar.cstr(d, "name")
    ar.cstr(d, "plugin")
    ar.blob(d, "image_bmp")
    ar.blob(d, "small_image_bmp")
    ar.values(d, "object_types", "i")
    ar.seq(d, "behaviors", family_share, 20)
    ar.seq(d, "variables", family_share, 20)
    ar.seq(d, "effects", family_share, 20)


def animation(ar: Archive, d: dict) -> None:
    """Structure/CAnimation.cpp :: CAnimation::Serialize"""
    ar.nested()
    v = ar.header(d, "CAnimation")
    ar.i32(d, "id")
    ar.cstr(d, "name")
    if v >= 3:
        ar.f32(d, "speed")
    else:
        ar.i32(d, "speed")
    ar.i32(d, "repeat")
    ar.i32(d, "repeat_to")
    ar.i32(d, "forever")
    ar.i32(d, "pingpong")
    ar.u8(d, "is_angle")
    ar.f32(d, "angle")
    ar.u8(d, "expanded")
    ar.values(d, "images", "i")
    ar.values(d, "frame_times", "f")
    ar.seq(d, "sub_animations", animation, 40)
    if v >= 2:
        ar.i32(d, "tag")
    ar.leave()


def image(ar: Archive, d: dict) -> None:
    """Structure/CImageResource.cpp :: CImageResource::Serialize"""
    v = ar.header(d, "CImageResource")
    ar.cstr(d, "name")
    ar.i32(d, "id")
    ar.cstr(d, "dimensions")
    ar.i32(d, "hotspot_x")
    ar.i32(d, "hotspot_y")

    def point(ar: Archive, x: dict) -> None:
        ar.i32(x, "x")
        ar.i32(x, "y")
        ar.cstr(x, "name")
    ar.seq(d, "action_points", point, 9)
    ar.blob(d, "png")
    if v >= 2:
        # collision image: size, then (format, data) only when size > 0
        if ar.loading:
            n = ar._count("collision image")
            if n:
                ar.i32(d, "collision_format")
            ar.raw(d, "collision", n)
        else:
            ar.out += struct.pack("<i", len(d.get("collision", b"")))
            if d.get("collision"):
                ar.i32(d, "collision_format")
                ar.out += d["collision"]


def binary_resource(ar: Archive, d: dict) -> None:
    """Structure/Resources.cpp :: BinaryResource::Serialize - a path on the author's disk, no content"""
    v = ar.header(d, "CBinaryResource")
    ar.cstr(d, "name")
    ar.i32(d, "type")
    ar.cstr(d, "path")
    if v >= 2:
        ar.i32(d, "id")


def icon_resource(ar: Archive, d: dict) -> None:
    ar.header(d, "CIconResource")
    ar.cstr(d, "name")
    ar.i32(d, "type")
    ar.blob(d, "png")


def menu_item(ar: Archive, d: dict) -> None:
    ar.nested()
    ar.header(d, "CMenuResourceItem")
    ar.cstr(d, "text")
    ar.i32(d, "id")
    ar.seq(d, "items", menu_item, 20)
    ar.leave()


def menu_resource(ar: Archive, d: dict) -> None:
    ar.header(d, "CMenuResource")
    ar.cstr(d, "name")
    ar.i32(d, "type")
    ar.seq(d, "items", menu_item, 20)


def container(ar: Archive, d: dict) -> None:
    """Structure/CContainer.h :: CContainer::Serialize"""
    ar.cstr(d, "name")
    ar.values(d, "object_types", "i")


def transition(ar: Archive, d: dict) -> None:
    """Structure/CTransition.h :: CTransition::Serialize"""
    ar.cstr(d, "name")
    ar.cstr(d, "file")
    ar.seq(d, "params", effect_parameter, 8)


def global_variable(ar: Archive, d: dict) -> None:
    ar.cstr(d, "name")
    ar.i32(d, "id")
    ar.i32(d, "type")
    ar.cstr(d, "value")


# --- events ------------------------------------------------------------------------
def token(ar: Archive, d: dict) -> None:
    ar.i32(d, "type")
    ar.u16(d, "length")
    ar.i32(d, "owner")
    ar.cstr(d, "text")
    ar.i32(d, "id")
    ar.i32(d, "subtype")


def param(ar: Archive, d: dict) -> None:
    """Editors/EditorParam.cpp :: CEditorParam::Serialize"""
    ar.header(d, "CEditorParam")
    ar.i32(d, "type")
    ar.cstr(d, "reserved")
    ar.i32(d, "oid")
    ar.seq(d, "tokens", token, 19)


def condition(ar: Archive, d: dict) -> None:
    """Editors/EditorCondition.cpp :: CEditorCondition::Serialize"""
    v = ar.header(d, "CEditorCondition")
    ar.i32(d, "movement_id")
    ar.i32(d, "id")
    ar.i32(d, "oid")
    ar.u8(d, "negated")
    ar.u8(d, "family")
    ar.cstr(d, "family_name")
    ar.rect(d, "rect")
    ar.i32(d, "select")
    ar.i32(d, "select2")
    ar.f32(d, "space")
    ar.cstr(d, "object_text")
    ar.cstr(d, "script")
    ar.seq(d, "params", param, 20)
    if d["oid"] != -1:
        ar.cstr(d, "plugin")
    if v >= 2:
        ar.u8(d, "enabled")


def action(ar: Archive, d: dict) -> None:
    """Editors/EditorAction.cpp :: CEditorAction::Serialize"""
    v = ar.header(d, "CEditorAction")
    ar.u8(d, "family")
    ar.cstr(d, "family_name")
    ar.i32(d, "id")
    ar.i32(d, "oid")
    ar.rect(d, "rect")
    ar.i32(d, "select")
    ar.f32(d, "space")
    ar.cstr(d, "object_text")
    ar.cstr(d, "script")
    ar.i32(d, "movement_id")
    ar.seq(d, "params", param, 20)
    ar.cstr(d, "plugin")
    if v >= 2:
        ar.u8(d, "enabled")


def event(ar: Archive, d: dict) -> None:
    """Editors/EditorEvent.cpp :: CEditorEvent::Serialize"""
    ar.nested()
    v = ar.header(d, "CEditorEvent")
    ar.i32(d, "type")
    ar.rect(d, "handle")
    ar.i32(d, "display_action_type")
    ar.i32(d, "display_condition_type")
    ar.i32(d, "display_event_type")
    ar.i32(d, "select")
    ar.f32(d, "space")
    ar.u8(d, "bookmarked")
    if v >= 2:
        ar.u8(d, "enabled")
    kind = d["type"]
    if kind == EVENT:
        ar.i32(d, "open")
        ar.i32(d, "breakpoint")
        ar.seq(d, "conditions", condition, 60)
        ar.seq(d, "actions", action, 60)
        ar.seq(d, "events", event, 40)
    elif kind in (COMMENT, SCRIPT):
        ar.cstr(d, "text")
        ar.u32(d, "background")
        ar.u32(d, "text_color")
        ar.cstr(d, "font_name")
        ar.i32(d, "font_size")
        for k in ("bold", "italic", "strike", "underline"):
            ar.i32(d, k)
    elif kind == GROUP:
        ar.i32(d, "open")
        ar.u8(d, "active")
        ar.cstr(d, "description")
        ar.cstr(d, "title")
        ar.seq(d, "events", event, 40)
    elif kind == INCLUDE:
        ar.i32(d, "event_sheet")
    else:
        raise CapError(f"unknown event type {kind}", ar.pos if ar.loading else None)
    ar.leave()


def event_sheet(ar: Archive, d: dict) -> None:
    """Structure/EventSheet.h :: EventSheet::Serialize + CEditorHeader::Serialize"""
    ar.i32(d, "id")
    ar.cstr(d, "name")
    ar.seq(d, "events", event, 40)
    ar.i32(d, "header_split")

    def header_object(ar: Archive, x: dict) -> None:
        ar.i32(x, "width")
        ar.i32(x, "oid")
        ar.u8(x, "family")
    ar.seq(d, "header_objects", header_object, 9)


# --- application -------------------------------------------------------------------
def application(ar: Archive, d: dict) -> None:
    """Structure/capplication.cpp :: CApplication::Serialize"""
    v = ar.header(d, "CApplication")
    ar.cstr(d, "name")
    ar.cstr(d, "creator")
    ar.cstr(d, "creator2")              # the writer stores the creator twice
    for k in ("next_object_id", "app_id", "next_layer_id", "next_image_id", "next_animation_id",
              "next_event_sheet_id", "next_layout_id"):
        ar.i32(d, k)
    ar.cstr(d, "file_path")
    ar.i32(d, "sampler")
    ar.i32(d, "window_width")
    ar.i32(d, "window_height")
    if v >= 18:
        ar.f32(d, "eye_distance")
    ar.i32(d, "run_without_ps")
    ar.u8(d, "disable_windows_key")
    ar.i32(d, "fps_mode")
    ar.i32(d, "fps")
    ar.u8(d, "fullscreen")
    ar.i32(d, "show_menu")
    ar.u8(d, "unused1")
    ar.u8(d, "hide_maximize")
    ar.u8(d, "hide_minimize")
    ar.u8(d, "unused2")
    ar.i32(d, "unused3")
    ar.u8(d, "unused4")
    ar.u8(d, "unused5")
    ar.u8(d, "unused6")
    ar.i32(d, "fps_in_caption")
    ar.i32(d, "unused7")
    ar.i32(d, "use_python")
    ar.i32(d, "reserved4")
    ar.i32(d, "next_variable_id")
    ar.i32(d, "runtime")                # 0 = Application, 1 = DirectX 9
    ar.i32(d, "simulate_shaders")
    if v >= 4:
        ar.u8(d, "use_motion_blur")
        ar.i32(d, "motion_blur_steps")
    if v >= 6:
        ar.i32(d, "text_rendering_mode")
    if v >= 7:
        ar.u8(d, "override_timedelta")
        ar.f32(d, "time_delta_override")
    if v >= 8:
        ar.i32(d, "created_with")
    if v >= 9:
        def plugin(ar: Archive, x: dict) -> None:
            ar.cstr(x, "name")
            ar.cstr(x, "file")
        ar.seq(d, "plugins", plugin, 2)
    if v >= 10:
        ar.u8(d, "caption")
        ar.u8(d, "minimize_box")
        ar.i32(d, "resizable")
    if v >= 11:
        ar.u8(d, "maximize_box")
    if v >= 12:
        ar.f32(d, "minimum_fps")
    if v >= 15:
        ar.i32(d, "multisamples")
    if v >= 17:
        ar.i32(d, "texture_loading")

    ar.seq(d, "layouts", layout, 60)
    ar.seq(d, "animations", animation, 40)
    ar.seq(d, "images", image, 30)
    if v <= 13:
        ar.seq(d, "binary_resources", binary_resource, 20)
    else:
        ar.seq(d, "files", binary_resource, 20)
        ar.seq(d, "sounds", binary_resource, 20)
        ar.seq(d, "music", binary_resource, 20)
    ar.seq(d, "icons", icon_resource, 20)
    ar.seq(d, "menus", menu_resource, 20)

    def keyed_type(ar: Archive, x: dict) -> None:
        ar.i32(x, "key")
        x.setdefault("type", {})
        object_type(ar, x["type"])
    ar.seq(d, "object_types", keyed_type, 60)
    ar.values(d, "traits", "s")
    ar.seq(d, "families", family, 30)
    ar.seq(d, "global_variables", global_variable, 10)
    ar.seq(d, "containers", container, 5)
    d.setdefault("system", {})
    object_type(ar, d["system"])
    ar.seq(d, "transitions", transition, 6)
    ar.seq(d, "event_sheets", event_sheet, 17)

    def control(ar: Archive, x: dict) -> None:
        ar.cstr(x, "name")
        ar.i32(x, "combo_index")
        ar.i32(x, "vk")
        if v >= 16:
            ar.i32(x, "player")
    ar.seq(d, "controls", control, 9)
    ar.i32(d, "unused_tree")
    # The loader passes bNew only for version 5 although the writer always stores typed
    # keys; with a non-empty collection the editor itself would misread its own file.
    key_collection(ar, d, "global_keys", typed=v == 5)
    if v >= 3:
        ar.values(d, "python_modules", "s")
    if v >= 13:
        ar.values(d, "object_folders", "s")
    ar.padding(d, 30, 20)


def read_cap(data: bytes, encoding: str = "cp1252") -> dict:
    """Parse a .cap file into a tree of dicts.  Raises :class:`CapError`."""
    if not data.startswith(SIGNATURE):
        if data[:9] == b"Construct":
            raise CapError("this .cap was saved by a Construct version older than the 'Construct1.7' format")
        raise CapError("not a Construct Classic .cap file")
    ar = Archive(data, encoding)
    ar.pos = len(SIGNATURE)
    app: dict = {}
    try:
        application(ar, app)
    except RecursionError:
        raise CapError("structure nested too deeply", ar.pos) from None
    if ar.pos != len(data):
        raise CapError(f"{len(data) - ar.pos} unparsed bytes after the application", ar.pos)
    return app


def write_cap(app: dict, encoding: str = "cp1252") -> bytes:
    """Serialize a tree produced by :func:`read_cap` (or built by cap_writer)."""
    ar = Archive(None, encoding)
    ar.out += SIGNATURE
    try:
        application(ar, app)
    except KeyError as e:
        raise CapError(f"missing field {e.args[0]!r} while writing the .cap") from None
    return bytes(ar.out)
