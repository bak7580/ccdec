"""Where the tests find their sample games.

* ``samples/mesh_editor`` - the Mesh Editor tool from the Construct Classic source tree
  (2009 runtime, with its .cap).  Public; most tests use it.
* ``samples/private/`` (git-ignored) - games that cannot be published.  The maintainer's
  set is: ``a.exe`` + ``a.cap`` (2011 runtime, Cyrillic strings; the .cap was saved after
  the export) and ``b.exe`` (2011 runtime, 75 layouts, 41 sound files, no .cap).  Tests that
  need them are skipped when the files are absent; their exact numbers are specific to
  those games.  Paths can be overridden with CCDEC_SAMPLE_A_EXE / _A_CAP / _B_EXE.
"""

from __future__ import annotations

import os

HERE = os.path.dirname(os.path.abspath(__file__))
PRIVATE = os.path.join(HERE, "samples", "private")

MESH = os.path.join(HERE, "samples", "mesh_editor", "Mesh Editor.exe")
MESH_CAP = os.path.join(HERE, "samples", "mesh_editor", "Mesh Editor.cap")
GAME = os.environ.get("CCDEC_SAMPLE_A_EXE") or os.path.join(PRIVATE, "a.exe")
GAME_CAP = os.environ.get("CCDEC_SAMPLE_A_CAP") or os.path.join(PRIVATE, "a.cap")
BIG = os.environ.get("CCDEC_SAMPLE_B_EXE") or os.path.join(PRIVATE, "b.exe")


def has_cyrillic(text: str) -> bool:
    return any("Ѐ" <= ch <= "ӿ" for ch in text)
