"""Cross-references over a parsed project: names for runtime ids, decoded
properties, and which object/animation/frame every image belongs to.

Shared by Level 1 (file names for assets) and Level 2 (readable JSON).
"""

from __future__ import annotations

import logging
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from . import model as m
from .aces import PluginAces, build_plugin_aces
from .container import Container
from .plugins import decode_properties

log = logging.getLogger(__name__)

_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_component(name: str, fallback: str = "_") -> str:
    """One portable, harmless path component; keeps non-ASCII letters (e.g. Cyrillic)."""
    name = unicodedata.normalize("NFC", name)
    name = "".join(c if c.isprintable() and c not in '<>:"/\\|?*' and not 0xD800 <= ord(c) <= 0xDFFF else "_"
                   for c in name)
    name = name.strip().rstrip(".").lstrip(".")
    if not name or name.lower().split(".")[0] in _RESERVED:
        name = (name + "_") if name else fallback
    return name[:80]


@dataclass
class ImageRef:
    image_id: int
    kind: str                       # "frame", "texture"
    object_type: Optional[str]
    path: list[str]                 # e.g. ["Player", "Walk", "angle_0"] (frame) or ["Wall"] (texture)
    frame: Optional[int] = None


@dataclass
class ProjectIndex:
    container: Container
    project: m.Project
    type_names: dict[int, str] = field(default_factory=dict)          # runtime oid -> display name
    behaviors_by_owner: dict[int, list[m.Behavior]] = field(default_factory=dict)
    plugin_aces: list[PluginAces] = field(default_factory=list)
    animations: dict[int, m.Animation] = field(default_factory=dict)
    animation_parent: dict[int, int] = field(default_factory=dict)
    type_animation_roots: dict[int, list[int]] = field(default_factory=dict)  # oid -> root animation ids
    image_refs: dict[int, list[ImageRef]] = field(default_factory=dict)
    image_files: dict[int, str] = field(default_factory=dict)          # id -> relative path (Level 1)
    property_cache: dict[tuple[int, bytes], tuple[Optional[dict], Optional[str]]] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    # -- names ------------------------------------------------------------------
    def plugin_file(self, index: int) -> Optional[str]:
        c = self.container
        if 0 <= index < len(c.plugins):
            return c.plugins[index].file_name
        if 0 <= index < len(c.preview_plugin_names):
            return c.preview_plugin_names[index]
        return None

    def type_name(self, oid: int) -> str:
        if oid == -1:
            return "System"
        return self.type_names.get(oid, f"<object {oid}>")

    def object_type(self, oid: int) -> Optional[m.ObjectType]:
        lv = self.project.level
        return lv.object_types[oid] if lv and 0 <= oid < len(lv.object_types) else None

    def family(self, oid: int) -> Optional[m.Family]:
        lv = self.project.level
        if not lv:
            return None
        k = oid - len(lv.object_types) - len(lv.behaviors) - len(lv.traits)
        return lv.families[k] if 0 <= k < len(lv.families) else None

    def plugin_of(self, oid: int) -> Optional[int]:
        t = self.object_type(oid)
        if t:
            return t.plugin_index
        f = self.family(oid)
        if f and f.oids:
            first = self.object_type(f.oids[0])
            return first.plugin_index if first else None
        return None

    def behavior(self, owner_oid: int, ordinal: int) -> Optional[m.Behavior]:
        for b in self.behaviors_by_owner.get(owner_oid, []):
            if b.index == ordinal:
                return b
        # families: behaviors of the first member with that ordinal
        f = self.family(owner_oid)
        if f and f.oids:
            return self.behavior(f.oids[0], ordinal)
        return None

    def properties(self, plugin_index: int, data: bytes) -> tuple[Optional[dict], Optional[str]]:
        key = (plugin_index, data)
        if key not in self.property_cache:
            self.property_cache[key] = decode_properties(self.plugin_file(plugin_index), data,
                                                         self.project.encoding)
        return self.property_cache[key]


