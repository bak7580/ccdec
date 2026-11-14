"""Command line entry point: ``python -m ccdec game.exe -o out/ --level 1|2 --report``.

Level 1 writes the assets, level 2 adds project.json, level 3 adds a rebuilt .cap.
``--compare-cap`` / ``--compare-exe`` check a rebuilt project against a reference.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import re
import sys
from typing import Optional, Sequence

from . import __version__
from .cap_format import CapError, read_cap, write_cap
from .cap_writer import build_cap
from .compare import compare_trees
from .container import Container, ContainerError, Resource, load_container
from .export_assets import export_assets
from .export_json import export_json
from .i18n import set_language
from .index import build_index
from .model import Event, Project
from .reader import PROFILES, parse_project
from .report import build_report

log = logging.getLogger("ccdec")


def _safe_name(name: object) -> str:
    """Turn an untrusted resource name into a single harmless path component."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", str(name)).lstrip(".")
    return cleaned[:100] or "_"


def _stamp(ts: Optional[int]) -> str:
    if not ts:
        return "?"
    try:
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "?"


def format_summary(c: Container) -> str:
    lines = [
        f"File:            {c.path} ({c.size} bytes)",
        f"Runtime build:   PE timestamp {c.pe_timestamp} ({_stamp(c.pe_timestamp)})"
        f"{', 64-bit' if c.is_pe32_plus else ''}",
        f"Kind:            {'installer -> ' if c.from_installer else ''}"
        f"{'preview build' if c.is_preview else 'exported exe' if c.is_construct_export else 'NOT a Construct Classic export'}"
        f"{', Python enabled' if c.has_python else ''}",
        "Blocks:",
    ]
    for name, r in sorted(c.blocks.items(), key=lambda kv: kv[1].name if isinstance(kv[1].name, int) else 0):
        lines.append(f"  {name + '/' + str(r.name):<16} {r.size:>10} bytes  sha1 {r.sha1}")
    lines.append(f"Plugins:         {len(c.plugins)} embedded (declared: {c.declared_plugin_count})")
    for p in c.plugins:
        lines.append(f"  [{p.index:>2}] {p.file_name or '<unknown name>':<24} {p.resource.size:>9} bytes  "
                     f"built {_stamp(p.timestamp)}  sha1 {p.sha1}")
    lines.append(f"Files:           {len(c.files)}")
    for r in c.files:
        lines.append(f"  FILES/{r.name:<6} {r.size:>10} bytes  head {r.data[:8].hex(' ')}")
    lines.append(f"Dependencies:    {len(c.dependencies)}")
    for r in c.dependencies:
        lines.append(f"  {r.name!s:<24} {r.size:>10} bytes")
    if c.warnings:
        lines.append("Warnings:")
        lines += [f"  - {w}" for w in c.warnings]
    return "\n".join(lines)


def format_project(p: Project) -> str:
    """Phase 2 summary: what the block parsers recovered."""
    described = {x.name: x.description for x in PROFILES}
    lines = [f"Parsed blocks (strings decoded as {p.encoding}):"]
    for name, profile in p.profiles.items():
        note = f"layout profile {profile!r} - {described[profile]}" if profile in described else "ok"
        lines.append(f"  {name:<11} {note}")
    for name, data in p.raw.items():
        lines.append(f"  {name:<11} NOT PARSED, kept raw ({len(data)} bytes)")
    if p.app:
        a = p.app
        lines.append(f"Application:     {a.name!r}, window {a.window_width}x{a.window_height}, "
                     f"{len(a.global_variables)} global variables, {len(a.controls)} controls")
        lines.append(f"Original .cap:   {a.cap_path}")
    if p.level:
        lv = p.level
        lines.append(f"Object types:    {len(lv.object_types)}, behaviors {len(lv.behaviors)}, "
                     f"traits {len(lv.traits)}, families {len(lv.families)}, containers {len(lv.containers)}")
        instances = sum(len(y.instances) for l in lv.layouts for y in l.layers)
        lines.append(f"Layouts:         {len(lv.layouts)} with {sum(len(l.layers) for l in lv.layouts)} layers, "
                     f"{instances} instances")
    if p.events:
        def count(lines_: list) -> tuple[int, int, int]:
            e = c = a = 0
            for x in lines_:
                if isinstance(x, Event):
                    e, c, a = e + 1, c + len(x.conditions), a + len(x.actions)
                se, sc, sa = count(x.children)
                e, c, a = e + se, c + sc, a + sa
            return e, c, a
        e, c, a = map(sum, zip(*(count(l) for l in p.events.layout_events))) if p.events.layout_events else (0, 0, 0)
        lines.append(f"Events:          {len(p.events.sheet_names)} sheets, {e} events, {c} conditions, {a} actions")
    if p.images is not None:
        lines.append(f"Images:          {len(p.images)} ({sum(len(i.data) for i in p.images)} bytes)")
    if p.hlsl:
        lines.append(f"Effects:         {len(p.hlsl.shaders)} shaders, {len(p.hlsl.transitions)} transitions")
    if p.issues:
        lines.append("Issues:")
        lines += [f"  - {i}" for i in p.issues]
    return "\n".join(lines)


