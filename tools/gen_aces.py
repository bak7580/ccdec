"""Generate ccdec/data/aces.json from the Construct Classic source tree.

The exported .exe only carries script names and parameter counts for plugin
conditions/actions/expressions, and nothing at all for the System object.  The
display texts, parameter names and *parameter types* live in the editor-side
source code:

* plugins and behaviors:  ``DefineACES`` built from ADDPARAM*/ADDCND/ADDACT/ADDEXP
  macros (Common/internal.hpp) plus Common/CommonAceTable.hpp (ADD*_AT, explicit ids);
* System object:  IDE/Structure/SystemObject.cpp, a switch over ids filling an
  ``ACESEntry2`` by hand, with strings resolved through the language files
  (IDE/Languages/English (UK)/<Section>.txt via GetString(section, key)).

This script is a small purpose-built interpreter for exactly those patterns.  It
never compiles or runs anything.  Its output is *verified at use time* against
the ACE tables inside each exe (ccdec/aces.py), so a parse inaccuracy here shows
up as a reported mismatch, not as silently wrong data.

Usage:  python tools/gen_aces.py [vendor/construct-src] [ccdec/data/aces.json]
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Optional

# Editor-side numbering of plugin ACE ids: the first EditAce call fills 255 blank
# slots for the common ACEs; a plugin's own ACEs are appended after them
# (IDE/Construct.cpp :: MicroAceTool::EditAce).
OWN_ACE_BASE = 255

PARAM_ENUM = {  # Common/internal.hpp :: enum PARAMS
    "PARAM_VALUE": 1, "PARAM_STRING": 2, "PARAM_COLOR": 3, "PARAM_POSITION": 4, "PARAM_ZONE": 5,
    "PARAM_OBJECT": 6, "PARAM_KEYBOARD": 7, "PARAM_MOUSE": 8, "PARAM_COMBO": 9, "PARAM_UNDEFINED": 10,
    "PARAM_ARRAY": 11, "PARAM_LAYER": 12, "PARAM_GLOBALVARIABLE": 13, "PARAM_PRIVATEVARIABLE": 14,
    "PARAM_MENURESOURCE": 15, "PARAM_BINARYRESOURCE": 16, "PARAM_TRANSITION": 17,
}


# --- tiny C lexing helpers -----------------------------------------------------
def strip_comments(src: str) -> str:
    out, i, n = [], 0, len(src)
    while i < n:
        c = src[i]
        if c in "\"'":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            out.append(src[i:j + 1])
            i = j + 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            out.append("\n" * src.count("\n", i, n if j < 0 else j))
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def split_args(s: str) -> list[str]:
    """Split a macro/function argument list at top-level commas."""
    args, depth, cur, i = [], 0, [], 0
    while i < len(s):
        c = s[i]
        if c in "\"'":
            j = i + 1
            while j < len(s) and s[j] != c:
                j += 2 if s[j] == "\\" else 1
            cur.append(s[i:j + 1])
            i = j + 1
            continue
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
        if c == "," and depth == 0:
            args.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        args.append("".join(cur).strip())
    return args


def matching(s: str, open_pos: int) -> int:
    """Index of the bracket matching s[open_pos]."""
    pairs = {"(": ")", "{": "}", "[": "]"}
    want, depth, i = pairs[s[open_pos]], 0, open_pos
    while i < len(s):
        c = s[i]
        if c in "\"'":
            j = i + 1
            while j < len(s) and s[j] != c:
                j += 2 if s[j] == "\\" else 1
            i = j + 1
            continue
        if c == s[open_pos]:
            depth += 1
        elif c == want:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ValueError("unbalanced brackets")


_ESC = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "'": "'", "0": "\0"}


def c_string(lit: str) -> str:
    body, out, i = lit[1:-1], [], 0
    while i < len(body):
        if body[i] == "\\" and i + 1 < len(body):
            out.append(_ESC.get(body[i + 1], body[i + 1]))
            i += 2
        else:
            out.append(body[i])
            i += 1
    return "".join(out)


class Evaluator:
    """Evaluates the constant expressions that appear in ACE definitions."""

    def __init__(self, defines: dict[str, str], lang: dict[str, dict[str, str]]) -> None:
        self.defines, self.lang = defines, lang

    def __call__(self, expr: str, depth: int = 0):
        expr = expr.strip()
        if depth > 20:
            return expr
        if re.fullmatch(r'(\s*"(?:[^"\\]|\\.)*"\s*)+', expr):
            return "".join(c_string(m) for m in re.findall(r'"(?:[^"\\]|\\.)*"', expr))
        if re.fullmatch(r"-?\d+", expr):
            return int(expr)
        if re.fullmatch(r"0x[0-9a-fA-F]+", expr):
            return int(expr, 16)
        if expr in PARAM_ENUM:
            return PARAM_ENUM[expr]
        m = re.fullmatch(r'GetString\(\s*"([^"]*)"\s*,\s*"([^"]*)"\s*\)', expr)
        if m:
            return self.lang.get(m.group(1).lower(), {}).get(m.group(2), f"<{m.group(1)}:{m.group(2)}>")
        if re.fullmatch(r"[A-Za-z_]\w*", expr) and expr in self.defines:
            return self(self.defines[expr], depth + 1)
        if expr.startswith("(") and expr.endswith(")") and matching(expr, 0) == len(expr) - 1:
            return self(expr[1:-1], depth + 1)
        return expr      # leave symbolic (e.g. flag combinations)


def load_language(ide: str) -> dict[str, dict[str, str]]:
    lang: dict[str, dict[str, str]] = {}
    folder = os.path.join(ide, "Languages", "English (UK)")
    for name in os.listdir(folder):
        if not name.endswith(".txt"):
            continue
        table = lang.setdefault(name[:-4].lower(), {})
        with open(os.path.join(folder, name), encoding="latin-1") as f:
            for line in f:
                if "=" in line:
                    k, v = line.rstrip("\r\n").split("=", 1)
                    table[k.strip()] = v
    return lang


def collect_defines(paths: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in paths:
        with open(p, encoding="latin-1") as f:
            for m in re.finditer(r"^\s*#define\s+([A-Za-z_]\w*)(?:[ \t]+(.+))?$", strip_comments(f.read()), re.M):
                out.setdefault(m.group(1), (m.group(2) or "1").strip())
    return out


# --- plugins / behaviors -------------------------------------------------------
def preprocess(src: str, path: str, common: str, defines: dict[str, str], depth: int = 0) -> str:
    """Resolve #ifdef/#ifndef/#if 0/#else/#endif, #define and the CommonAceTable include."""
    out: list[str] = []
    stack: list[bool] = []          # active state of each open conditional

    def active() -> bool:
        return all(stack)

    for line in strip_comments(src).splitlines():
        s = line.strip()
        m = re.match(r"#\s*(\w+)\s*(.*)", s)
        if not m:
            if active():
                out.append(line)
            continue
        kw, rest = m.group(1), m.group(2).strip()
        if kw == "ifdef":
            stack.append(rest.split()[0] in defines if rest else False)
        elif kw == "ifndef":
            stack.append(rest.split()[0] not in defines if rest else True)
        elif kw == "if":
            stack.append(rest not in ("0", "false"))
        elif kw == "elif":
            if stack:
                stack[-1] = False
        elif kw == "else":
            if stack:
                stack[-1] = not stack[-1]
        elif kw == "endif":
            if stack:
                stack.pop()
        elif kw == "define" and active():
            parts = rest.split(None, 1)
            if parts:
                defines[parts[0]] = parts[1] if len(parts) > 1 else "1"
        elif kw == "include" and active() and "commonacetable" in rest.lower() and depth == 0:
            with open(common, encoding="latin-1") as f:
                out.append(preprocess(f.read(), common, common, defines, depth + 1))
    return "\n".join(out)


