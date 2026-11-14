"""Reduce a .cap tree (cap_format.read_cap) to an id-free canonical form and compare projects.

Two projects with the same canonical form have the same objects, layouts, animations
and events, whatever edit-time ids, icons and editor UI state they carry.  Used to
compare a .cap rebuilt from an exe with the original .cap.

Normalisations follow what the exporter drops or rewrites (FORMAT.md 5.1): disabled
events/conditions/actions and comments are removed, includes are expanded, object
positions are truncated to integers, ``.X`` is spelled ``Owner.X``.
"""

from __future__ import annotations

import collections
import difflib
import hashlib
import re

from . import model as m
from .cap_format import COMMENT, EVENT, GROUP, INCLUDE, SCRIPT
from .cap_tokens import T_COLOR, T_WHITESPACE, TT_MOVEMENTNAME, TT_OBJECTNAME
from .expressions import token_text
from .plugins import decode_properties


class Canon:
    def __init__(self, app: dict) -> None:
        self.app = app
        self.types = {t["key"]: t["type"] for t in app["object_types"]}
        self.families = {f["id"]: f for f in app["families"]}
        self.globals = {g["id"]: g["name"] for g in app["global_variables"]}
        self.sheets = {s["id"]: s for s in app["event_sheets"]}
        self.images = {i["id"]: hashlib.sha1(i["png"]).hexdigest()[:12] for i in app["images"]}
        self.files = [f["name"] for f in app.get("files", [])]

    def owner_name(self, oid: int) -> str:
        if oid == -1:
            return "System"
        if oid in self.types:
            return self.types[oid]["name"]
        if oid in self.families:
            return self.families[oid]["name"]
        return f"?{oid}"

    def var_name(self, type_id: int, var_id: int) -> str:
        t = self.types.get(type_id)
        if t:
            for v in t["private_variables"]:
                if v["id"] == var_id:
                    return v["name"]
        return f"?var{var_id}"

    # -- expressions ---------------------------------------------------------------
    def expression(self, tokens: list[dict], owner: str) -> str:
        """Whitespace-free, canonically spelled source text."""
        parts: list[str] = []
        prev_type = None
        for t in tokens:
            k = t["type"]
            if k == T_WHITESPACE:
                continue
            if k == T_COLOR:
                s = str(t["id"])
            elif k == m.T_IDENTIFIER:
                if t["subtype"] in (TT_OBJECTNAME, TT_MOVEMENTNAME) and t["owner"] in self.types:
                    s = self.types[t["owner"]]["name"] + (t["text"][t["text"].find("["):] if "[" in t["text"] else "")
                else:
                    s = t["text"]
            elif k == m.T_STRINGLITERAL:
                s = '"' + t["text"] + '"'
            elif k == m.T_VARIABLENAME:
                s = "'" + t["text"] + "'"
            elif k in (m.T_INTEGER, m.T_FLOAT):
                s = t["text"]
                if s == "-":            # editor-mode quirk: a '-' after whitespace is stored as an integer token
                    s = "-"
                elif k == m.T_FLOAT:
                    s = repr(float(s))
            else:
                s = token_text(m.Token(k))
            # `.X` means "my own X": the exporter writes the owner's name
            if k == 13 and (prev_type is None or prev_type not in (m.T_IDENTIFIER, 11, m.T_VARIABLENAME)):
                parts.append(owner)
            parts.append(s)
            prev_type = k
        return "".join(parts).lower()

    def param(self, p: dict, owner: str, type_id: int) -> str:
        toks = [t for t in p["tokens"] if t["type"] != T_WHITESPACE]
        single = toks[0] if len(toks) == 1 else None
        if p["type"] == 6 and single and single["type"] == m.T_INTEGER:
            return "obj:" + self.owner_name(int(single["text"]))
        if p["type"] == 14 and single and single["type"] == m.T_INTEGER:
            return "pv:" + self.var_name(type_id, int(single["text"]))
        if p["type"] == 13 and single and single["type"] == m.T_INTEGER:
            return "gv:" + self.globals.get(int(single["text"]), "?")
        if p["type"] == 17 and single:
            return "tr:" + ("" if single["text"] == "None" else single["text"]).lower()
        return self.expression(p["tokens"], owner)

    # -- events --------------------------------------------------------------------
    def ace(self, a: dict, kind: str) -> tuple:
        owner = self.owner_name(a["oid"])
        mov = ""
        if a["movement_id"] >= 0 and a["oid"] in self.types:
            mov = next((b["name"] for b in self.types[a["oid"]]["behaviors"] if b["id"] == a["movement_id"]), "?")
        elif a["movement_id"] < -1 and a["oid"] in self.types:
            ids = [e["id"] for e in self.types[a["oid"]]["effects"]]
            mov = f"fx{ids.index(-a['movement_id'])}" if -a["movement_id"] in ids else "fx?"
        params = tuple(self.param(p, owner, a["oid"]) for p in a["params"])
        return (kind, owner, mov, a["id"], bool(a.get("negated", 0)), params)

    def events(self, events: list[dict], depth: int = 0) -> list:
        out = []
        for e in events:
            if e["type"] == COMMENT:
                continue
            if e["type"] == INCLUDE:
                sheet = self.sheets.get(e["event_sheet"])
                if sheet and depth < 50:
                    out.extend(self.events(sheet["events"], depth + 1))
                continue
            if e["type"] == SCRIPT:
                out.append(("script", e["text"]))
                continue
            if e["type"] == GROUP:
                out.append(("group", e["title"], bool(e["active"]), self.events(e["events"], depth)))
                continue
            if e["type"] == EVENT:
                on = e.get("enabled", 1)
                conds = [self.ace(c, "c") for c in e["conditions"] if on and c.get("enabled", 1)]
                acts = [self.ace(a, "a") for a in e["actions"] if on and a.get("enabled", 1)]
                out.append(("event", conds, acts, self.events(e["events"], depth)))
        return out

    # -- structure -----------------------------------------------------------------
    def animation(self, a: dict) -> tuple:
        return (a["name"], a["speed"], a["repeat"], a["repeat_to"], bool(a["forever"]), bool(a["pingpong"]),
                bool(a["is_angle"]), a["angle"], a.get("tag", 0),
                tuple(self.images.get(i, f"?{i}") for i in a["images"]), tuple(a["frame_times"]),
                tuple(self.animation(s) for s in a["sub_animations"]))

    def object_types(self) -> dict:
        out = {}
        for t in self.types.values():
            out[t["name"]] = {
                "plugin": t["plugin"],
                "variables": [(v["name"], v["type"]) for v in t["private_variables"]],
                "behaviors": [(b["plugin"], b["name"], b["data"].hex()) for b in t["behaviors"]],
                "effects": [(e["file"], e["name"], e["disable_mode"],
                             tuple((q["type"], q["variable_name"], q["value"], q["name"]) for q in e["params"]))
                            for e in t["effects"]],
                "traits": sorted(t["traits"]), "global": bool(t["global"]), "destroy_when": t["destroy_when"],
            }
        return out

    def layouts(self, mask_animation_root: bool = False) -> list:
        out = []
        for l in self.app["layouts"]:
            objects = {o["key"]: o["object"] for o in l["objects"]}
            layers = []
            for y in l["layers"]:
                inst = []
                for key in y["z_order"]:
                    o = objects[key]
                    t = self.types.get(o["object_type"], {})
                    data = o["data"]
                    if mask_animation_root and o.get("plugin") == "Sprite" and len(data) >= 29:
                        data = data[:25] + b"\0\0\0\0" + data[29:]
                    shown = data.hex()
                    if o.get("plugin") == "Sprite":
                        # Compare Sprite properties by value: plugin builds of different age write
                        # different blob versions (v4 lacks the start animation fields of v5).
                        props, _ = decode_properties("Sprite.csx", data, "latin-1")
                        if props is not None:
                            props.pop("version")
                            props.setdefault("start_animation", "Default")
                            props.setdefault("start_frame", 1)
                            shown = repr(sorted(props.items()))
                    values = {self.var_name(o["object_type"], v["id"]): v["value"] for v in o["values"]}
                    inst.append((t.get("name"), o.get("plugin"), int(o["x"]), int(o["y"]), int(o["width"]),
                                 int(o["height"]), o["angle"] if o["show_angles"] else 0.0, o["filter"],
                                 key, shown, tuple(sorted(values.items()))))
                layers.append((y["name"], y["id"], y["type"], y["angle"], y["color"], y["opacity"],
                               y["scroll_x_rate"], y["scroll_y_rate"], y.get("scroll_x", 0.0), y.get("scroll_y", 0.0),
                               y["zoom_x"], y["zoom_y"], y.get("zoom_x_rate", 1.0), y.get("zoom_y_rate", 1.0),
                               bool(y["clear_background"]), y["background_color"],
                               bool(y.get("force_own_texture", 0)), y.get("sampler", 0), bool(y.get("enable_3d", 0)),
                               tuple(e["name"] for e in y["effects"]), inst))
            out.append((l["name"], l["width"], l["height"], l["color"], bool(l["unbounded_scrolling"]),
                        bool(l["application_background"]), layers))
        return out

    def animations_by_type(self) -> dict:
        """Animation tree of each Sprite type, found through its instances' property blob."""
        anims = {a["id"]: a for a in self.app["animations"]}
        out: dict = {}
        for l in self.app["layouts"]:
            for o in l["objects"]:
                o = o["object"]
                if o.get("plugin") == "Sprite" and len(o.get("data", b"")) >= 29:
                    root = int.from_bytes(o["data"][25:29], "little")
                    t = self.types.get(o["object_type"], {}).get("name")
                    if root in anims:
                        out.setdefault(t, set()).add(self.animation(anims[root]))
        return out

    def event_lists(self) -> list:
        """One event list per layout, as the exporter builds them."""
        out = []
        for l in self.app["layouts"]:
            sheet = self.sheets.get(l["event_sheet_id"])
            out.append(self.events(sheet["events"]) if sheet else [])
        return out

    def application(self) -> dict:
        a = self.app
        keys = ("name", "sampler", "window_width", "window_height", "run_without_ps", "disable_windows_key",
                "fps_mode", "fps", "fullscreen", "show_menu", "fps_in_caption", "use_python", "runtime",
                "simulate_shaders", "use_motion_blur", "motion_blur_steps", "text_rendering_mode",
                "override_timedelta", "time_delta_override", "caption", "minimize_box", "resizable",
                "maximize_box", "minimum_fps", "multisamples", "texture_loading")
        d = {k: a.get(k) for k in keys}
        d["globals"] = [(g["name"], g["type"], g["value"]) for g in a["global_variables"]]
        d["controls"] = [(c["name"], c["vk"], c["combo_index"]) for c in a["controls"]]
        d["containers"] = sorted(tuple(sorted(self.owner_name(o) for o in c["object_types"])) for c in a["containers"])
        d["plugins"] = sorted((p["name"], p["file"]) for p in a["plugins"])
        return d


