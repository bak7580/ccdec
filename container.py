"""Phase 1: locate the project data inside a Construct Classic exported .exe.

A Construct Classic export is the stock runtime executable with the project data
appended as plain PE resources (see FORMAT.md, section 1).  Nothing is compressed
or encrypted, so "extraction" is a strict, bounds-checked walk of the PE resource
directory.

The input file is untrusted.  This module never executes or loads it; it only
reads bytes, and every offset and size is validated against the file length.
"""

from __future__ import annotations

import hashlib
import logging
import os
import struct
from dataclasses import dataclass, field
from typing import Optional, Union

log = logging.getLogger(__name__)

ResId = Union[int, str]

# --- limits (untrusted input) -------------------------------------------------
MAX_FILE_SIZE = 1 << 30          # 1 GiB
MAX_SECTIONS = 96                # PE loader limit
MAX_RESOURCES = 65536            # leaves in the resource tree
MAX_RES_DEPTH = 3                # type / name / language
MAX_PLUGINS = 1000               # same sanity limit as CRuntime::UnpackPlugins
MAX_NAME_CHARS = 4096

# --- well-known resources (FORMAT.md, table 1.1) --------------------------------
RT_ICON = 3
RT_STRING = 6
RT_GROUP_ICON = 14

BLOCK_IDS: dict[str, int] = {
    "APPBLOCK": 997,
    "LEVELBLOCK": 998,
    "EVENTBLOCK": 999,
    "IMAGEBLOCK": 995,
    "HLSL": 994,
    "MENUBLOCK": 993,
    "PYTHONLIBS": 992,
}
#: Blocks the runtime opens unconditionally (CRuntime::Initialise).
REQUIRED_BLOCKS = ("APPBLOCK", "LEVELBLOCK", "EVENTBLOCK", "IMAGEBLOCK", "MENUBLOCK")
PLUGIN_BASE_ID = 1000
INSTALLER_TYPE = "RUNTIME"


class ContainerError(Exception):
    """The file is not a readable PE / Construct container.

    ``offset`` is the file offset the problem was detected at, when known.
    """

    def __init__(self, message: str, offset: Optional[int] = None) -> None:
        self.offset = offset
        if offset is not None:
            message = f"{message} (at file offset {offset:#x})"
        super().__init__(message)


@dataclass(frozen=True)
class Resource:
    """One leaf of the PE resource tree."""

    type: ResId
    name: ResId
    lang: int
    offset: int
    size: int
    _buf: bytes = field(repr=False, compare=False)

    @property
    def data(self) -> bytes:
        """The resource bytes (bounds were validated when the tree was walked)."""
        return self._buf[self.offset:self.offset + self.size]

    @property
    def sha1(self) -> str:
        return hashlib.sha1(self.data).hexdigest()

    @property
    def path(self) -> str:
        return f"{self.type}/{self.name}/{self.lang}"


@dataclass(frozen=True)
class Section:
    name: str
    virtual_address: int
    virtual_size: int
    raw_offset: int
    raw_size: int