MACROS = ("ADDPARAMCOMBO", "ADDPARAMDEF", "ADDPARAMRESOURCE", "ADDPARAM",
          "ADDCND_AT", "ADDACT_AT", "ADDEXP_AT", "ADDCND", "ADDACT", "ADDEXP")
MACRO_RE = re.compile(r"\b(" + "|".join(MACROS) + r")\s*\(")


def parse_plugin(path: str, common: str, ev_defines: dict[str, str], lang) -> dict:
    with open(path, encoding="latin-1") as f:
        raw = f.read()
    defines = dict(ev_defines)
    # Mouse & keyboard builds its key combo at run time from keyTable[] (Main.cpp);
    # reproduce that: names joined with "|" in table order, up to the {-1, ...} end mark.
    table = re.search(r"ControlTableEntry\s+keyTable\s*\[\s*\]\s*=\s*\{(.*?)\};", strip_comments(raw), re.S)
    if table:
        names = []
        for vk, name in re.findall(r"\{\s*([^,{}]+?)\s*,\s*(\"(?:[^\"\\]|\\.)*\")\s*\}", table.group(1)):
            if vk.strip() == "-1":
                break
            names.append(c_string(name))
        defines["keysCombo"] = "\"" + "|".join(names).replace("\"", "\\\"") + "\""
    text = preprocess(raw, path, common, defines)
    ev = Evaluator(defines, lang)
    tables: dict[str, dict[int, dict]] = {"conditions": {}, "actions": {}, "expressions": {}}
    own = {"conditions": OWN_ACE_BASE, "actions": OWN_ACE_BASE, "expressions": OWN_ACE_BASE}
    params: list[dict] = []
    for m in MACRO_RE.finditer(text):
        name = m.group(1)
        start = m.end() - 1
        try:
            args = split_args(text[start + 1:matching(text, start)])
        except ValueError:
            continue
        if name.startswith("ADDPARAM"):
            if name == "ADDPARAMCOMBO" and len(args) >= 3:
                params.append({"type": 9, "name": ev(args[0]), "init": ev(args[2])})
            elif name == "ADDPARAMRESOURCE" and len(args) >= 3:
                params.append({"type": 16, "name": ev(args[0]), "init": ev(args[2])})
            elif name == "ADDPARAMDEF" and len(args) >= 4:
                params.append({"type": ev(args[0]), "name": ev(args[1]), "init": ev(args[3])})
            elif len(args) >= 3:
                params.append({"type": ev(args[0]), "name": ev(args[1]), "init": ""})
            continue
        explicit = name.endswith("_AT")
        if explicit:
            if not args:
                continue
            index = ev(args[0])
            args = args[1:]
        kind = {"CND": "conditions", "ACT": "actions", "EXP": "expressions"}[name[3:6]]
        if kind == "expressions":
            if len(args) < 5:
                continue
            ace = {"name": ev(args[0]), "category": ev(args[1]), "display": ev(args[2]),
                   "script": ev(args[2]), "flags": ev(args[4])}
        else:
            if len(args) < 6:
                continue
            ace = {"name": ev(args[0]), "category": ev(args[1]), "display": ev(args[2]),
                   "script": ev(args[4]), "flags": ev(args[5])}
        ace["params"] = params
        params = []
        if not explicit or not isinstance(index, int) or index >= OWN_ACE_BASE:
            index = own[kind]
            own[kind] += 1
        tables[kind][index] = ace
    return tables


