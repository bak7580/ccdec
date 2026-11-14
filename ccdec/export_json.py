"""Level 2: a readable, structured dump of the project (``project.json``).

Ids are resolved to names wherever the exe allows it: object types, behaviors,
families, global/private variables, layouts, images, files.  Every decoded value
keeps a pointer back to its raw form where decoding involved interpretation
(``raw_hex`` for plugin properties, ``tokens`` for expressions), so nothing that
was in the exe is hidden behind a guess.
"""

from __future__ import annotations

import base64
import json
import os
import re
from typing import Optional, Union

from . import __version__
from . import model as m
from .aces import AceInfo, ParamInfo, system_ace
from .expressions import render, single_int, varindex
from .index import ProjectIndex

COMPARISONS = ["Equal to", "Not equal to", "Less than", "Less or equal", "Greater than", "Greater or equal"]
VK_NAMES = {8: "Backspace", 9: "Tab", 13: "Enter", 16: "Shift", 17: "Control", 18: "Alt", 19: "Pause",
            20: "Caps Lock", 27: "Escape", 32: "Space", 33: "Page Up", 34: "Page Down", 35: "End", 36: "Home",
            37: "Left arrow", 38: "Up arrow", 39: "Right arrow", 40: "Down arrow", 45: "Insert", 46: "Delete",
            91: "Left Windows", 92: "Right Windows", 93: "Applications", 106: "Numpad *", 107: "Numpad +",
            109: "Numpad -", 110: "Numpad .", 111: "Numpad /", 144: "Num Lock", 145: "Scroll Lock",
            160: "Left Shift", 161: "Right Shift", 162: "Left Control", 163: "Right Control",
            164: "Left Alt", 165: "Right Alt", 186: ";", 187: "=", 188: ",", 189: "-", 190: ".", 191: "/",
            192: "`", 219: "[", 220: "\\", 221: "]", 222: "'"}
VK_NAMES.update({48 + i: str(i) for i in range(10)})
VK_NAMES.update({65 + i: chr(65 + i) for i in range(26)})
VK_NAMES.update({96 + i: f"Numpad {i}" for i in range(10)})
VK_NAMES.update({112 + i: f"F{i + 1}" for i in range(24)})


def color(value: int) -> dict:
    """COLORREF 0x00BBGGRR (alpha byte sometimes used as opacity)."""
    return {"r": value & 0xFF, "g": (value >> 8) & 0xFF, "b": (value >> 16) & 0xFF,
            "a": (value >> 24) & 0xFF, "hex": f"#{value & 0xFF:02x}{(value >> 8) & 0xFF:02x}{(value >> 16) & 0xFF:02x}"}


def _strip_markup(text: str) -> str:
    text = re.sub(r"<img>.*?</img>", "", text)
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)
    return re.sub(r"\s+", " ", text).strip()