def triggers_last(events: list, heads: set) -> list:
    """Stable-partition every event list: events led by a trigger condition go last.

    The exporter moves triggered events to the end of the list (and ccdec returns them to the
    end of their group), so position among siblings is not preserved for them.
    """
    def is_trigger(e) -> bool:
        return e[0] == "event" and any((c[1] + (f"[{c[2]}]" if c[2] and not c[2].startswith("fx") else ""), c[3])
                                       in heads for c in e[1])
    out = []
    for e in events:
        if e[0] == "group":
            out.append((e[0], e[1], e[2], triggers_last(e[3], heads)))
        elif e[0] == "event":
            out.append((e[0], e[1], e[2], triggers_last(e[3], heads)))
        else:
            out.append(e)
    return [e for e in out if not is_trigger(e)] + [e for e in out if is_trigger(e)]


def flatten(events: list, path: str = "") -> list[str]:
    """Readable lines for diffing canonical event trees."""
    out = []
    for k, e in enumerate(events):
        here = f"{path}/{k}"
        if e[0] == "group":
            out.append(f"{here} group {e[1]!r} active={e[2]}")
            out += flatten(e[3], here)
        elif e[0] == "event":
            for c in e[1]:
                out.append(f"{here} ? {c[1:]}")
            for a in e[2]:
                out.append(f"{here} > {a[1:]}")
            out += flatten(e[3], here)
        else:
            out.append(f"{here} {e}")
    return out