def dump_raw(c: Container, out_dir: str) -> list[str]:
    """Write every project resource, unmodified, under ``out_dir/raw``."""
    raw = os.path.join(out_dir, "raw")
    os.makedirs(raw, exist_ok=True)
    written: list[str] = []

    def put(name: str, r: Resource) -> None:
        path = os.path.join(raw, name)
        with open(path, "wb") as f:
            f.write(r.data)
        written.append(path)

    for name, r in c.blocks.items():
        put(f"{name}_{r.name}.bin", r)
    for p in c.plugins:
        put(f"plugin_{p.index:03d}_{_safe_name(p.file_name or 'unknown.csx')}", p.resource)
    for r in c.files:
        put(f"file_{r.name:04d}.bin", r)
    for r in c.dependencies:
        put(f"dependency_{_safe_name(r.name)}", r)
    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="ccdec", description=__doc__.splitlines()[0])
    ap.add_argument("exe", help="exported Construct Classic executable")
    ap.add_argument("-o", "--out", help="output directory")
    ap.add_argument("--raw", action="store_true", help="dump the raw data blocks to OUT/raw")
    ap.add_argument("--level", type=int, choices=(1, 2, 3),
                    help="1 = assets, 2 = assets + project.json, 3 = also rebuild a .cap project file")
    ap.add_argument("--compare-cap", metavar="CAP",
                    help="compare the project rebuilt from EXE with an existing .cap (exit code 3 if they differ)")
    ap.add_argument("--compare-exe", metavar="EXE2",
                    help="compare with another exe, e.g. one re-exported from the rebuilt .cap")
    ap.add_argument("--report", action="store_true", help="print the recovery report (and save OUT/report.md)")
    ap.add_argument("--masks", action="store_true", default=None,
                    help="classify collision masks and write custom ones (pure Python, slow on big images)")
    ap.add_argument("--no-tokens", action="store_true", help="omit raw expression tokens from project.json")
    ap.add_argument("--encoding", default=None,
                    help="ANSI code page of the project strings (default: guess cp1251/cp1252)")
    ap.add_argument("--lang", choices=("ru", "en"), help="language of report.md (default: from the system)")
    ap.add_argument("-v", "--verbose", action="count", default=0)
    ap.add_argument("--version", action="version", version=f"ccdec {__version__}")
    args = ap.parse_args(argv)
    if args.lang:
        set_language(args.lang)

    logging.basicConfig(level=(logging.WARNING, logging.INFO, logging.DEBUG)[min(args.verbose, 2)],
                        format="%(levelname)s %(name)s: %(message)s")
    # Strings may hold bytes the code page cannot decode (kept as surrogates): never crash on print.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="backslashreplace")
    try:
        c = load_container(args.exe)
    except (ContainerError, OSError) as e:
        print(f"ccdec: {args.exe}: {e}", file=sys.stderr)
        return 1
    try:
        return _run(ap, args, c)
    except (CapError, ContainerError, OSError) as e:
        print(f"ccdec: {e}", file=sys.stderr)
        return 1