class PEImage:
    """Minimal read-only PE parser: headers, sections, resources, export name."""

    def __init__(self, data: bytes, origin: str = "<memory>") -> None:
        self.data = data
        self.origin = origin
        self.sections: list[Section] = []
        self._parse_headers()

    # -- low-level helpers ------------------------------------------------------
    def _unpack(self, fmt: str, offset: int, what: str) -> tuple:
        size = struct.calcsize(fmt)
        if offset < 0 or offset + size > len(self.data):
            raise ContainerError(f"truncated file while reading {what}", offset)
        return struct.unpack_from(fmt, self.data, offset)

    def rva_to_offset(self, rva: int, size: int, what: str) -> int:
        """Map an RVA range to a file offset; the whole range must be file-backed."""
        for s in self.sections:
            if s.virtual_address <= rva and rva + size <= s.virtual_address + s.raw_size:
                off = rva - s.virtual_address + s.raw_offset
                if off + size > len(self.data):
                    break
                return off
        raise ContainerError(f"{what}: RVA {rva:#x}+{size:#x} is not backed by file data")

    # -- headers ----------------------------------------------------------------
    def _parse_headers(self) -> None:
        if self.data[:2] != b"MZ":
            raise ContainerError("not a PE file: missing 'MZ' signature", 0)
        (pe,) = self._unpack("<I", 0x3C, "e_lfanew")
        if self._unpack("<4s", pe, "PE signature")[0] != b"PE\0\0":
            raise ContainerError("not a PE file: missing 'PE' signature", pe)
        machine, nsec, timestamp, _, _, opt_size, _ = self._unpack("<HHIIIHH", pe + 4, "COFF header")
        if nsec > MAX_SECTIONS:
            raise ContainerError(f"implausible section count {nsec}", pe + 6)
        opt = pe + 24
        (magic,) = self._unpack("<H", opt, "optional header magic")
        if magic == 0x10B:
            dd_count_off, dd_off = opt + 92, opt + 96
        elif magic == 0x20B:
            dd_count_off, dd_off = opt + 108, opt + 112
        else:
            raise ContainerError(f"unknown optional header magic {magic:#x}", opt)
        self.machine = machine
        self.timestamp = timestamp
        self.is_pe32_plus = magic == 0x20B
        (dd_count,) = self._unpack("<I", dd_count_off, "data directory count")
        self._data_dirs: list[tuple[int, int]] = []
        for i in range(min(dd_count, 16)):
            off = dd_off + 8 * i
            if off + 8 > opt + opt_size:
                break
            self._data_dirs.append(self._unpack("<II", off, "data directory"))  # type: ignore[arg-type]

        sec = opt + opt_size
        for i in range(nsec):
            o = sec + 40 * i
            name, vsize, va, rsize, rptr = self._unpack("<8sIIII", o, "section header")
            if rsize and rptr + rsize > len(self.data):
                log.debug("%s: section %r extends past end of file; clamping", self.origin, name)
                rsize = max(0, len(self.data) - rptr)
            self.sections.append(Section(name.rstrip(b"\0").decode("latin-1"), va, vsize, rptr, rsize))

    @property
    def overlay_offset(self) -> int:
        return max((s.raw_offset + s.raw_size for s in self.sections), default=0)

    @property
    def overlay_size(self) -> int:
        return max(0, len(self.data) - self.overlay_offset)

    def _data_dir(self, index: int) -> tuple[int, int]:
        return self._data_dirs[index] if index < len(self._data_dirs) else (0, 0)

    # -- resources --------------------------------------------------------------
    def resources(self) -> list[Resource]:
        """Walk the resource directory.  Returns [] if the image has none."""
        rva, size = self._data_dir(2)
        if not rva or not size:
            return []
        base = self.rva_to_offset(rva, 16, "resource directory")
        # The directory must stay inside the section that holds it.
        limit = next(s.raw_offset + s.raw_size for s in self.sections
                     if s.raw_offset <= base < s.raw_offset + s.raw_size)
        out: list[Resource] = []
        visited: set[int] = set()

        def read_name(value: int) -> ResId:
            if not value & 0x80000000:
                return value
            o = base + (value & 0x7FFFFFFF)
            (n,) = self._unpack("<H", o, "resource name length")
            if n > MAX_NAME_CHARS or o + 2 + 2 * n > limit:
                raise ContainerError("resource name out of bounds", o)
            return self.data[o + 2:o + 2 + 2 * n].decode("utf-16-le", "replace")

        def walk(rel: int, path: tuple) -> None:
            node = base + rel
            if node in visited:
                raise ContainerError("resource directory contains a cycle", node)
            visited.add(node)
            if node + 16 > limit:
                raise ContainerError("resource directory out of bounds", node)
            named, ids = self._unpack("<HH", node + 12, "resource directory header")
            count = named + ids
            if node + 16 + 8 * count > limit:
                raise ContainerError("resource directory entries out of bounds", node)
            for i in range(count):
                ident, target = self._unpack("<II", node + 16 + 8 * i, "resource directory entry")
                sub = path + (read_name(ident),)
                if target & 0x80000000:
                    if len(sub) >= MAX_RES_DEPTH:
                        raise ContainerError("resource directory nested too deeply", node)
                    walk(target & 0x7FFFFFFF, sub)
                    continue
                if len(out) >= MAX_RESOURCES:
                    raise ContainerError(f"more than {MAX_RESOURCES} resources")
                entry = base + target
                if entry + 16 > limit:
                    raise ContainerError("resource data entry out of bounds", entry)
                data_rva, data_size = self._unpack("<II", entry, "resource data entry")
                off = self.rva_to_offset(data_rva, data_size, f"resource {'/'.join(map(str, sub))}")
                sub = sub + (0,) * (MAX_RES_DEPTH - len(sub))
                lang = sub[2] if isinstance(sub[2], int) else 0
                out.append(Resource(sub[0], sub[1], lang, off, data_size, self.data))

        walk(0, ())
        return out

    # -- exports ----------------------------------------------------------------
    def export_name(self) -> Optional[str]:
        """The DLL's internal file name from its export directory, if any."""
        rva, size = self._data_dir(0)
        if not rva or size < 40:
            return None
        try:
            off = self.rva_to_offset(rva, 40, "export directory")
            (name_rva,) = self._unpack("<I", off + 12, "export name RVA")
            name_off = self.rva_to_offset(name_rva, 1, "export name")
        except ContainerError as e:
            log.debug("%s: no usable export name: %s", self.origin, e)
            return None
        end = self.data.find(b"\0", name_off, name_off + 260)
        if end < 0:
            return None
        return self.data[name_off:end].decode("latin-1")