def build_index(container: Container, project: m.Project) -> ProjectIndex:
    ix = ProjectIndex(container, project)
    lv = project.level
    if lv is None:
        return ix
    nt, nb, ntr = len(lv.object_types), len(lv.behaviors), len(lv.traits)
    for t in lv.object_types:
        ix.type_names[t.oid] = t.name
    for i, b in enumerate(lv.behaviors):
        ix.behaviors_by_owner.setdefault(b.owner_oid, []).append(b)
        ix.type_names[nt + i] = f"{ix.type_names.get(b.owner_oid, '?')}[{b.name}]"
    for i, t in enumerate(lv.traits):
        ix.type_names[nt + nb + i] = t.name
    for i, f in enumerate(lv.families):
        ix.type_names[nt + nb + ntr + i] = f.name
    n_plugins = max(len(container.plugins), len(container.preview_plugin_names))
    ix.plugin_aces = build_plugin_aces(
        project, [ix.plugin_file(i) for i in range(n_plugins)],
        [container.plugins[i].display_name if i < len(container.plugins) else None for i in range(n_plugins)])

    stack = [(a, None) for a in lv.animations]
    while stack:
        a, parent = stack.pop()
        if a.id in ix.animations:
            ix.issues.append(f"animation id {a.id} occurs more than once")
        ix.animations[a.id] = a
        if parent is not None:
            ix.animation_parent[a.id] = parent
        stack.extend((s, a.id) for s in a.sub_animations)

    _link_images(ix)
    return ix


def _link_images(ix: ProjectIndex) -> None:
    lv = ix.project.level
    assert lv is not None
    # Which animation root / texture each object type uses, from its instances' properties.
    textures: dict[int, int] = {}
    for layout in lv.layouts:
        for layer in layout.layers:
            for inst in layer.instances:
                t = ix.object_type(inst.oid)
                if t is None:
                    continue
                props, _ = ix.properties(t.plugin_index, inst.data)
                if not props:
                    continue
                if "animation_root" in props:
                    roots = ix.type_animation_roots.setdefault(t.oid, [])
                    if props["animation_root"] not in roots:
                        roots.append(props["animation_root"])
                if "image" in props:
                    textures.setdefault(t.oid, props["image"])

    claimed: set[int] = set()

    def walk(anim: m.Animation, path: list[str], owner: Optional[str]) -> None:
        for k, f in enumerate(anim.frames):
            ix.image_refs.setdefault(f.image_id, []).append(ImageRef(f.image_id, "frame", owner, path, k))
        angles = [s for s in anim.sub_animations if s.is_angle]
        for s in anim.sub_animations:
            if s.is_angle:
                sub = path if len(angles) == 1 and abs(s.angle) < 1e-6 else path + [f"angle_{s.angle:g}"]
            else:
                sub = path + [s.name or f"animation_{s.id}"]
            walk(s, sub, owner)

    # Normally all instances of a type share one animation tree; instances pasted as
    # clones keep their own copy, which gets a "#<root id>" suffix.
    for oid, roots in sorted(ix.type_animation_roots.items()):
        for k, root in enumerate(roots):
            anim = ix.animations.get(root)
            if anim is None:
                ix.issues.append(f"object type {ix.type_name(oid)!r} references missing animation {root}")
                continue
            claimed.add(root)
            name = ix.type_name(oid)
            walk(anim, [name if k == 0 else f"{name}#{root}"], name)
    for anim in lv.animations:
        if anim.id not in claimed:
            # Exported with the project but used by no object: left over from deleted objects.
            walk(anim, [f"_orphaned_animation_{anim.id}"], None)
    for oid, image in sorted(textures.items()):
        ix.image_refs.setdefault(image, []).append(ImageRef(image, "texture", ix.type_name(oid), [ix.type_name(oid)]))


def assign_image_files(ix: ProjectIndex) -> dict[int, str]:
    """Pick one unique relative file path per image, from its first reference."""
    used: set[str] = set()
    out: dict[int, str] = {}
    for img in ix.project.images or []:
        ext = img.format if img.format != "bin" else "bin"
        refs = ix.image_refs.get(img.id)
        if refs:
            r = refs[0]
            parts = [safe_component(p) for p in r.path]
            leaf = f"frame_{r.frame:03d}" if r.kind == "frame" else "texture"
        else:
            parts, leaf = ["_unreferenced"], f"image_{img.id}"
        base = "/".join(["images", *parts, leaf])
        path, n = f"{base}.{ext}", 2
        while path.lower() in used:
            path, n = f"{base}_{n}.{ext}", n + 1
        used.add(path.lower())
        out[img.id] = path
    ix.image_files = out
    return out

