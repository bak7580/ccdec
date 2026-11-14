"""Level 3: rebuild a .cap project file from the data recovered out of an exe.

The tree built here is written by :mod:`cap_format`, the same code that reads
(and byte-exactly rewrites) real .cap files.  What the exe does not contain is
filled with the values a fresh editor project has; every such decision is listed
in ``Rebuilt.notes`` and ends up in the report.

Facts this relies on (FORMAT.md sections 10 and 14):

* plugin property blobs are identical in exe and .cap, so they are copied as is;
* image PNG files are identical in exe and .cap;
* instance ids in the exe are the editor's own instance ids;
* object type ids, behavior ids, variable ids and image names are *not* in the
  exe; fresh ones are assigned consistently;
* event parameters must be stored as editor tokens (cap_tokens).
"""

from __future__ import annotations

import copy
import io
import logging
import os
import zipfile
from dataclasses import dataclass, field
from typing import Optional, Union

from . import model as m
from . import png
from .aces import AceInfo, ParamInfo
from .cap_format import EVENT, GROUP, LATEST
from .cap_tokens import T, T_COLOR, EditorToken, Names, TokenizeError, resolve, tokenize
from .export_assets import sniff
from .export_json import Dumper
from .expressions import render, single_int, token_text, varindex
from .i18n import T as TR              # T is taken by cap_tokens' token table
from .index import ProjectIndex

log = logging.getLogger(__name__)

#: CONSTRUCT_VERSION_ID stored in the file.  10102 is the value found in a real
#: CApplication v18 file (a private test sample), so every editor that can read this
#: class version accepts it; Classic r2 (10200) opens it as "saved by an older version".
CREATED_WITH = 10102

DEFAULT_TRAITS = ["Solid", "Center view on me", "Destroy on Startup", "Destroy if far",
                  "Destroy out of screen", "Platform", "No serialize"]

#: IDE/Bars/AppProperties.cpp :: controlTable[] - a control's comboIndex is the index of its VK here
CONTROL_VKS = [1, 4, 2, 9, 13, 16, 160, 161, 17, 162, 163, 18, 164, 165, 19, 27, 32, 36, 35, 38, 40, 37, 39,
               45, 46, *range(48, 58), *range(65, 91), 8, *range(96, 106), 107, 109, 106, 111, 110,
               *range(112, 124)]

#: (draw, show_angles) the editor stores per plugin, observed in the reference .cap files
OBJECT_FLAGS = {
    "sprite": (1, 1), "box": (1, 1), "line": (1, 1), "text": (1, 0), "tiled background": (1, 0),
    "button": (1, 0), "edit box": (1, 0), "mouse & keyboard": (0, 0), "function": (0, 0),
    "common dialog": (0, 0), "file": (0, 0), "xaudio2": (0, 0), "mod": (0, 0), "image list": (0, 0),
}
ICON_PIXEL_BUDGET = 400_000          # decode an image for a thumbnail only below this size (pure-Python PNG)

EVENT_SPACE, ACE_SPACE, GROUP_SPACE = 23.0, 21.0, 38.0     # must be > 0: the editor deletes rows with Space <= 0


@dataclass
class Rebuilt:
    app: dict
    encoding: str
    notes: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    trigger_heads: set = field(default_factory=set)