@dataclass(frozen=True)
class Plugin:
    """One embedded plugin/behavior module (DLLBLOCK/1000+index)."""

    index: int
    resource: Resource
    file_name: Optional[str]     # original .csx name, from the DLL export directory
    timestamp: Optional[int]     # PE timestamp of the module
    sha1: str
    display_name: Optional[str] = None   # plugin name as the editor shows it (DLL string resource 1, OBJ_NAME)


@dataclass
class Container:
    """Everything Phase 1 can say about an export without parsing the blocks."""

    path: str
    size: int
    pe_timestamp: int
    is_pe32_plus: bool
    overlay_size: int
    resources: list[Resource]
    blocks: dict[str, Resource] = field(default_factory=dict)
    plugins: list[Plugin] = field(default_factory=list)
    files: list[Resource] = field(default_factory=list)          # FILES/1..M, in index order
    dependencies: list[Resource] = field(default_factory=list)   # DEPENDENCIES/<NAME>
    icons: list[Resource] = field(default_factory=list)
    declared_plugin_count: Optional[int] = None                   # RT_STRING id 1
    preview_plugin_names: list[str] = field(default_factory=list)
    from_installer: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def is_preview(self) -> bool:
        """Preview build: plugin *names* in the string table instead of embedded DLLs."""
        return bool(self.preview_plugin_names) and not self.plugins

    @property
    def is_construct_export(self) -> bool:
        return "APPBLOCK" in self.blocks and "LEVELBLOCK" in self.blocks

    @property
    def has_python(self) -> bool:
        return "PYTHONLIBS" in self.blocks


def parse_string_block(data: bytes) -> list[str]:
    """Decode one RT_STRING block: up to 16 strings, each ``u16 length`` + UTF-16LE."""
    out: list[str] = []
    pos = 0
    while pos + 2 <= len(data) and len(out) < 16:
        (n,) = struct.unpack_from("<H", data, pos)
        pos += 2
        if pos + 2 * n > len(data):
            break
        out.append(data[pos:pos + 2 * n].decode("utf-16-le", "replace"))
        pos += 2 * n
    return out


def _construct_string(res: Resource) -> Optional[str]:
    """The string the editor stored via AllocStringResourceFormat(text, 1): slot 1 of the block."""
    strings = parse_string_block(res.data)
    return strings[1].rstrip("\0") if len(strings) > 1 else None


def _read_file(path: str, max_file_size: int) -> bytes:
    size = os.stat(path).st_size
    if size > max_file_size:
        raise ContainerError(f"file is {size} bytes, over the {max_file_size} byte limit")
    # Read exactly the size stat() reported: f.read(limit) would preallocate the whole limit
    # (1 GiB), which a 32-bit Python on Windows 7 cannot afford.
    with open(path, "rb") as f:
        data = f.read(size)
        if f.read(1):
            raise ContainerError("file grew while it was being read")
    return data


def load_container(path: str, *, max_file_size: int = MAX_FILE_SIZE,
                   unwrap_installer: bool = True) -> Container:
    """Open ``path`` and index the Construct Classic data inside it.

    Raises :class:`ContainerError` if the file is not a parseable PE.  A PE that
    simply is not a Construct export is returned with ``is_construct_export``
    False and an explanatory warning, so callers can report it cleanly.
    """
    return parse_container(_read_file(path, max_file_size), path, unwrap_installer=unwrap_installer)


