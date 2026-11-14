"""Learn event parameter types from existing .cap files.

For plugins whose source is not in vendor/construct-src (Button, File, MOD, third-party
plugins...) the exe only gives script names and parameter counts.  A .cap stores the
editor's parameter type with every parameter, so any project that *uses* a condition
or action teaches us its parameter types.  This script collects them into
``ccdec/data/aces_learned.json``; ``ccdec.aces`` consults that file for ACEs that have
no source metadata.

Usage:  python tools/learn_aces.py a.cap [b.cap ...] [--encoding cp1251]

Entries are keyed by the plugin's display name (the name stored in the .cap and in
string resource 1 of the plugin DLL), then by kind and ACE id.
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ccdec.cap_format import read_cap  # noqa: E402

OUT = os.path.join(ROOT, "ccdec", "data", "aces_learned.json")


def learn(app: dict, db: dict, origin: str) -> int:
    types = {t["key"]: t["type"] for t in app["object_types"]}
    added = 0

    def visit(events: list) -> None:
        nonlocal added
        for e in events:
            for kind in ("conditions", "actions"):
                for a in e.get(kind, []):
                    if a["oid"] == -1:
                        continue
                    plugin = a.get("plugin") or ""
                    if a["movement_id"] >= 0:
                        t = types.get(a["oid"])
                        beh = next((b for b in (t["behaviors"] if t else []) if b["id"] == a["movement_id"]), None)
                        if beh is None:
                            continue
                        plugin = beh["plugin"]
                    elif a["movement_id"] < -1:
                        continue                      # effect actions are System actions at run time
                    if not plugin:
                        continue
                    entry = {"params": [p["type"] for p in a["params"]],
                             "reserved": [p["reserved"] if p["type"] == 9 else "" for p in a["params"]]}
                    slot = db.setdefault(plugin, {}).setdefault(kind, {})
                    key = str(a["id"])
                    if key not in slot:
                        slot[key] = {"params": entry["params"], "seen_in": origin}
                        added += 1
                    elif slot[key]["params"] != entry["params"]:
                        print(f"  conflict for {plugin} {kind} {key}: {slot[key]['params']} vs {entry['params']}"
                              f" ({origin}); keeping the first")
            visit(e.get("events", []))

    for sheet in app["event_sheets"]:
        visit(sheet["events"])
    return added


def main() -> None:
    args = sys.argv[1:]
    encoding = "cp1252"
    if "--encoding" in args:
        k = args.index("--encoding")
        encoding = args[k + 1]
        del args[k:k + 2]
    db: dict = {}
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            db = json.load(f)
    for path in args:
        with open(path, "rb") as f:
            app = read_cap(f.read(), encoding)
        n = learn(app, db, os.path.basename(os.path.dirname(os.path.abspath(path))) + "/" + os.path.basename(path)
                  if False else os.path.basename(path))
        print(f"{path}: {n} new entries")
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"wrote {OUT}: " + ", ".join(f"{k} ({sum(len(v) for v in d.values())})" for k, d in sorted(db.items())))


if __name__ == "__main__":
    main()