class CapBuilder:
    def __init__(self, ix: ProjectIndex, files_dir: Optional[str] = None, thumbnails: bool = True) -> None:
        self.ix = ix
        self.p = ix.project
        self.c = ix.container
        self.lv = ix.project.level
        self.files_dir = files_dir
        self.thumbnails = thumbnails
        self.dumper = Dumper(ix, include_tokens=False)
        self.notes: list[str] = []
        self.stats: dict[str, int] = {}
        self.names = Names()
        self.type_id: dict[int, int] = {}                 # runtime oid -> edit id (types and families)
        self.behavior_id: dict[tuple[int, int], int] = {}  # (owner oid, ordinal) -> unique id
        self.effect_ids: dict[int, list[int]] = {}         # owner oid -> effect ids in order
        self.file_names: dict[int, str] = {}
        self._next_id = 0
        self.trigger_heads: set[tuple[str, int]] = set()   # (owner name, condition id) of returned triggers
        self._icon_cache: dict[int, tuple[bytes, bytes]] = {}

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def count(self, key: str, n: int = 1) -> None:
        self.stats[key] = self.stats.get(key, 0) + n

    def new_id(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    def plugin_name(self, index: int) -> str:
        if 0 <= index < len(self.c.plugins):
            plug = self.c.plugins[index]
            if plug.display_name:
                return plug.display_name
            if plug.file_name:
                self.note(TR(f"плагин {plug.file_name}: отображаемое имя не найдено в DLL, вместо него взято имя файла",
            f"plugin {plug.file_name}: display name not found in the DLL, file name used instead"))
                return plug.file_name.rsplit(".", 1)[0]
        return f"Plugin{index}"

    # -- icons ---------------------------------------------------------------------
    def _flat_icons(self) -> tuple[bytes, bytes]:
        big = bytes((200, 200, 200, 255)) * (32 * 32)
        return png.encode_bmp24(32, 32, big), png.encode_bmp24(16, 16, big[:16 * 16 * 4])

    def type_icons(self, oid: int) -> tuple[bytes, bytes]:
        """32x32 and 16x16 BMP icons for the object bar (the editor's own are not in the exe)."""
        image_id = None
        for ref_id, refs in self.ix.image_refs.items():
            if any(r.object_type == self.ix.type_name(oid) and (r.frame in (0, None)) for r in refs):
                image_id = ref_id
                break
        if image_id is None or not self.thumbnails:
            return self._flat_icons()
        if image_id in self._icon_cache:
            return self._icon_cache[image_id]
        result = self._flat_icons()
        img = next((i for i in self.p.images or [] if i.id == image_id), None)
        try:
            if img is not None and img.format == "png":
                w, h = png.png_size(bytes(img.data[:32]))
                if w * h <= ICON_PIXEL_BUDGET:
                    decoded = png.decode(bytes(img.data))
                    result = (png.encode_bmp24(32, 32, png.thumbnail(decoded, 32)),
                              png.encode_bmp24(16, 16, png.thumbnail(decoded, 16)))
        except png.PNGError:
            pass
        self._icon_cache[image_id] = result
        return result

    # -- identifiers ---------------------------------------------------------------
    def assign_ids(self) -> None:
        lv = self.lv
        instance_ids = [i.instance_id for l in lv.layouts for y in l.layers for i in y.instances]
        self._next_id = max([0, *instance_ids]) + 1
        nt, nb, ntr = len(lv.object_types), len(lv.behaviors), len(lv.traits)
        for t in lv.object_types:
            self.type_id[t.oid] = self.new_id()
            self.names.object_types[t.name.lower()] = self.type_id[t.oid]
            for k, v in enumerate(t.private_variables):
                self.names.private_variables[(self.type_id[t.oid], v.name.lower())] = k
        for t in lv.object_types:
            for b in sorted(self.ix.behaviors_by_owner.get(t.oid, []), key=lambda b: b.index):
                self.behavior_id[(t.oid, b.index)] = self.new_id()
                self.names.behaviors[(self.type_id[t.oid], b.name.lower())] = self.behavior_id[(t.oid, b.index)]
        if self.p.hlsl:
            for oe in self.p.hlsl.object_effects:
                self.effect_ids[oe.oid] = [self.new_id() for _ in oe.effects]
        for k, f in enumerate(lv.families):
            fid = self.new_id()
            self.type_id[nt + nb + ntr + k] = fid
            self.names.families[f.name.lower()] = fid
            for j, v in enumerate(f.private_variables):
                self.names.private_variables[(fid, v.name.lower())] = j
        if self.p.app:
            for k, g in enumerate(self.p.app.global_variables):
                self.names.global_variables[g.name.lower()] = k

    # -- pieces --------------------------------------------------------------------
    def effect(self, e: m.EffectUse, effect_id: int) -> dict:
        shaders = self.p.hlsl.shaders if self.p.hlsl else []
        shader = shaders[e.shader_index] if 0 <= e.shader_index < len(shaders) else None
        path = shader.path if shader else ""
        return {"id": effect_id, "file": path.replace("/", "\\").rsplit("\\", 1)[-1], "name": e.name,
                "version": float(f"{shader.major}.{shader.minor}") if shader else 0.0,
                "disable_mode": e.disable_mode,
                "params": [{"type": q.type, "variable_name": q.variable_name, "value": q.value,
                            "name": q.name, "description": ""} for q in e.params]}

    def object_type(self, t: m.ObjectType) -> dict:
        big, small = self.type_icons(t.oid)
        effects = next((oe.effects for oe in (self.p.hlsl.object_effects if self.p.hlsl else []) if oe.oid == t.oid), [])
        return {"key": self.type_id[t.oid], "type": {
            "name": t.name, "id": self.type_id[t.oid], "plugin": self.plugin_name(t.plugin_index),
            "image_bmp": big, "small_image_bmp": small,
            "behaviors": [{"plugin": self.plugin_name(b.plugin_index), "id": self.behavior_id[(t.oid, b.index)],
                           "name": b.name, "data": b.data}
                          for b in sorted(self.ix.behaviors_by_owner.get(t.oid, []), key=lambda b: b.index)],
            "effects": [self.effect(e, self.effect_ids[t.oid][k]) for k, e in enumerate(effects)],
            "traits": [tr.name for tr in self.lv.traits if t.oid in tr.oids],
            "variable_id_counter": len(t.private_variables),
            "private_variables": [{"name": v.name, "type": v.type, "id": k}
                                  for k, v in enumerate(t.private_variables)],
            "global": int(t.is_global), "destroy_when": t.destroy_when,
            "show_in_event_sheet_editor": 1, "category": "Object", "folder": "Default"}}

    def system_type(self) -> dict:
        # Empty bitmaps: the editor keeps the System icons it loaded from its own resources.
        return {"name": "System", "id": -1, "plugin": "", "image_bmp": b"", "small_image_bmp": b"",
                "behaviors": [], "effects": [], "traits": [], "variable_id_counter": 0,
                "private_variables": [], "global": 0, "destroy_when": 0,
                "show_in_event_sheet_editor": 1, "category": "Object", "folder": "Default"}

    def family(self, k: int, f: m.Family) -> dict:
        lv = self.lv
        fid = self.type_id[len(lv.object_types) + len(lv.behaviors) + len(lv.traits) + k]
        first = self.ix.object_type(f.oids[0]) if f.oids else None
        big, small = self._flat_icons()
        variables = []
        for j, v in enumerate(f.private_variables):
            refs = []
            for oid in f.oids:
                t = self.ix.object_type(oid)
                idx = next((n for n, pv in enumerate(t.private_variables) if pv.name.lower() == v.name.lower()), None) \
                    if t else None
                if idx is not None:
                    refs.append({"item_id": idx, "object": self.type_id[oid]})
            variables.append({"id": j, "name": v.name, "references": refs})
        self.note(TR("families восстановлены по спискам участников; общие behaviors и эффекты families "
                  "в .exe не попадают, на образцах families не проверялись",
            "families are rebuilt from the exported member lists; family behaviors/effects sharing "
                  "is not in the exe and families were not present in any verified sample (unverified)"))
        return {"id": fid, "name": f.name, "plugin": self.plugin_name(first.plugin_index) if first else "",
                "image_bmp": big, "small_image_bmp": small,
                "object_types": [self.type_id[o] for o in f.oids if o in self.type_id],
                "behaviors": [], "variables": variables, "effects": []}

    def origin(self, t: m.ObjectType, props: Optional[dict]) -> tuple[float, float]:
        """Hotspot ratio the editor keeps per object (Sprite: from its first frame; others 0)."""
        if not props or "animation_root" not in props:
            return 0.0, 0.0
        anim = self.ix.animations.get(props["animation_root"])
        stack = [anim] if anim else []
        while stack:
            a = stack.pop(0)
            if a.frames:
                img = next((i for i in self.p.images or [] if i.id == a.frames[0].image_id), None)
                try:
                    if img is not None and img.format == "png":
                        w, h = png.png_size(bytes(img.data[:32]))
                        if w and h:
                            return img.hotspot_x / w, img.hotspot_y / h
                except png.PNGError:
                    pass
                break
            stack.extend(a.sub_animations)
        return 0.5, 0.5

    def instance(self, i: m.Instance, key: int) -> dict:
        t = self.ix.object_type(i.oid)
        plugin = self.plugin_name(t.plugin_index) if t else ""
        props = self.ix.properties(t.plugin_index, i.data)[0] if t else None
        ox, oy = self.origin(t, props) if t else (0.0, 0.0)
        draw, angles = OBJECT_FLAGS.get(plugin.lower(), (1, 1))
        return {"key": key, "object": {
            "x": float(i.x), "y": float(i.y), "width": float(i.width), "height": float(i.height),
            "origin_x": ox, "origin_y": oy, "angle": i.angle, "filter": i.filter, "is_group_flag": 0,
            "plugin": plugin, "instance_id": key, "object_type": self.type_id.get(i.oid, -1),
            "locked": 0, "visible": 1, "allow_size": 1, "draw": draw, "show_angles": angles,
            "in_group": 0, "is_group": 0, "group_open": 0, "edit_pointer": 0,
            "temp_name": t.name if t else "", "data": i.data,
            "values": [{"id": k, "value": v} for k, v in enumerate(i.private_values)],
            "expand_states": []}}

    def layout(self, index: int, l: m.Layout) -> dict:
        layer_fx = {}
        if self.p.hlsl:
            for le in self.p.hlsl.layer_effects:
                if le.layout_index == index:
                    layer_fx[le.layer_index] = le.effects
        objects, layers, used = [], [], set()
        order = [len(l.layers) - 1, *range(len(l.layers) - 1)] if l.layers else []   # editor order
        for k in order:
            y = l.layers[k]
            z = []
            for inst in y.instances:
                key = inst.instance_id
                if key in used:                     # objects of an inherited layer are exported twice
                    key = self.new_id()
                    self.note(TR("часть экземпляров встречается на нескольких слоях (экспортёр разворачивает "
                              "наследуемые слои); копиям выданы новые id",
            "some instances appear on more than one layer (inherited layers are flattened "
                              "by the exporter); the copies were given new instance ids"))
                used.add(key)
                objects.append(self.instance(inst, key))
                z.append(key)
            fx = layer_fx.get(-1 if k == len(l.layers) - 1 else k, [])
            layers.append({
                "id": y.id, "type": y.type, "name": y.name, "state": 1 if y.type == 2 else 0, "angle": y.angle,
                "color": y.filter, "scroll_x_rate": y.scroll_x_factor, "scroll_y_rate": y.scroll_y_factor,
                "opacity": y.opacity, "clear_background": y.clear_background,
                "background_color": y.background_color, "zoom_x": y.zoom_x, "zoom_y": y.zoom_y,
                "force_own_texture": int(y.force_own_texture), "zoom_x_rate": y.zoom_x_factor,
                "zoom_y_rate": y.zoom_y_factor, "scroll_x": y.scroll_x, "scroll_y": y.scroll_y,
                "inherit_layer_id": -1, "sampler": y.sampler, "enable_3d": int(y.enable_3d),
                "clear_depth_buffer": int(bool(y.clear_depth_buffer)),
                "effects": [self.effect(e, self.new_id()) for e in fx], "z_order": z})
        # The exporter keeps only the RGB part (CLayout::GetColor); the high byte is the editor's
        # opacity and is 0xFF in every reference file.
        return {"id": index, "width": l.width, "height": l.height, "name": l.name,
                "color": (l.color & 0xFFFFFF) | 0xFF000000,
                "unbounded_scrolling": int(l.unbounded_scrolling),
                "application_background": int(l.application_background),
                "objects": objects, "layers": layers, "event_sheet_id": index,
                "keys": [{"name": k.key, "type": k.type, "value": k.value} for k in l.keys],
                "grid": 0, "snap_movements": 0, "snap_resize": 0, "grid_width": 16, "grid_height": 16,
                "texture_loading": l.texture_loading}

    def animation(self, a: m.Animation) -> dict:
        return {"id": a.id, "name": a.name, "speed": a.speed, "repeat": 1 if a.repeat == -1 else a.repeat,
                "repeat_to": a.repeat_to + 1, "forever": 1 if a.repeat == -1 else 0,
                "pingpong": int(bool(a.pingpong)), "is_angle": int(a.is_angle), "angle": a.angle,
                "expanded": 0 if a.is_angle else 1, "images": [f.image_id for f in a.frames],
                "frame_times": [f.time_factor for f in a.frames],
                "sub_animations": [self.animation(s) for s in a.sub_animations], "tag": a.tag}

    def images_and_animations(self) -> tuple[list[dict], list[dict]]:
        roots = sorted({r for v in self.ix.type_animation_roots.values() for r in v if r in self.ix.animations})
        anims = [self.animation(self.ix.animations[r]) for r in roots]
        used: set[int] = set()
        stack = [self.ix.animations[r] for r in roots]
        while stack:
            a = stack.pop()
            used.update(f.image_id for f in a.frames)
            stack.extend(a.sub_animations)
        for image_id, refs in self.ix.image_refs.items():
            if any(r.kind == "texture" for r in refs):
                used.add(image_id)
        images = []
        for img in self.p.images or []:
            if img.id not in used:
                self.count("images_dropped")
                continue
            if img.format != "png":
                self.note(TR("изображения не в PNG (экспорт Application-рантаймом); редактор ждёт PNG — не проверено",
            "images are not PNG (Application runtime export); the editor expects PNG - unverified"))
            try:
                w, h = png.png_size(bytes(img.data[:32]))
                dims = f"{w} x {h}"
            except png.PNGError:
                dims = ""
            images.append({"name": f"ObjectImage{img.id}", "id": img.id, "dimensions": dims,
                           "hotspot_x": img.hotspot_x, "hotspot_y": img.hotspot_y,
                           "action_points": [{"x": a.x, "y": a.y, "name": a.name} for a in img.action_points],
                           "png": bytes(img.data), "collision": b""})
        if self.stats.get("images_dropped"):
            self.note(TR(f"{self.stats['images_dropped']} изображений не принадлежат ни одному объекту (анимации удалённых "
                      "объектов) и не записаны в .cap — редактор сам не сохраняет такие; Level 1 их извлекает",
            f"{self.stats['images_dropped']} images belong to no object (orphaned animations) and "
                      "are left out of the .cap, as the editor itself would do on save; Level 1 still extracts them"))
        return anims, images

    def files(self) -> list[dict]:
        out = []
        for r in self.c.files:
            name = f"file_{r.name:04d}.{sniff(r.data)}"
            self.file_names[r.name] = name
            path = os.path.abspath(os.path.join(self.files_dir, name)) if self.files_dir else name
            out.append({"name": name, "type": 1, "path": path, "id": r.name})
        if out:
            self.note(TR("файлы-ресурсы подключены путями, как и в настоящем .cap: не перемещайте папку files "
                      "или заново укажите файлы в списке Files редактора",
            "file resources are referenced by path, exactly as in a real .cap: keep the extracted "
                      "'files' folder where it is, or relink the files in the editor's Files list"))
        return out

    def icon(self) -> dict:
        data = next((r.data for r in self.c.icons if r.type == 3), None)
        image = None
        if data:
            if data[:8] == png.SIGNATURE:
                return {"name": "Default", "type": 2, "png": data}
            try:
                image = png.decode_icon_dib(data)
            except png.PNGError as e:
                self.note(TR(f"иконку приложения не удалось прочитать ({e}); записана пустая",
            f"application icon could not be decoded ({e}); a blank icon was written"))
        if image is None:
            image = png.Image(32, 32, bytes(32 * 32 * 4))
        return {"name": "Default", "type": 2, "png": png.encode_rgba(image.width, image.height, image.rgba)}

    # -- events --------------------------------------------------------------------
    def _int_token(self, value: int, owner: int = -1, ident: int = 0) -> dict:
        text = str(value)
        return EditorToken(T["INTEGER"], text, len(text), owner, ident).as_cap()

    def _generic_tokens(self, tokens: list[m.Token], cap_oid: int, caller: Optional[str]) -> list[dict]:
        text = render(tokens)
        try:
            toks = tokenize(text, caller)
        except TokenizeError:
            # e.g. identifiers the editor's tokenizer rejects: keep a direct token mapping
            self.count("params_untokenized")
            toks = []
            for t in tokens:
                s = token_text(t)
                if t.type == m.T_STRINGLITERAL:
                    s = str(t.value).replace('"', '""')
                elif t.type == m.T_VARIABLENAME:
                    s = str(t.value)
                toks.append(EditorToken(t.type, s, len(s) + (2 if t.type in (m.T_STRINGLITERAL, m.T_VARIABLENAME) else 0)))
        resolve(toks, self.names, cap_oid)
        return [t.as_cap() for t in toks]

    def param(self, tokens: list[m.Token], info: Optional[ParamInfo], cap_oid: int, runtime_oid: int) -> dict:
        ptype = info.type if info else None
        value = single_int(tokens)
        reserved = ""
        caller = self.ix.type_name(runtime_oid) if runtime_oid != -1 else None
        vi = varindex(tokens)
        out: Optional[list[dict]] = None
        if ptype is None:
            ptype = 14 if vi else 1
            self.count("params_without_type")
        if ptype == 6 and value is not None and value in self.type_id:                 # object
            eid = self.type_id[value]
            out = [self._int_token(eid, eid, eid)]
        elif ptype == 14 and vi is not None:                                            # private variable
            out = [self._int_token(vi[0])]
        elif ptype == 13 and value is not None:                                         # global variable
            out = [self._int_token(value)]
        elif ptype == 9 and value is not None:                                          # combo
            options = info.init.split("|") if info and info.init else []
            reserved = options[value] if 0 <= value < len(options) else ""
            out = [self._int_token(value)]
        elif ptype == 3 and value is not None:                                          # colour
            text = str(value)
            out = [EditorToken(T_COLOR, text, len(text), -1, value).as_cap()]
        elif ptype == 16 and value is not None:                                         # file resource
            name = self.file_names.get(value, "")
            out = [EditorToken(T["STRINGLITERAL"], name, len(name) + 2).as_cap()]
        elif ptype == 17 and len(tokens) == 1 and tokens[0].type == m.T_STRINGLITERAL:  # transition
            name = str(tokens[0].value) or "None"
            out = [EditorToken(T["STRINGLITERAL"], name, len(name) + 2).as_cap()]
        if out is None:
            out = self._generic_tokens(tokens, cap_oid, caller)
        self.count("params")
        return {"type": ptype, "reserved": reserved, "oid": cap_oid, "tokens": out}

    def _ace_common(self, oid: int, movement_id: int, kind: str, ace_id: int,
                    params: list[list[m.Token]]) -> tuple[dict, list[dict]]:
        info, owner = self.dumper.ace(oid, movement_id, kind, ace_id)
        cap_oid = -1 if oid == -1 else self.type_id.get(oid, oid)
        fam = self.ix.family(oid)
        mid = -1
        if movement_id >= 0:
            b = self.ix.behavior(oid, movement_id)
            base = fam.oids[0] if fam and fam.oids else oid
            mid = self.behavior_id.get((base, movement_id), movement_id) if b else movement_id
        elif movement_id <= -2:
            ids = self.effect_ids.get(oid, [])
            k = -2 - movement_id
            mid = -ids[k] if k < len(ids) else movement_id
        infos: list[Optional[ParamInfo]] = list(info.params) if info and len(info.params) == len(params) \
            else [None] * len(params)
        if info is None or info.origin == "exe":
            self.count("aces_without_metadata")
        plugin_index = self.ix.plugin_of(oid)
        common = {"movement_id": mid, "id": ace_id, "oid": cap_oid, "family": int(fam is not None),
                  "family_name": fam.name if fam else "", "rect": [0, 0, 0, 0], "select": 0, "space": ACE_SPACE,
                  "object_text": "System" if oid == -1 else self.ix.type_name(oid).split("[")[0], "script": "",
                  "plugin": "" if oid == -1 or plugin_index is None else self.plugin_name(plugin_index),
                  "enabled": 1}
        return common, [self.param(tok, pi, cap_oid, oid) for tok, pi in zip(params, infos)]

    def condition(self, c: m.Condition) -> dict:
        d, params = self._ace_common(c.oid, c.movement_id, "conditions", c.cnd_id, c.params)
        d.update(negated=int(c.negated), select2=0, params=params)
        return d

    def action(self, a: m.Action) -> dict:
        d, params = self._ace_common(a.oid, a.movement_id, "actions", a.act_id, a.params)
        d["params"] = params
        return d

    def event(self, e: Union[m.Event, m.EventGroup]) -> dict:
        base = {"handle": [0, 0, 0, 0], "display_action_type": 3, "display_condition_type": 3,
                "display_event_type": 1, "select": 0, "bookmarked": 0, "enabled": 1}
        if isinstance(e, m.EventGroup):
            self.count("groups")
            return {**base, "type": GROUP, "space": GROUP_SPACE, "open": 1, "active": int(e.active),
                    "description": "", "title": e.name, "events": [self.event(x) for x in e.children]}
        self.count("events")
        return {**base, "type": EVENT, "space": EVENT_SPACE, "open": 1, "breakpoint": 0,
                "conditions": [self.condition(c) for c in e.conditions],
                "actions": [self.action(a) for a in e.actions],
                "events": [self.event(x) for x in e.children]}

    def unhoist(self, events: list) -> list:
        """Put triggers the exporter lifted out of groups back into their groups.

        ExportEvent::ProcessThisEventForTrigger turns ``Group{ Event(trigger, rest...) }`` into a
        top-level ``Event(trigger){ Group-clone{ Event(rest...) } }`` appended to the list.  That
        shape is recognised and reversed; the event returns to its group (at the end - its original
        position inside the group is not recoverable, and does not matter for a triggered event).
        """
        kept: list = []
        for e in events:
            target = None
            if isinstance(e, m.Event) and len(e.conditions) == 1 and not e.actions \
                    and len(e.children) == 1 and isinstance(e.children[0], m.EventGroup):
                path = [e.children[0]]
                while len(path[-1].children) == 1 and isinstance(path[-1].children[0], m.EventGroup):
                    path.append(path[-1].children[0])
                inner = path[-1].children
                if len(inner) == 1 and isinstance(inner[0], m.Event):
                    scope = kept
                    for g in path:
                        target = next((x for x in scope if isinstance(x, m.EventGroup)
                                       and x.name == g.name and x.active == g.active), None)
                        if target is None:
                            break
                        scope = target.children
                    if target is not None:
                        target.children.append(m.Event(inner[0].line_number, inner[0].sheet_index,
                                                       e.conditions + inner[0].conditions, inner[0].actions,
                                                       inner[0].children))
                        c = e.conditions[0]
                        self.trigger_heads.add((self.dumper.ace(c.oid, c.movement_id, "conditions", c.cnd_id)[1],
                                                c.cnd_id))
                        self.count("triggers_returned_to_groups")
            if target is None:
                kept.append(e)
        return kept

    def event_sheets(self) -> list[dict]:
        ev = self.p.events
        n_layouts = len(self.lv.layouts)
        sheets = []
        names = ev.sheet_names if ev else []
        one_to_one = len(names) == n_layouts and len(set(names)) == len(names)
        if not one_to_one:
            self.note(TR("число листов событий не совпадает с числом layouts (в проекте были include или общие "
                      "листы); создано по одному листу на layout с именем по layout",
            "the number of event sheet names differs from the number of layouts (the project used "
                      "includes or shared sheets); one sheet per layout was created, named after the layout"))
        for k in range(n_layouts):
            events = self.unhoist(copy.deepcopy(ev.layout_events[k])) if ev and k < len(ev.layout_events) else []
            name = names[k] if one_to_one else f"{self.lv.layouts[k].name} events"
            sheets.append({"id": k, "name": name, "events": [self.event(e) for e in events],
                           "header_split": 200, "header_objects": []})
        return sheets

    # -- application ---------------------------------------------------------------
    def build(self) -> Rebuilt:
        if self.lv is None or self.p.app is None:
            raise ValueError("LEVELBLOCK and APPBLOCK must be parsed to rebuild a .cap")
        a, lv = self.p.app, self.lv
        self.assign_ids()
        files = self.files()
        types = [self.object_type(t) for t in lv.object_types]
        families = [self.family(k, f) for k, f in enumerate(lv.families)]
        layouts = [self.layout(k, l) for k, l in enumerate(lv.layouts)]
        animations, images = self.images_and_animations()
        sheets = self.event_sheets()
        if self.stats.get("aces_without_metadata"):
            self.note(TR(f"{self.stats['aces_without_metadata']} условий/действий относятся к плагинам без метаданных: "
                      "их параметры записаны как обычные значения, что неверно для параметров-объектов, "
                      "переменных и списков — проверьте их в редакторе",
            f"{self.stats['aces_without_metadata']} conditions/actions belong to plugins without "
                      "source metadata: their parameters are written as plain values, which is wrong for "
                      "object/variable/combo parameters of those plugins - check them in the editor"))
        python_modules: list[str] = []
        py = self.c.blocks.get("PYTHONLIBS")
        if py is not None:
            try:
                with zipfile.ZipFile(io.BytesIO(py.data)) as z:
                    python_modules = [n for n in z.namelist()[:10000] if n != "StringIO.pyc"]
            except zipfile.BadZipFile:
                self.note(TR("PYTHONLIBS не читается как zip; список модулей Python пуст",
            "PYTHONLIBS is not a readable zip; the Python module list is empty"))
        is_png = all(i.format == "png" for i in self.p.images or [])
        used_plugins = sorted({t.plugin_index for t in lv.object_types})
        traits = DEFAULT_TRAITS + [t.name for t in lv.traits if t.name not in DEFAULT_TRAITS]
        max_anim = max([-1, *self.ix.animations])
        max_image = max([-1, *(i.id for i in self.p.images or [])])
        max_layer = max([-1, *(y.id for l in lv.layouts for y in l.layers)])
        app = {
            "name": a.name, "creator": "Creator", "creator2": "Creator",
            "next_object_id": 0,        # filled in below, after every id has been handed out
            "app_id": 1, "next_layer_id": max_layer + 1, "next_image_id": max_image + 1,
            "next_animation_id": max_anim + 1, "next_event_sheet_id": len(sheets),
            "next_layout_id": len(layouts), "file_path": "", "sampler": a.sampler,
            "window_width": a.window_width, "window_height": a.window_height,
            "eye_distance": a.eye_distance if a.eye_distance is not None else 500.0,
            "run_without_ps": self.p.hlsl.run_without_ps if self.p.hlsl else 1,
            "disable_windows_key": int(a.disable_windows_key), "fps_mode": a.fps_mode, "fps": a.fps,
            "fullscreen": int(a.fullscreen), "show_menu": a.show_menu, "unused1": 0, "hide_maximize": 0,
            "hide_minimize": 0, "unused2": 0, "unused3": 0, "unused4": 0, "unused5": 0, "unused6": 0,
            "fps_in_caption": a.fps_in_caption, "unused7": 0, "use_python": int(py is not None),
            "reserved4": 0, "next_variable_id": len(a.global_variables),
            "runtime": 1 if is_png else 0, "simulate_shaders": a.simulate_shaders,
            "use_motion_blur": int(a.use_motion_blur), "motion_blur_steps": a.motion_blur_steps,
            "text_rendering_mode": a.text_rendering_mode, "override_timedelta": int(a.override_timedelta),
            "time_delta_override": a.time_delta_override, "created_with": CREATED_WITH,
            "plugins": [{"name": self.plugin_name(k), "file": self.ix.plugin_file(k) or ""} for k in used_plugins],
            "caption": int(a.caption), "minimize_box": int(a.minimize_box), "resizable": a.resizable,
            "maximize_box": int(a.maximize_box), "minimum_fps": a.minimum_fps, "multisamples": a.multisamples,
            "texture_loading": a.texture_loading,
            "layouts": layouts, "animations": animations, "images": images,
            "files": files, "sounds": [], "music": [],
            "icons": [self.icon()], "menus": [{"name": "Default", "type": 4, "items": []}],
            "object_types": types, "traits": traits, "families": families,
            "global_variables": [{"name": g.name, "id": k, "type": g.type, "value": g.value}
                                 for k, g in enumerate(a.global_variables)],
            "containers": [{"name": "", "object_types": [self.type_id[o] for o in cont if o in self.type_id]}
                           for cont in lv.containers],
            "system": self.system_type(),
            "transitions": [{"name": t.name, "file": self.effect(t.effect, 0)["file"],
                             "params": self.effect(t.effect, 0)["params"]}
                            for t in (self.p.hlsl.transitions if self.p.hlsl else [])],
            "event_sheets": sheets,
            "controls": [{"name": c.name, "combo_index": CONTROL_VKS.index(c.vk) if c.vk in CONTROL_VKS else 0,
                          "vk": c.vk, "player": c.player if 0 <= c.player <= 9 else 0} for c in a.controls],
            "unused_tree": 0, "global_keys": [], "python_modules": python_modules,
            "object_folders": ["Default"],
        }
        app["next_object_id"] = self._next_id
        if a.global_keys:
            self.note(TR("глобальные ключи плагинов не записаны: редактор не читает непустую коллекцию, "
                      "которую сам же пишет (расхождение чтения и записи в CApplication::Serialize)",
            "global plugin keys are not written: the editor cannot reload a non-empty key "
                      "collection it saved itself (loader/writer mismatch in CApplication::Serialize)"))
        if self.p.raw:
            self.note(TR("в .cap нет данных из нераспознанных блоков: " + ", ".join(sorted(self.p.raw)),
            "blocks that could not be parsed are missing from the .cap: " + ", ".join(sorted(self.p.raw))))
        self.note(TR("данные только для редактора созданы по умолчанию: иконки типов (миниатюры), папки "
                  "объектов, сетка layout, вид листов событий, состояния свёрнутости и блокировки",
            "editor-only data is recreated with defaults: object type icons (thumbnails), object folders, "
                  "layout grid, event sheet layout, expanded/locked states"))
        self.note(TR("позиции и размеры объектов экспортёр округлил до целых; дробные части утрачены",
            "object positions and sizes were stored as integers by the exporter; fractional parts are gone"))
        self.note(TR("события взяты из плоского экспорта: include развёрнуты на месте, отключённых событий и "
                  "комментариев нет, триггеры в под-событиях и Wait — в переписанном экспортёром виде",
            "events come from the flattened export: includes are expanded in place, disabled events and "
                  "comments are absent, triggers inside sub-events and Wait objects appear in rewritten form"))
        if self.stats.get("triggers_returned_to_groups"):
            self.note(TR(f"{self.stats['triggers_returned_to_groups']} событий с триггерами, которые экспортёр вынес из "
                      "групп, возвращены в свои группы (в конец группы)",
            f"{self.stats['triggers_returned_to_groups']} triggered events that the exporter had lifted "
                      "out of their groups were put back (at the end of the group)"))
        return Rebuilt(app, self.p.encoding, self.notes, self.stats, self.trigger_heads)


def build_cap(ix: ProjectIndex, files_dir: Optional[str] = None, thumbnails: bool = True) -> Rebuilt:
    """Build the .cap tree for a parsed project.  ``files_dir`` is where Level 1 put ``files/``."""
    return CapBuilder(ix, files_dir, thumbnails).build()


def versions_used(app: dict) -> dict[str, int]:
    """Class versions the tree will be written with (all latest for rebuilt projects)."""
    return {"CApplication": app.get("_version", LATEST["CApplication"])}