def strip_positions(lines: list[str]) -> list[str]:
    return [re.sub(r"^\S+ ", "", x) for x in lines]


def compare_trees(a: dict, b: dict, label_a: str = "A", label_b: str = "B",
                  trigger_heads: frozenset = frozenset(), max_lines: int = 60) -> list[str]:
    """Human-readable differences between two .cap trees; an empty list means equivalent.

    Instances are compared as multisets per layer (z-order changes are reported separately),
    events with the exporter's trigger reordering neutralised.
    """
    ca, cb = Canon(a), Canon(b)
    out: list[str] = []
    xa, xb = ca.application(), cb.application()
    for k in xa:
        if xa[k] != xb[k]:
            out.append(f"application.{k}: {label_a}={str(xa[k])[:120]} | {label_b}={str(xb[k])[:120]}")
    ta, tb = ca.object_types(), cb.object_types()
    for n in sorted(set(ta) - set(tb)):
        out.append(f"object type {n!r} only in {label_a}")
    for n in sorted(set(tb) - set(ta)):
        out.append(f"object type {n!r} only in {label_b}")
    for n in sorted(set(ta) & set(tb)):
        for k in ta[n]:
            if ta[n][k] != tb[n][k]:
                out.append(f"object type {n!r}: {k} differs")
    la, lb = ca.layouts(), cb.layouts()
    if len(la) != len(lb):
        out.append(f"layout count: {len(la)} vs {len(lb)}")
    for x, y in zip(la, lb):
        if x[:6] != y[:6]:
            out.append(f"layout {x[0]!r}: properties differ")
        if [l[0] for l in x[6]] != [l[0] for l in y[6]]:
            out.append(f"layout {x[0]!r}: layers {[l[0] for l in x[6]]} vs {[l[0] for l in y[6]]}")
            continue
        for l1, l2 in zip(x[6], y[6]):
            if l1[:-1] != l2[:-1]:
                out.append(f"layout {x[0]!r} layer {l1[0]!r}: properties differ")
            strip = lambda inst: [i[:8] + i[9:] for i in inst]          # drop the instance id
            m1, m2 = collections.Counter(strip(l1[-1])), collections.Counter(strip(l2[-1]))
            if m1 != m2:
                only1, only2 = sum((m1 - m2).values()), sum((m2 - m1).values())
                names = sorted({str(i[0]) for i in list((m1 - m2)) + list((m2 - m1))})
                out.append(f"layout {x[0]!r} layer {l1[0]!r}: {only1} instances only in {label_a}, {only2} only "
                           f"in {label_b} (objects: {', '.join(names[:12])})")
            elif strip(l1[-1]) != strip(l2[-1]):
                out.append(f"layout {x[0]!r} layer {l1[0]!r}: same instances, different z-order")
    aa, ab = ca.animations_by_type(), cb.animations_by_type()
    for n in sorted(set(aa) | set(ab), key=str):
        if aa.get(n) != ab.get(n):
            out.append(f"animations of {n!r} differ")
    ea = [triggers_last(x, trigger_heads) for x in ca.event_lists()]
    eb = [triggers_last(x, trigger_heads) for x in cb.event_lists()]
    for k, (x, y) in enumerate(zip(ea, eb)):
        fx, fy = flatten(x), flatten(y)
        if fx != fy:
            diff = list(difflib.unified_diff(fx, fy, label_a, label_b, lineterm="", n=0))
            out.append(f"events of layout {k}: {len(diff) - 2} differing lines")
            out += ["    " + line[:200] for line in diff[2:max_lines]]
    return out