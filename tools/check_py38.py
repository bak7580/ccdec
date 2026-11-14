"""Static check that the code still runs on Python 3.8 (the newest Python for Windows 7).

The development machine has a much newer Python, so mistakes that only an old interpreter
would reject are caught here instead:

* grammar newer than 3.8 (``ast.parse(feature_version=(3, 8))``);
* f-strings that reuse the outer quote or contain a backslash inside ``{...}`` (allowed only
  since 3.12, and ``ast`` does not report them);
* ``list[int]``-style generics or ``X | Y`` unions evaluated at run time (3.9 / 3.10);
* standard-library calls added after 3.8.

Usage:  python tools/check_py38.py   (exit code 1 if anything is found)
"""

from __future__ import annotations

import ast
import glob
import io
import os
import re
import sys
import tokenize
import warnings

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GENERICS = {"list", "dict", "tuple", "set", "frozenset", "type"}
NEWER_APIS = {
    r"\.removeprefix\(": "str.removeprefix (3.9)", r"\.removesuffix\(": "str.removesuffix (3.9)",
    r"functools\.cache\b|from functools import .*\bcache\b": "functools.cache (3.9)",
    r"\bzip\([^)]*strict=": "zip(strict=) (3.10)", r"getencoding\(": "locale.getencoding (3.11)",
    r"randbytes\(": "random.randbytes (3.9)", r"file_digest\(": "hashlib.file_digest (3.11)",
    r"slots=True|kw_only=": "dataclass slots/kw_only (3.10)", r"\bpairwise\(": "itertools.pairwise (3.10)",
    r"contextlib\.chdir": "contextlib.chdir (3.11)", r"\bStrEnum\b": "enum.StrEnum (3.11)",
    r"datetime\.UTC\b": "datetime.UTC (3.11)", r"\btomllib\b": "tomllib (3.11)",
    r"\bzoneinfo\b": "zoneinfo (3.9)", r"\bgraphlib\b": "graphlib (3.9)",
    r"from typing import .*\b(TypeAlias|ParamSpec|Self|Concatenate|TypeGuard)\b": "typing (3.10+)",
    r"\bmath\.lcm\(": "math.lcm (3.9)", r"\.bit_count\(": "int.bit_count (3.10)",
    r"\bmatch\s+\S.*:\s*$": "match statement (3.10)",
}


class RuntimeGenerics(ast.NodeVisitor):
    """Find ``list[...]`` / ``A | B`` outside annotations (annotations are strings under the
    ``from __future__ import annotations`` that every module uses)."""

    def __init__(self, lazy: bool) -> None:
        self.lazy = lazy
        self.found: list[tuple[int, str]] = []
        self.in_annotation = 0

    def _annotation(self, node) -> None:
        if node is not None:
            self.in_annotation += 1
            self.visit(node)
            self.in_annotation -= 1

    def visit_arg(self, node: ast.arg) -> None:
        self._annotation(node.annotation)

    def visit_FunctionDef(self, node) -> None:
        for d in node.decorator_list:
            self.visit(d)
        self.visit(node.args)
        self._annotation(node.returns)
        for s in node.body:
            self.visit(s)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._annotation(node.annotation)
        if node.value is not None:
            self.visit(node.value)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.value, ast.Name) and node.value.id in GENERICS and not (self.lazy and self.in_annotation):
            self.found.append((node.lineno, f"{node.value.id}[...] evaluated at run time (3.9+)"))
        self.generic_visit(node)

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.BitOr) and not (self.lazy and self.in_annotation):
            sides = [node.left, node.right]
            if any(isinstance(s, ast.Constant) and s.value is None for s in sides) or \
                    any(isinstance(s, ast.Name) and s.id in {"int", "str", "float", "bytes", "bool"} for s in sides):
                self.found.append((node.lineno, "X | Y type union evaluated at run time (3.10+)"))
        self.generic_visit(node)


def fstring_problems(src: str) -> list[tuple[int, str]]:
    """f-string features newer than 3.8, found with the 3.12+ f-string tokens."""
    if not hasattr(tokenize, "FSTRING_START"):
        return []
    out = []
    stack: list[tuple[str, int]] = []           # (quote of the open f-string, brace depth)
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.FSTRING_START:
            if stack:
                outer = stack[-1][0]
                if tok.string.lstrip("rRfFbB")[:1] == outer:
                    out.append((tok.start[0], "nested f-string reuses the outer quote (3.12+)"))
            stack.append((tok.string.lstrip("rRfFbB")[:1], 0))
        elif tok.type == tokenize.FSTRING_END:
            if stack:
                stack.pop()
        elif stack and tok.type != tokenize.FSTRING_MIDDLE:
            if tok.type == tokenize.STRING and tok.string.lstrip("rRbBuU")[:1] == stack[-1][0]:
                out.append((tok.start[0], "string inside an f-string reuses the outer quote (3.12+)"))
            if "\\" in tok.string:
                out.append((tok.start[0], "backslash inside an f-string expression (3.12+)"))
            if tok.type == tokenize.COMMENT:
                out.append((tok.start[0], "comment inside an f-string expression (3.12+)"))
    return out


def check_file(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        src = f.read()
    rel = os.path.relpath(path, ROOT)
    problems = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(src, path, feature_version=(3, 8))
    except SyntaxError as e:
        return [f"{rel}:{e.lineno}: syntax not available in 3.8: {e.msg}"]
    lazy = "from __future__ import annotations" in src
    v = RuntimeGenerics(lazy)
    v.visit(tree)
    for line, msg in v.found + fstring_problems(src):
        problems.append(f"{rel}:{line}: {msg}")
    for n, text in enumerate(src.splitlines(), 1):
        if text.lstrip().startswith("#"):
            continue
        for pattern, what in NEWER_APIS.items():
            if re.search(pattern, text) and "check_py38" not in rel:
                problems.append(f"{rel}:{n}: {what}")
    return problems


def main() -> int:
    files = sorted(glob.glob(os.path.join(ROOT, "ccdec", "**", "*.py"), recursive=True)
                   + glob.glob(os.path.join(ROOT, "tests", "*.py"))
                   + glob.glob(os.path.join(ROOT, "tools", "*.py"))
                   + [os.path.join(ROOT, "ccdec-gui.pyw")])
    problems = [p for f in files for p in check_file(f)]
    for p in problems:
        print(p)
    print(f"{len(files)} files checked, {len(problems)} problems")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