def parse_container(data: bytes, origin: str = "<memory>", *,
                    unwrap_installer: bool = True) -> Container:
    """Like :func:`load_container`, for bytes already in memory."""
    pe = PEImage(data, origin)
    resources = pe.resources()
    log.info("%s: PE timestamp %d, %d resources, overlay %d bytes",
             origin, pe.timestamp, len(resources), pe.overlay_size)

    # Installer export: the real game exe sits in RUNTIME/131 (Export - Installer.cpp).
    has_blocks = any(r.type == "APPBLOCK" for r in resources)
    wrapped = [r for r in resources if r.type == INSTALLER_TYPE]
    if unwrap_installer and wrapped and not has_blocks:
        log.info("%s: installer wrapper detected, descending into %s", origin, wrapped[0].path)
        inner = parse_container(wrapped[0].data, f"{origin}!{wrapped[0].path}", unwrap_installer=False)
        inner.from_installer = True
        inner.path = origin
        inner.warnings.insert(0, "file is an installer; data was read from the embedded "
                                 f"{wrapped[0].path} executable (installer payload format is unverified)")
        return inner

    c = Container(path=origin, size=len(data), pe_timestamp=pe.timestamp,
                  is_pe32_plus=pe.is_pe32_plus, overlay_size=pe.overlay_size, resources=resources)

    dll_by_id: dict[int, Resource] = {}
    file_by_id: dict[int, Resource] = {}
    for r in resources:
        if isinstance(r.type, str):
            t = r.type.upper()
            if t in BLOCK_IDS:
                if r.name != BLOCK_IDS[t]:
                    c.warnings.append(f"{r.path}: unexpected id, runtime expects {BLOCK_IDS[t]}; ignored")
                elif t in c.blocks:
                    c.warnings.append(f"{r.path}: duplicate block (another language); ignored")
                else:
                    c.blocks[t] = r
            elif t == "DLLBLOCK" and isinstance(r.name, int):
                dll_by_id.setdefault(r.name, r)
            elif t == "FILES" and isinstance(r.name, int):
                file_by_id.setdefault(r.name, r)
            elif t == "DEPENDENCIES":
                c.dependencies.append(r)
        elif r.type == RT_STRING and isinstance(r.name, int):
            text = _construct_string(r)
            if r.name == 1 and c.declared_plugin_count is None and text is not None:
                try:
                    c.declared_plugin_count = int(text)
                except ValueError:
                    c.warnings.append(f"{r.path}: plugin count string {text!r} is not a number")
            elif r.name >= PLUGIN_BASE_ID and text:
                c.preview_plugin_names.append(text)
        elif r.type in (RT_ICON, RT_GROUP_ICON):
            c.icons.append(r)

    c.files = [file_by_id[k] for k in sorted(file_by_id)]
    _collect_plugins(c, dll_by_id)
    _validate(c, file_by_id)
    return c


def _collect_plugins(c: Container, dll_by_id: dict[int, Resource]) -> None:
    if len(dll_by_id) > MAX_PLUGINS:
        raise ContainerError(f"{len(dll_by_id)} DLLBLOCK resources, over the limit of {MAX_PLUGINS}")
    for res_id in sorted(dll_by_id):
        r = dll_by_id[res_id]
        name: Optional[str] = None
        stamp: Optional[int] = None
        display: Optional[str] = None
        try:
            dll = PEImage(r.data, r.path)
            name, stamp = dll.export_name(), dll.timestamp
            # OBJ_NAME = string id 1 = slot 1 of string block 1 (LoadString in IDE/Construct.cpp)
            for sr in dll.resources():
                if sr.type == RT_STRING and sr.name == 1:
                    strings = parse_string_block(sr.data)
                    if len(strings) > 1 and strings[1]:
                        display = strings[1]
                    break
        except ContainerError as e:
            c.warnings.append(f"{r.path}: embedded plugin is not a valid PE ({e})")
        c.plugins.append(Plugin(res_id - PLUGIN_BASE_ID, r, name, stamp, r.sha1, display))


def _validate(c: Container, file_by_id: dict[int, Resource]) -> None:
    """Cross-checks derived from the exporter/runtime source (FORMAT.md section 1)."""
    if not c.is_construct_export:
        c.warnings.append("no APPBLOCK/LEVELBLOCK resources: this does not look like a "
                          "Construct Classic export")
        return
    for name in REQUIRED_BLOCKS:
        if name not in c.blocks:
            c.warnings.append(f"required block {name}/{BLOCK_IDS[name]} is missing")
    if c.is_preview:
        c.warnings.append("preview build: plugins are referenced by name only "
                          f"({', '.join(c.preview_plugin_names)}), not embedded")
        expected = len(c.preview_plugin_names)
    else:
        expected = len(c.plugins)
        ids = [p.index for p in c.plugins]
        if ids != list(range(len(ids))):
            c.warnings.append(f"DLLBLOCK ids are not contiguous from {PLUGIN_BASE_ID}: "
                              f"{[i + PLUGIN_BASE_ID for i in ids]}")
    if c.declared_plugin_count is None:
        c.warnings.append("plugin count string (RT_STRING id 1) is missing")
    elif c.declared_plugin_count != expected:
        c.warnings.append(f"plugin count string says {c.declared_plugin_count}, "
                          f"but {expected} plugin entries were found")
    if sorted(file_by_id) != list(range(1, len(file_by_id) + 1)):
        c.warnings.append(f"FILES ids are not contiguous from 1: {sorted(file_by_id)}")
    menu = c.blocks.get("MENUBLOCK")
    if menu is not None and menu.data != b"\0\0\0\0":
        c.warnings.append(f"MENUBLOCK is not the expected 4 zero bytes ({menu.size} bytes)")
    if c.overlay_size:
        c.warnings.append(f"{c.overlay_size} bytes of overlay after the last PE section "
                          "(not produced by the Construct exporter; left untouched)")
