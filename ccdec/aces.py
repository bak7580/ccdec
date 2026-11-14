"""ACE (condition / action / expression) metadata, verified against the exe.

Two sources are combined:

* the ACE tables inside the exe (LEVELBLOCK, one per plugin): authoritative ids,
  script names and parameter counts for every plugin actually used, including
  third-party ones;
* ``data/aces.json`` generated from the Construct Classic sources by
  ``tools/gen_aces.py``: display texts, parameter names and parameter types for
  the stock plugins and for the System object (which has no table in the exe).

A source entry is used for a plugin ACE only when its script name and parameter
count agree with the exe's entry at the same id.  Everything else falls back to
what the exe itself says, and the fallback is reported.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Optional

from . import model as m

KINDS = ("conditions", "actions", "expressions")

# Common/internal.hpp :: enum PARAMS (same numbers as EDITORPARAM_* in the editor)
PARAM_TYPE_NAMES = {
    1: "value", 2: "string", 3: "color", 4: "position", 5: "zone", 6: "object", 7: "keyboard",
    8: "mouse", 9: "combo", 10: "any", 11: "array", 12: "layer", 13: "global_variable",
    14: "private_variable", 15: "menu", 16: "file", 17: "transition", 18: "music", 19: "sound",
}


@dataclass(frozen=True)
class ParamInfo:
    type: int
    name: str
    init: str = ""

    @property
    def type_name(self) -> str:
        return PARAM_TYPE_NAMES.get(self.type, f"type_{self.type}")


@dataclass(frozen=True)
class AceInfo:
    script: str
    param_count: int
    name: str = ""
    category: str = ""
    display: str = ""
    params: tuple[ParamInfo, ...] = ()
    #: "exe+source" (verified), "exe+cap" (types learned from a .cap), "exe" (script name only)
    #: or "source" (System object)
    origin: str = "exe"


@lru_cache(maxsize=1)
def load_database() -> dict:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "aces.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def load_learned() -> dict:
    """Parameter types mined from .cap files by tools/learn_aces.py (may be absent)."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "aces_learned.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _info_from_source(entry: dict, origin: str, script: Optional[str] = None) -> AceInfo:
    params = tuple(ParamInfo(p["type"] if isinstance(p["type"], int) else 10, str(p.get("name", "")),
                             str(p.get("init", ""))) for p in entry.get("params", []))
    return AceInfo(script=script if script is not None else str(entry.get("script", "")),
                   param_count=len(params), name=str(entry.get("name", "")).lstrip("*"),
                   category=str(entry.get("category", "")), display=str(entry.get("display", "")),
                   params=params, origin=origin)


@dataclass
class PluginAces:
    """ACE metadata of one embedded plugin."""

    plugin_index: int
    file_name: Optional[str]
    source: Optional[str] = None            # matched source folder, e.g. "Plugins/Text"
    match_ratio: float = 0.0
    tables: dict[str, dict[int, AceInfo]] = field(default_factory=dict)
    mismatches: list[str] = field(default_factory=list)

    def get(self, kind: str, ace_id: int) -> Optional[AceInfo]:
        return self.tables.get(kind, {}).get(ace_id)


def _exe_table(table: m.AceTable) -> dict[str, list[m.AceEntry]]:
    return {"conditions": table.conditions, "actions": table.actions, "expressions": table.expressions}


def _score(exe: dict[str, list[m.AceEntry]], src: dict) -> tuple[int, int]:
    hit = total = 0
    for kind in KINDS:
        entries = src.get(kind, {})
        for i, e in enumerate(exe[kind]):
            if not e.script_name:
                continue
            total += 1
            s = entries.get(str(i))
            if s and str(s.get("script", "")).lower() == e.script_name.lower() \
                    and len(s.get("params", [])) == e.param_count:
                hit += 1
    return hit, total


def match_plugin(plugin_index: int, file_name: Optional[str], table: Optional[m.AceTable],
                 display_name: Optional[str] = None) -> PluginAces:
    """Pair an exe ACE table with the best-matching source plugin."""
    result = PluginAces(plugin_index, file_name)
    learned = load_learned().get(display_name or "", {})
    if table is None:
        return result
    exe = _exe_table(table)
    # A source plugin is accepted when its output file name matches and at least half
    # of the named ACEs agree, or - for renamed builds - when nearly all of a
    # reasonably large table agrees.  Small tables of unrelated plugins can share
    # a couple of names (e.g. DragDrop vs SelectionBox), hence the size floor.
    best, best_rank, best_ratio = None, -1.0, 0.0
    for src in load_database()["plugins"]:
        hit, total = _score(exe, src)
        ratio = hit / total if total else 0.0
        same_name = bool(file_name and src.get("dll") and src["dll"].lower() == file_name.lower())
        if not ((same_name and ratio >= 0.5) or (ratio >= 0.9 and hit >= 5)):
            continue
        rank = ratio + (1.0 if same_name else 0.0)
        if rank > best_rank:
            best, best_rank, best_ratio = src, rank, ratio
    if best is not None:
        result.source, result.match_ratio = best["source"], round(best_ratio, 3)
    for kind in KINDS:
        out: dict[int, AceInfo] = {}
        entries = best.get(kind, {}) if result.source else {}
        for i, e in enumerate(exe[kind]):
            if not e.script_name and e.param_count == 0:
                continue
            s = entries.get(str(i))
            if s and str(s.get("script", "")).lower() == e.script_name.lower() \
                    and len(s.get("params", [])) == e.param_count:
                out[i] = _info_from_source(s, "exe+source", e.script_name)
            else:
                if result.source:
                    result.mismatches.append(f"{kind[:-1]} {i} ({e.script_name or '<unnamed>'})")
                known = learned.get(kind, {}).get(str(i))
                if known and len(known["params"]) == e.param_count:
                    # parameter types seen in a real .cap (tools/learn_aces.py); names are not known
                    out[i] = AceInfo(script=e.script_name, param_count=e.param_count, name=e.script_name,
                                     params=tuple(ParamInfo(pt, "") for pt in known["params"]), origin="exe+cap")
                else:
                    out[i] = AceInfo(script=e.script_name, param_count=e.param_count, name=e.script_name)
        result.tables[kind] = out
    return result


def system_ace(kind: str, ace_id: int) -> Optional[AceInfo]:
    entry = load_database()["system"].get(kind, {}).get(str(ace_id))
    return _info_from_source(entry, "source") if entry else None


def build_plugin_aces(project: m.Project, plugin_files: list[Optional[str]],
                      display_names: Optional[list[Optional[str]]] = None) -> list[PluginAces]:
    """One PluginAces per plugin index, from the ACE tables found in LEVELBLOCK."""
    tables: dict[int, m.AceTable] = {}
    if project.level:
        for t in project.level.object_types:
            if t.ace and t.plugin_index not in tables:
                tables[t.plugin_index] = t.ace
        for b in project.level.behaviors:
            if b.ace and b.plugin_index not in tables:
                tables[b.plugin_index] = b.ace
    names = display_names or [None] * len(plugin_files)
    return [match_plugin(i, name, tables.get(i), names[i] if i < len(names) else None)
            for i, name in enumerate(plugin_files)]