class Dumper:
    def __init__(self, ix: ProjectIndex, include_tokens: bool = True) -> None:
        self.ix = ix
        self.p = ix.project
        self.include_tokens = include_tokens
        self.unknown_aces: dict[str, int] = {}
        self.property_problems: dict[str, str] = {}

    # -- helpers ----------------------------------------------------------------
    def image_ref(self, image_id: int) -> Union[str, int]:
        return self.ix.image_files.get(image_id, image_id)

    def props(self, plugin_index: int, data: bytes, owner: str) -> dict:
        decoded, problem = self.ix.properties(plugin_index, data)
        out: dict = {"plugin": self.ix.plugin_file(plugin_index)}
        if decoded is not None:
            if "color" in decoded:
                decoded = {**decoded, "color": color(decoded["color"])}
            if "image" in decoded:
                decoded = {**decoded, "image": self.image_ref(decoded["image"])}
            out["properties"] = decoded
        elif problem:
            self.property_problems.setdefault(f"{out['plugin']}: {problem}", owner)
        out["raw_hex"] = data.hex()
        return out

    def ace(self, oid: int, movement_id: int, kind: str, ace_id: int) -> tuple[Optional[AceInfo], str]:
        """(metadata, owner display name) for a condition/action."""
        if oid == -1:
            info = system_ace(kind, ace_id)
            if info is None:
                self.unknown_aces[f"System {kind[:-1]} {ace_id}"] = 1
            return info, "System"
        owner = self.ix.type_name(oid)
        plugin = self.ix.plugin_of(oid)
        if movement_id >= 0:
            b = self.ix.behavior(oid, movement_id)
            if b is None:
                return None, f"{owner}[behavior {movement_id}]"
            owner, plugin = f"{owner}[{b.name}]", b.plugin_index
        elif movement_id <= -2:
            return None, f"{owner} (effect {-2 - movement_id})"
        if plugin is None or not 0 <= plugin < len(self.ix.plugin_aces):
            return None, owner
        info = self.ix.plugin_aces[plugin].get(kind, ace_id)
        if info is None:
            self.unknown_aces[f"{self.ix.plugin_file(plugin)} {kind[:-1]} {ace_id}"] = 1
        return info, owner

    def param(self, tokens: list[m.Token], info: Optional[ParamInfo], caller_oid: int) -> dict:
        d: dict = {"expression": render(tokens)}
        if info is not None:
            d["name"], d["type"] = info.name, info.type_name
            value = single_int(tokens)
            lv = self.p.level
            if info.type == 6 and value is not None:                      # object
                d["object"] = self.ix.type_name(int(value))
            elif info.type == 9 and value is not None:                    # combo
                options = info.init.split("|") if info.init else []
                if 0 <= value < len(options):
                    d["option"] = options[value]
            elif info.type == 13 and value is not None and self.p.app:    # global variable index
                g = self.p.app.global_variables
                if 0 <= value < len(g):
                    d["global_variable"] = g[value].name
            elif info.type == 14:                                         # private variable
                vi = varindex(tokens)
                if vi and lv:
                    t = self.ix.object_type(vi[1])
                    if t and 0 <= vi[0] < len(t.private_variables):
                        d["private_variable"] = t.private_variables[vi[0]].name
            elif info.type == 7 and value is not None:                    # keyboard
                d["key"] = VK_NAMES.get(int(value), f"VK {value}")
            elif info.type == 3 and value is not None:                    # colour
                d["color"] = color(int(value))
            elif info.type == 16 and value is not None:                   # file resource
                d["file"] = f"files/file_{value:04d}"
        if self.include_tokens:
            d["tokens"] = [[t.name] if t.value is None else [t.name, t.value] for t in tokens]
        return d

    def display(self, info: Optional[AceInfo], owner: str, params: list[dict]) -> str:
        if info is None or not info.display:
            name = info.script if info else "?"
            return f"{owner}: {name}(" + ", ".join(p["expression"] for p in params) + ")"
        text = info.display

        def sub(mt: re.Match) -> str:
            k = int(mt.group(1))
            if k >= len(params):
                return mt.group(0)
            p = params[k]
            for key in ("object", "option", "global_variable", "private_variable", "key"):
                if key in p:
                    return str(p[key])
            return p["expression"]

        text = re.sub(r"%(\d)", sub, text)
        has_owner = "%n" in text or "%o" in text
        text = text.replace("%o", "").replace("%n", owner)
        text = _strip_markup(text)
        return text if has_owner or owner == "System" else f"{owner}: {text}"

    # -- sections ----------------------------------------------------------------
    def application(self) -> Optional[dict]:
        a = self.p.app
        if a is None:
            return None
        return {
            "name": a.name, "window": {"width": a.window_width, "height": a.window_height,
                                       "caption": a.caption, "minimize_box": a.minimize_box,
                                       "maximize_box": a.maximize_box, "resizable": a.resizable,
                                       "fullscreen": a.fullscreen, "show_menu": a.show_menu,
                                       "disable_windows_key": a.disable_windows_key},
            "eye_distance": a.eye_distance, "screensaver": a.screensaver,
            "fps_mode": a.fps_mode, "fps": a.fps, "fps_in_caption": a.fps_in_caption,
            "minimum_fps": a.minimum_fps, "sampler": ["point", "linear"][a.sampler] if a.sampler in (0, 1) else a.sampler,
            "text_rendering_mode": a.text_rendering_mode, "multisamples": a.multisamples,
            "texture_loading": a.texture_loading, "simulate_shaders": a.simulate_shaders,
            "motion_blur": {"enabled": a.use_motion_blur, "steps": a.motion_blur_steps},
            "override_timedelta": {"enabled": a.override_timedelta, "value": a.time_delta_override},
            "original_cap_path": a.cap_path,
            "global_variables": [{"name": g.name, "type": ["number", "text"][g.type] if g.type in (0, 1) else g.type,
                                  "value": g.value} for g in a.global_variables],
            "controls": [{"name": c.name, "key": VK_NAMES.get(c.vk, f"VK {c.vk}"), "vk": c.vk,
                          "player": c.player if 0 <= c.player <= 9 else None} for c in a.controls],
            "global_keys": [{"key": k.key, "value": k.value} for k in a.global_keys],
        }

    def plugins(self) -> list[dict]:
        out = []
        for pa in self.ix.plugin_aces:
            plug = self.ix.container.plugins[pa.plugin_index] if pa.plugin_index < len(self.ix.container.plugins) else None
            out.append({"index": pa.plugin_index, "file": pa.file_name,
                        "sha1": plug.sha1 if plug else None, "built": plug.timestamp if plug else None,
                        "source_match": pa.source, "source_match_ratio": pa.match_ratio,
                        "unverified_aces": len(pa.mismatches)})
        return out

    def object_types(self) -> list[dict]:
        lv = self.p.level
        if lv is None:
            return []
        effects = {o.oid: o.effects for o in self.p.hlsl.object_effects} if self.p.hlsl else {}
        families = {}
        for f in lv.families:
            for oid in f.oids:
                families.setdefault(oid, []).append(f.name)
        traits = {}
        for t in lv.traits:
            for oid in t.oids:
                traits.setdefault(oid, []).append(t.name)
        containers = {}
        for k, cont in enumerate(lv.containers):
            for oid in cont:
                containers[oid] = [self.ix.type_name(o) for o in cont]
        out = []
        for t in lv.object_types:
            d = {
                "name": t.name, "id": t.oid, "plugin": self.ix.plugin_file(t.plugin_index),
                "global": t.is_global, "destroy_when": t.destroy_when,
                "private_variables": [{"name": v.name, "type": ["number", "text"][v.type] if v.type in (0, 1) else v.type}
                                      for v in t.private_variables],
                "behaviors": [{"name": b.name, "plugin": self.ix.plugin_file(b.plugin_index),
                               **{k: v for k, v in self.props(b.plugin_index, b.data, f"{t.name}[{b.name}]").items()
                                  if k != "plugin"}}
                              for b in self.ix.behaviors_by_owner.get(t.oid, [])],
                "families": families.get(t.oid, []), "attributes": traits.get(t.oid, []),
                "container": containers.get(t.oid),
                "effects": [self.effect(e) for e in effects.get(t.oid, [])],
            }
            roots = self.ix.type_animation_roots.get(t.oid)
            if roots:
                d["animations"] = [self.animation(self.ix.animations[r]) for r in roots if r in self.ix.animations]
            if t.reserved != b"\0" * 16:
                d["reserved_hex"] = t.reserved.hex()
            out.append(d)
        return out

    def effect(self, e: m.EffectUse) -> dict:
        shader = self.p.hlsl.shaders[e.shader_index] if self.p.hlsl and 0 <= e.shader_index < len(self.p.hlsl.shaders) else None
        return {"name": e.name, "file": shader.path if shader else e.shader_index, "disable_mode": e.disable_mode,
                "params": [{"name": q.name, "variable": q.variable_name, "type": q.type, "value": q.value}
                           for q in e.params]}

    def animation(self, a: m.Animation) -> dict:
        d: dict = {"id": a.id, "name": a.name, "speed": a.speed, "repeat": "forever" if a.repeat == -1 else a.repeat,
                   "repeat_to": a.repeat_to, "pingpong": bool(a.pingpong)}
        if a.tag:
            d["tag"] = a.tag
        if a.is_angle:
            d["angle"] = a.angle
        if a.frames:
            d["frames"] = [{"image": self.image_ref(f.image_id), "time": f.time_factor} for f in a.frames]
        if a.sub_animations:
            key = "angles" if all(s.is_angle for s in a.sub_animations) else "animations"
            d[key] = [self.animation(s) for s in a.sub_animations]
        return d

    def layouts(self) -> list[dict]:
        lv = self.p.level
        if lv is None:
            return []
        layer_fx = {}
        if self.p.hlsl:
            for le in self.p.hlsl.layer_effects:
                layer_fx[(le.layout_index, le.layer_index)] = le.effects
        out = []
        for li, l in enumerate(lv.layouts):
            # Runtime order puts the editor's first layer last (CExport::GenerateLayout).
            order = [len(l.layers) - 1, *range(len(l.layers) - 1)] if l.layers else []
            layers = []
            for editor_pos, k in enumerate(order):
                y = l.layers[k]
                layers.append({
                    "name": y.name, "id": y.id, "editor_index": editor_pos, "type": y.type,
                    "opacity": y.opacity, "filter": color(y.filter), "angle": y.angle,
                    "scroll": {"x_rate": y.scroll_x_factor, "y_rate": y.scroll_y_factor,
                               "x_offset": y.scroll_x, "y_offset": y.scroll_y},
                    "zoom": {"x_rate": y.zoom_x_factor, "y_rate": y.zoom_y_factor,
                             "x_offset": y.zoom_x, "y_offset": y.zoom_y},
                    "clear_background": bool(y.clear_background), "background_color": color(y.background_color),
                    "force_own_texture": y.force_own_texture, "sampler": y.sampler, "enable_3d": y.enable_3d,
                    "clear_depth_buffer": y.clear_depth_buffer,
                    # HLSL stores the runtime layer index, or -1 for the editor's first layer.
                    "effects": [self.effect(e) for e in layer_fx.get((li, -1 if k == len(l.layers) - 1 else k), [])],
                    "instances": [self.instance(i) for i in y.instances],
                })
            sheet = self.p.events.sheet_names[li] if self.p.events and li < len(self.p.events.sheet_names) else None
            out.append({"name": l.name, "width": l.width, "height": l.height, "color": color(l.color),
                        "unbounded_scrolling": l.unbounded_scrolling,
                        "application_background": l.application_background,
                        "texture_loading": l.texture_loading, "keys": [{"key": k.key, "value": k.value} for k in l.keys],
                        "event_sheet_guess": sheet, "layers": layers,
                        "images_used": [self.image_ref(i) for i in l.image_ids]})
        return out

    def instance(self, i: m.Instance) -> dict:
        t = self.ix.object_type(i.oid)
        d: dict = {"object": self.ix.type_name(i.oid), "uid": i.instance_id, "x": i.x, "y": i.y,
                   "width": i.width, "height": i.height, "angle": i.angle, "filter": color(i.filter)}
        if t is not None:
            d["private_variables"] = {v.name: val for v, val in zip(t.private_variables, i.private_values)}
            d.update({k: v for k, v in self.props(t.plugin_index, i.data, t.name).items() if k != "plugin"})
        return d

    def events(self) -> Optional[dict]:
        ev = self.p.events
        if ev is None:
            return None
        names = self.p.level.layouts if self.p.level else []
        return {
            "sheet_names": ev.sheet_names,
            "note": "the exe has one flattened event list per layout: includes are expanded, disabled "
                    "events and comments are absent, triggers in sub-events were lifted to the top level",
            "layouts": [{"layout": names[k].name if k < len(names) else k,
                         "events": [self.event_line(e) for e in lst]} for k, lst in enumerate(ev.layout_events)],
        }

    def event_line(self, e: Union[m.Event, m.EventGroup]) -> dict:
        if isinstance(e, m.EventGroup):
            return {"group": e.name, "active": e.active, "events": [self.event_line(x) for x in e.children]}
        sheet = self.p.events.sheet_names[e.sheet_index] if 0 <= e.sheet_index < len(self.p.events.sheet_names) else e.sheet_index
        d: dict = {"line": e.line_number, "sheet": sheet,
                   "conditions": [self.condition(c) for c in e.conditions],
                   "actions": [self.action(a) for a in e.actions]}
        if e.children:
            d["sub_events"] = [self.event_line(x) for x in e.children]
        return d

    def _ace_params(self, info: Optional[AceInfo], params: list[list[m.Token]], oid: int) -> list[dict]:
        infos = list(info.params) if info and len(info.params) == len(params) else [None] * len(params)
        return [self.param(tok, pi, oid) for tok, pi in zip(params, infos)]

    def condition(self, c: m.Condition) -> dict:
        info, owner = self.ace(c.oid, c.movement_id, "conditions", c.cnd_id)
        params = self._ace_params(info, c.params, c.oid)
        d = {"object": owner, "id": c.cnd_id, "name": info.script or info.name if info else None,
             "negated": c.negated, "text": ("NOT " if c.negated else "") + self.display(info, owner, params),
             "params": params}
        if info is None or info.origin == "exe":
            d["metadata"] = "script name only" if info else "unknown"
        return d

    def action(self, a: m.Action) -> dict:
        info, owner = self.ace(a.oid, a.movement_id, "actions", a.act_id)
        params = self._ace_params(info, a.params, a.oid)
        d = {"object": owner, "id": a.act_id, "name": info.script or info.name if info else None,
             "text": self.display(info, owner, params), "params": params}
        if a.oid == -1 and a.act_id == 45 and a.params:                 # Run Script (ExportScript::Export)
            first = a.params[0]
            if len(first) == 1 and first[0].type == m.T_STRINGLITERAL:
                d["python_script"] = first[0].value
        if info is None or info.origin == "exe":
            d["metadata"] = "script name only" if info else "unknown"
        return d

    def project(self) -> dict:
        c = self.ix.container
        lv = self.p.level
        return {
            "format": "ccdec project dump", "ccdec_version": __version__,
            "source": {"file": os.path.basename(c.path), "size": c.size, "runtime_timestamp": c.pe_timestamp,
                       "layout_profiles": self.p.profiles, "string_encoding": self.p.encoding,
                       "unparsed_blocks": sorted(self.p.raw)},
            "application": self.application(),
            "plugins": self.plugins(),
            "object_types": self.object_types(),
            "families": [{"name": f.name, "members": [self.ix.type_name(o) for o in f.oids],
                          "private_variables": [v.name for v in f.private_variables]} for f in (lv.families if lv else [])],
            "attributes": [{"name": t.name, "members": [self.ix.type_name(o) for o in t.oids]} for t in (lv.traits if lv else [])],
            "containers": [[self.ix.type_name(o) for o in cont] for cont in (lv.containers if lv else [])],
            "layouts": self.layouts(),
            "events": self.events(),
            "orphaned_animations": [self.animation(a) for a in (lv.animations if lv else [])
                                    if a.id not in {r for v in self.ix.type_animation_roots.values() for r in v}],
            "effects": [{"file": s.path, "pixel_shader": f"{s.major}.{s.minor}", "cross_sampling": s.cross_sampling,
                         "border_mode": s.border_mode, "fullscreen_mode": s.fullscreen_mode}
                        for s in (self.p.hlsl.shaders if self.p.hlsl else [])],
            "transitions": [{"name": t.name, **self.effect(t.effect)} for t in (self.p.hlsl.transitions if self.p.hlsl else [])],
            "raw_blocks_base64": {k: base64.b64encode(v).decode() for k, v in self.p.raw.items()},
        }


def export_json(ix: ProjectIndex, out_dir: str, include_tokens: bool = True) -> tuple[dict, Dumper]:
    dumper = Dumper(ix, include_tokens)
    data = dumper.project()
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "project.json"), "w", encoding="utf-8", errors="backslashreplace") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return data, dumper
