"""Level 1: write the embedded assets as ordinary files.

Output (relative to the output directory)::

    images/<Object>/<Animation>[/angle_<deg>]/frame_000.png   sprite frames
    images/<Object>/texture.png                               tiled backgrounds, panels
    images/_orphaned_animation_<id>/...                       animation trees no object uses (deleted objects)
    images/_unreferenced/image_<id>.png                       images nothing references
    masks/<same path>.png                                     custom collision masks (only with --masks)
    files/file_0001.mp3 ...                                   "Files" resources (names are not exported)
    effects/<name>.fxo                                        compiled D3DX effects (source .fx is not exported)
    icon/application.ico
    python/PythonLibs.zip                                     if scripting was enabled
    assets.json                                               index of everything above

Image bytes are written exactly as stored in the exe (no re-encoding).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import struct
from typing import Callable, Optional

from . import png
from .container import Container
from .index import ProjectIndex, assign_image_files, safe_component

log = logging.getLogger(__name__)


def sniff(data: bytes) -> str:
    """File extension from content (the exporter drops names and extensions)."""
    head = data[:16]
    if head.startswith(b"RIFF") and data[8:12] == b"WAVE":
        return "wav"
    if head.startswith(b"RIFF") and data[8:12] == b"AVI ":
        return "avi"
    if head.startswith(b"OggS"):
        return "ogg"
    if head.startswith(b"fLaC"):
        return "flac"
    if head.startswith(b"ID3"):
        return "mp3"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith(b"PK\x03\x04"):
        return "zip"
    if head.startswith(b"MZ"):
        return "dll"
    if head.startswith(b"0&\xb2u\x8e f\x11"):
        return "wma"
    if head.startswith(b"MThd"):
        return "mid"
    if len(data) > 1084 and data[1080:1084] in (b"M.K.", b"M!K!", b"FLT4", b"4CHN", b"6CHN", b"8CHN"):
        return "mod"
    if head.startswith(b"Extended Module"):
        return "xm"
    if len(data) > 48 and data[44:48] == b"SCRM":
        return "s3m"
    if head.startswith(b"IMPM"):
        return "it"
    # MP3 without ID3v2: frame sync, possibly after zero padding
    i = 0
    while i < min(len(data), 4096) and data[i] == 0:
        i += 1
    if i + 1 < len(data) and data[i] == 0xFF and data[i + 1] & 0xE0 == 0xE0:
        return "mp3"
    try:
        sample = data[:4096].decode("utf-8")
        if all(c.isprintable() or c in "\r\n\t" for c in sample):
            return "txt"
    except UnicodeDecodeError:
        pass
    return "bin"


def build_ico(container: Container) -> Optional[bytes]:
    """Rebuild an .ico from RT_ICON/1 (+ RT_GROUP_ICON) as written by CExport::ExportResources."""
    icon = next((r for r in container.icons if r.type == 3), None)
    if icon is None:
        return None
    group = next((r for r in container.icons if r.type == 14), None)
    width = height = 32
    colors, planes, bpp = 0, 1, 32
    if group is not None and len(group.data) >= 20:
        width, height, colors, _, planes, bpp = struct.unpack_from("<BBBBHH", group.data, 6)
    data = icon.data
    if data[:4] == b"\x89PNG":
        width, height = (min(v, 256) % 256 for v in png.png_size(data))
    elif len(data) >= 16:            # BITMAPINFOHEADER: height counts XOR + AND masks
        w, h = struct.unpack_from("<ii", data, 4)
        width, height = w % 256, (h // 2) % 256
        planes, bpp = struct.unpack_from("<HH", data, 12)
    return struct.pack("<HHH", 0, 1, 1) + struct.pack("<BBBBHHII", width, height, colors, 0, planes, bpp,
                                                     len(data), 22) + data


def _write(out_dir: str, rel: str, data: bytes) -> str:
    path = os.path.normpath(os.path.join(out_dir, *rel.split("/")))
    root = os.path.normpath(os.path.abspath(out_dir))
    if not os.path.abspath(path).startswith(root + os.sep):
        raise ValueError(f"refusing to write outside the output directory: {rel}")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return rel


def analyse_mask(img) -> tuple[str, Optional[bytes]]:
    """Classify a collision mask: 'none', 'alpha' (derived from the image), 'custom' or 'error:...'.

    Returns the mask as a 1-bit PNG for 'custom'.
    """
    if img.mask_width <= 0 or img.mask_height <= 0 or img.mask_pitch <= 0:
        return "none", None
    rows = png.mask_rows(bytes(img.mask), img.mask_width, img.mask_height, img.mask_pitch)
    if img.format == "png":
        try:
            decoded = png.decode(bytes(img.data))
        except png.PNGError as e:
            return f"error: {e}", png.encode_bilevel(rows, img.mask_width)
        if (decoded.width, decoded.height) == (img.mask_width, img.mask_height) and decoded.alpha_mask() == rows:
            return "alpha", None
    return "custom", png.encode_bilevel(rows, img.mask_width)


def export_assets(ix: ProjectIndex, out_dir: str, analyse_masks: bool = False,
                  progress: Optional[Callable[[int, int], None]] = None) -> dict:
    """Write Level 1 output and return the assets index (also saved as assets.json).

    ``progress(done, total)`` is called before each image; it may raise to abort the export.
    """
    c, p = ix.container, ix.project
    index: dict = {"images": [], "files": [], "effects": [], "icon": None, "python": None, "notes": []}
    files = assign_image_files(ix)

    for k, img in enumerate(p.images or []):
        if progress is not None:
            progress(k, len(p.images or []))
        rel = files[img.id]
        data = bytes(img.data)
        _write(out_dir, rel, data)
        entry = {
            "id": img.id, "file": rel, "format": img.format, "size": len(data),
            "sha1": hashlib.sha1(data).hexdigest(),
            "hotspot": [img.hotspot_x, img.hotspot_y],
            "action_points": [{"name": a.name, "x": a.x, "y": a.y} for a in img.action_points],
            "collision_mask": {"width": img.mask_width, "height": img.mask_height, "pitch": img.mask_pitch},
            "used_by": [{"object": r.object_type, "kind": r.kind, "path": r.path, "frame": r.frame}
                        for r in ix.image_refs.get(img.id, [])],
        }
        if img.format == "png":
            try:
                entry["width"], entry["height"] = png.png_size(data)
            except png.PNGError:
                pass
        if analyse_masks:
            kind, mask_png = analyse_mask(img)
            entry["collision_mask"]["kind"] = kind
            if mask_png is not None:
                entry["collision_mask"]["file"] = _write(out_dir, "masks/" + rel.split("/", 1)[1].rsplit(".", 1)[0]
                                                         + ".png", mask_png)
        index["images"].append(entry)
    if not analyse_masks:
        index["notes"].append("collision masks were not analysed (use --masks); a mask that differs from the "
                              "image alpha means a custom collision image was set in the editor")

    for r in c.files:
        data = r.data
        ext = sniff(data)
        rel = _write(out_dir, f"files/file_{r.name:04d}.{ext}", data)
        index["files"].append({"index": r.name, "file": rel, "type": ext, "size": len(data),
                               "sha1": hashlib.sha1(data).hexdigest()})
    if c.files:
        index["notes"].append("file resources carry no names in the exe; they are numbered in the order "
                              "of the editor's Files list, which is also how events refer to them")

    if p.hlsl:
        used: set[str] = set()
        for k, s in enumerate(p.hlsl.shaders):
            stem = safe_component(s.path.replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0], f"effect_{k}")
            folder = "transitions" if s.path.lower().startswith("transitions") else "effects"
            rel = f"{folder}/{stem}.fxo"
            while rel.lower() in used:
                rel = f"{folder}/{stem}_{k}.fxo"
            used.add(rel.lower())
            _write(out_dir, rel, s.bytecode)
            index["effects"].append({"index": k, "source_path": s.path, "file": rel,
                                     "pixel_shader": f"{s.major}.{s.minor}", "size": len(s.bytecode)})
        if p.hlsl.shaders:
            index["notes"].append("effects are compiled D3DX bytecode (.fxo); the .fx source text is not in the "
                                  "exe - the stock .fx files ship with Construct under the same names")

    ico = build_ico(c)
    if ico:
        index["icon"] = _write(out_dir, "icon/application.ico", ico)

    py = c.blocks.get("PYTHONLIBS")
    if py is not None:
        index["python"] = _write(out_dir, "python/PythonLibs.zip", py.data)

    with open(os.path.join(out_dir, "assets.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
    return index