def plugin_dll_name(folder: str) -> Optional[str]:
    for name in os.listdir(folder):
        if name.endswith(".vcproj"):
            with open(os.path.join(folder, name), encoding="latin-1") as f:
                for m in re.finditer(r'OutputFile="[^"]*?([^"\\/]+\.csx)"', f.read()):
                    return m.group(1)
    return None


# --- System object -------------------------------------------------------------
def function_bodies(src: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(r"\bvoid\s+(\w+)\s*\(([^)]*)\)\s*\{", src):
        open_pos = m.end() - 1
        try:
            out[m.group(1)] = src[open_pos + 1:matching(src, open_pos)]
        except ValueError:
            pass
    return out


def split_statements(body: str) -> list[str]:
    stmts, depth, cur, i = [], 0, [], 0
    while i < len(body):
        c = body[i]
        if c in "\"'":
            j = i + 1
            while j < len(body) and body[j] != c:
                j += 2 if body[j] == "\\" else 1
            cur.append(body[i:j + 1])
            i = j + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if (c == ";" and depth == 0) or (c in "{}" and depth == 0):
            if "".join(cur).strip():
                stmts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
        i += 1
    if "".join(cur).strip():
        stmts.append("".join(cur).strip())
    return stmts


def run_system_ace(body: str, functions: dict[str, str], ev: Evaluator, ace: dict, depth: int = 0) -> None:
    vars_: dict[str, dict] = {}
    for st in split_statements(body):
        st = re.sub(r"^(case\s+\d+\s*:|default\s*:)\s*", "", st).strip()
        if not st or st == "break":
            continue
        m = re.fullmatch(r"(SO_\w+)\s*\(\s*\*?\s*Out\s*\)", st)
        if m and m.group(1) in functions and depth < 5:
            run_system_ace(functions[m.group(1)], functions, ev, ace, depth + 1)
            continue
        m = re.fullmatch(r"PARAM2\s+(\w+)", st)
        if m:
            vars_[m.group(1)] = {"type": 1, "name": "", "init": ""}
            continue
        m = re.fullmatch(r"Out\s*(?:\.|->)\s*params\s*\.\s*push_back\s*\(\s*(\w+)\s*\)", st)
        if m:
            ace["params"].append(dict(vars_.get(m.group(1), {"type": 1, "name": "", "init": ""})))
            continue
        m = re.fullmatch(r"(\w+)\s*(?:\.|->)\s*(\w+)\s*=\s*(.+)", st, re.S)
        if not m:
            continue
        target, field, value = m.group(1), m.group(2), ev(m.group(3))
        if target == "Out":
            key = {"aceDisplayText": "display", "scriptName": "script", "aceListName": "name",
                   "aceCategory": "category", "retrn": "flags"}.get(field)
            if key:
                ace[key] = value
        elif target in vars_:
            key = {"type": "type", "name": "name", "initStr": "init"}.get(field)
            if key:
                vars_[target][key] = value


def parse_system(ide: str, lang, defines: dict[str, str]) -> dict:
    path = os.path.join(ide, "Structure", "SystemObject.cpp")
    with open(path, encoding="latin-1") as f:
        src = strip_comments(f.read())
    functions = function_bodies(src)
    ev = Evaluator(defines, lang)
    result = {}
    for kind, fname, category in (("actions", "GetSystemAction", "default"),
                                  ("conditions", "GetSystemCondition", "System")):
        body = functions[fname]
        sw = body.index("switch")
        open_pos = body.index("{", sw)
        switch = body[open_pos + 1:matching(body, open_pos)]
        labels = list(re.finditer(r"\bcase\s+(\d+)\s*:", switch))
        table = {}
        for k, lab in enumerate(labels):
            end = labels[k + 1].start() if k + 1 < len(labels) else len(switch)
            ace = {"name": "", "category": category, "display": "", "script": "", "flags": 0, "params": []}
            run_system_ace(switch[lab.end():end], functions, ev, ace)
            if ace["display"] or ace["name"]:
                table[int(lab.group(1))] = ace
        result[kind] = table
    return result


def main() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else os.path.join("vendor", "construct-src")
    out_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join("ccdec", "data", "aces.json")
    ide = os.path.join(root, "IDE")
    lang = load_language(ide)
    sys_defines = collect_defines([
        os.path.join(ide, "Structure", "Languages", "Languages.h"),
        os.path.join(ide, "Editors", "ChronoEventEditor", "ChronoEventEditor.h"),
        os.path.join(ide, "Structure.h"),
    ])
    common = os.path.join(root, "Common", "CommonAceTable.hpp")
    plugin_defines = collect_defines([os.path.join(root, "Common", "internal.hpp")])
    plugins = []
    for group in ("Plugins", "Behaviors"):
        for name in sorted(os.listdir(os.path.join(root, group))):
            folder = os.path.join(root, group, name)
            if not os.path.isdir(folder):
                continue
            src = None
            for fn in sorted(os.listdir(folder)):
                if fn.lower().endswith((".cpp", ".h", ".hpp")):
                    with open(os.path.join(folder, fn), encoding="latin-1") as f:
                        if "DefineACES" in f.read():
                            src = os.path.join(folder, fn)
                            break
            if not src:
                continue
            headers = [os.path.join(folder, fn) for fn in sorted(os.listdir(folder))
                       if fn.lower().endswith((".h", ".hpp"))]
            tables = parse_plugin(src, common, {**plugin_defines, **collect_defines(headers)}, lang)
            plugins.append({"source": f"{group}/{name}", "dll": plugin_dll_name(folder),
                            "behavior": group == "Behaviors",
                            **{k: {str(i): a for i, a in sorted(v.items())} for k, v in tables.items()}})
    system = parse_system(ide, lang, sys_defines)
    data = {
        "generated_by": "tools/gen_aces.py",
        "source": "github.com/aolko/construct master (SVN r321, Classic r2)",
        "own_ace_base": OWN_ACE_BASE,
        "system": {k: {str(i): a for i, a in sorted(v.items())} for k, v in system.items()},
        "plugins": plugins,
    }
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, sort_keys=False)
    print(f"system: {len(system['conditions'])} conditions, {len(system['actions'])} actions")
    for p in plugins:
        print(f"{p['source']:<32} {p['dll'] or '?':<22} cnd {len(p['conditions']):>3} "
              f"act {len(p['actions']):>3} exp {len(p['expressions']):>3}")
    print(f"wrote {out_path}")


if __name__ == "__main__":
    main()
