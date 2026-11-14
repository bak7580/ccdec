"""Build the archives for a release.

    python tools/build_zip.py            dist/ccdec-<version>.zip         - for users
    python tools/build_zip.py --source   dist/ccdec-<version>-source.zip  - the repository
                                                                          contents, for GitHub

The user archive contains only what is needed to run the program.  The source archive is
what belongs in the public repository: no Construct sources (vendor/), no private test
samples, outputs or caches - the same rules as .gitignore.  Every name is plain ASCII,
because the zip extractor built into Windows 7 does not understand UTF-8 file names.
"""

from __future__ import annotations

import os
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ccdec import __version__  # noqa: E402

USER_FILES = ["ccdec-gui.pyw", "ccdec-gui.bat", "README.md", "README.ru.md", "FORMAT.md", "LICENSE", "NOTICE",
              "tools/learn_aces.py"]
SOURCE_FILES = USER_FILES + [".gitignore", "docs/window.png", "docs/window.ru.png",
                             "docs/window-dark.png", "docs/window-dark.ru.png",
                             "tests/samples/mesh_editor/Mesh Editor.exe", "tests/samples/mesh_editor/Mesh Editor.cap"]
SOURCE_DIRS = {"tools": (".py",), "tests": (".py",)}


def walk(folder: str, exts: tuple) -> list[str]:
    out = []
    base = os.path.join(ROOT, folder)
    for here, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", "samples"))
        for name in sorted(names):
            if name.endswith(exts):
                out.append(os.path.relpath(os.path.join(here, name), ROOT).replace(os.sep, "/"))
    return out


def collect(source: bool) -> list[str]:
    files = list(SOURCE_FILES if source else USER_FILES) + walk("ccdec", (".py", ".json"))
    if source:
        for folder, exts in SOURCE_DIRS.items():
            files += walk(folder, exts)
    return sorted(set(files), key=files.index)


def main() -> int:
    source = "--source" in sys.argv[1:]
    top = f"ccdec-{__version__}"
    name = top + ("-source" if source else "") + ".zip"
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    target = os.path.join(ROOT, "dist", name)
    files = collect(source)
    for rel in files:
        if not rel.isascii():
            raise SystemExit(f"non-ASCII name would be garbled by the Windows 7 extractor: {rel}")
        if not os.path.isfile(os.path.join(ROOT, rel)):
            raise SystemExit(f"missing: {rel}")
        if rel.startswith(("vendor/", "tests/samples/private/", "out/", "dist/")):
            raise SystemExit(f"must not be published: {rel}")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in files:
            z.write(os.path.join(ROOT, rel), f"{top}/{rel}")
    print(f"{target}: {len(files)} files, {os.path.getsize(target) // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