def _run(ap: argparse.ArgumentParser, args: argparse.Namespace, c: Container) -> int:

    print(format_summary(c))
    if not c.is_construct_export:
        return 1
    project = parse_project(c, args.encoding)
    print(format_project(project))
    if args.raw:
        if not args.out:
            ap.error("--raw requires -o OUT")
        written = dump_raw(c, args.out)
        print(f"Wrote {len(written)} raw files to {os.path.join(args.out, 'raw')}")
    if not args.level and not args.report and not args.compare_cap and not args.compare_exe:
        return 0
    if args.level and not args.out:
        ap.error("--level requires -o OUT")

    level = args.level or 0
    ix = build_index(c, project)
    assets = dumper = None
    masks = args.masks if args.masks is not None else False
    if level >= 1:
        os.makedirs(args.out, exist_ok=True)
        assets = export_assets(ix, args.out, analyse_masks=masks)
        print(f"Level 1: {len(assets['images'])} images, {len(assets['files'])} files, "
              f"{len(assets['effects'])} effects written to {args.out}")
    if level >= 2:
        _, dumper = export_json(ix, args.out, include_tokens=not args.no_tokens)
        print(f"Level 2: {os.path.join(args.out, 'project.json')}")
    rebuilt = None
    if level >= 3 or args.compare_cap or args.compare_exe:
        try:
            rebuilt = build_cap(ix, os.path.join(args.out, "files") if args.out else None)
        except ValueError as e:
            print(f"ccdec: cannot rebuild a .cap: {e}", file=sys.stderr)
            return 1
    if level >= 3 and rebuilt is not None:
        cap_path = os.path.join(args.out, os.path.splitext(os.path.basename(args.exe))[0] + ".cap")
        data = write_cap(rebuilt.app, rebuilt.encoding)
        # Self-check: the file must parse with the same code that reads real .cap files
        # and serialize back to the identical bytes.
        if write_cap(read_cap(data, rebuilt.encoding), rebuilt.encoding) != data:
            print("ccdec: internal error: the rebuilt .cap does not round-trip", file=sys.stderr)
            return 1
        with open(cap_path, "wb") as f:
            f.write(data)
        print(f"Level 3: {cap_path} ({len(data)} bytes)")
    exit_code = 0
    if rebuilt is not None and (args.compare_cap or args.compare_exe):
        if args.compare_cap:
            with open(args.compare_cap, "rb") as f:
                other = read_cap(f.read(), rebuilt.encoding)
            label = os.path.basename(args.compare_cap)
            heads = frozenset(rebuilt.trigger_heads)
        else:
            c2 = load_container(args.compare_exe)
            r2 = build_cap(build_index(c2, parse_project(c2, args.encoding)))
            other, label = r2.app, os.path.basename(args.compare_exe)
            heads = frozenset(rebuilt.trigger_heads | r2.trigger_heads)
        # Compare through the file format so both sides are in the same (as-read) form.
        mine = read_cap(write_cap(rebuilt.app, rebuilt.encoding), rebuilt.encoding)
        other = read_cap(write_cap(other, rebuilt.encoding), rebuilt.encoding)
        diffs = compare_trees(mine, other, os.path.basename(args.exe), label, heads)
        print(f"\nComparison with {label}: " + ("equivalent" if not diffs else f"{len(diffs)} difference lines"))
        for line in diffs:
            print("  " + line)
        exit_code = 3 if diffs else 0
    if args.report:
        if dumper is None:
            from .export_json import Dumper
            dumper = Dumper(ix)
            dumper.project()            # fills the unknown-ACE / property statistics
        text = build_report(ix, assets, dumper, level, rebuilt)
        print()
        print(text)
        if args.out:
            os.makedirs(args.out, exist_ok=True)
            with open(os.path.join(args.out, "report.md"), "w", encoding="utf-8", errors="backslashreplace") as f:
                f.write(text)
    return exit_code
