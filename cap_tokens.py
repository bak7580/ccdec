"""The editor's expression tokenizer, as the .cap stores its output.

A .cap keeps every event parameter as the token list produced by
``CExpression::ParseUserString(text, editorMode = true)`` followed by
``ProcessEdittimeExpressionInformation`` (IDE/Editors/Expressions/CExpression.cpp).
To write a .cap we have to produce the same lists from expression text, so this
module is a line-by-line port of those two functions.  It is checked against
ground truth: re-tokenising the text of every parameter in the reference .cap
files must reproduce their stored tokens exactly (tests/test_cap.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import model as m

T = {name: i for i, name in enumerate(m.TOKEN_NAMES)}
T_WHITESPACE, T_COLOR = 52, 53                      # editor-only token types
TT_NORMAL, TT_OBJECTNAME, TT_FAMILYNAME, TT_MOVEMENTNAME = 0, 1, 2, 3

_KEYWORDS = {
    "sin": "SIN", "cos": "COS", "tan": "TAN", "sqrt": "SQRT", "str": "FUNC_STR", "int": "FUNC_INT",
    "float": "FUNC_FLOAT", "num": "FUNC_FLOAT", "and": "AND", "or": "OR", "asin": "ASIN",
    "acos": "ACOS", "atan": "ATAN", "abs": "ABS", "exp": "EXP", "log": "LN", "ln": "LN",
    "log10": "LOG10", "floor": "FUNC_INT", "ceil": "CEIL", "round": "ROUND", "random": "RANDOM",
    "len": "LEN", "at": "AT",
}
_SINGLE = {"+": "ADD", "*": "MULTIPLY", "/": "DIVIDE", "^": "POWER", "%": "MOD", "(": "LEFTBRACKET",
           ")": "RIGHTBRACKET", "{": "LEFTCURLY", "}": "RIGHTCURLY", "@": "AT", ",": "COMMA", ".": "DOT",
           "=": "EQUAL", "&": "AND", "|": "OR", "?": "CONDITIONAL", ":": "COLON"}
_ANY_VALUE = {T["IDENTIFIER"], T["INTEGER"], T["FLOAT"], T["STRINGLITERAL"], T["VARIABLENAME"],
              T["LEFTBRACKET"], T["LEFTCURLY"]}


class TokenizeError(ValueError):
    pass


@dataclass
class EditorToken:
    type: int
    text: str
    length: int
    owner: int = -1
    id: int = 0
    subtype: int = TT_NORMAL

    def as_cap(self) -> dict:
        return {"type": self.type, "length": self.length & 0xFFFF, "owner": self.owner, "text": self.text,
                "id": self.id, "subtype": self.subtype}


@dataclass
class Names:
    """What the editor's name lookups need (all keys lower-case)."""
    object_types: dict[str, int] = field(default_factory=dict)              # name -> edit id
    behaviors: dict[tuple[int, str], int] = field(default_factory=dict)     # (type id, name) -> unique id
    families: dict[str, int] = field(default_factory=dict)                  # name -> edit id
    private_variables: dict[tuple[int, str], int] = field(default_factory=dict)  # (type id, name) -> var id
    global_variables: dict[str, int] = field(default_factory=dict)          # name -> var id


def _is_alpha(c: str) -> bool:
    return ("a" <= c <= "z") or ("A" <= c <= "Z") or c == "_"


def _is_digit(c: str) -> bool:      # CExpression::IsChDigit also accepts '.'
    return ("0" <= c <= "9") or c == "."


def tokenize(text: str, caller_name: Optional[str] = None) -> list[EditorToken]:
    """CExpression::ParseUserString with editorMode = true."""
    toks: list[EditorToken] = []
    n = len(text)

    def at(k: int) -> str:
        return text[k] if k < n else "\0"

    i = 0
    while i < n:
        c = text[i]
        if c == "-":
            negative = not toks or ((toks[-1].type not in _ANY_VALUE or toks[-1].type == T["LEFTBRACKET"])
                                    and toks[-1].type != T["RIGHTBRACKET"])
            if negative:                                    # ConsumeDigits
                j = i + 1
                while _is_digit(at(j)):
                    j += 1
                buf = text[i:j]
                toks.append(EditorToken(T["FLOAT"] if "." in buf else T["INTEGER"], buf, len(buf)))
                i = j
                continue
            toks.append(EditorToken(T["SUBTRACT"], "-", 1))
        elif c in _SINGLE:
            toks.append(EditorToken(T[_SINGLE[c]], c, 1))
        elif c in "\"'":                                    # AppendString
            single = c == "'"
            out = []
            j = i + 1
            while True:
                ch = at(j)
                if ch == '"':
                    if not single and at(j + 1) == '"':
                        out.append('""')                    # editor mode keeps the doubled quote
                        j += 2
                        continue
                    break
                if single and ch == "'":
                    break
                if j >= n:
                    raise TokenizeError("unclosed string quote")
                out.append(ch)
                j += 1
            s = "".join(out)
            if single:
                toks.append(EditorToken(T["VARIABLENAME"], s, len(s) + 2))
            else:
                s = s.replace("$n", "\n").replace("$q", '"').replace("$t", "\t")
                toks.append(EditorToken(T["STRINGLITERAL"], s, len(s) + 2))
            i = j + 1
            continue
        elif c == "<":
            if at(i + 1) == "=":
                toks.append(EditorToken(T["LESSEQUAL"], "<=", 2))
                i += 1
            elif at(i + 1) == ">":
                toks.append(EditorToken(T["NOTEQUAL"], "<>", 2))
                i += 1
            else:
                toks.append(EditorToken(T["LESS"], "<", 1))
        elif c == "!":
            if at(i + 1) != "=":
                raise TokenizeError("syntax error: '!'")
            toks.append(EditorToken(T["NOTEQUAL"], "!=", 2))
            i += 1
        elif c == ">":
            if at(i + 1) == "=":
                toks.append(EditorToken(T["GREATEREQUAL"], ">=", 2))
                i += 1
            else:
                toks.append(EditorToken(T["GREATER"], ">", 1))
        elif _is_alpha(c) or "0" <= c <= "9":               # ConsumeAppendIdentifier
            j, alpha = i, False
            while True:
                ch = at(j)
                if ch == ".":
                    break
                if _is_alpha(ch):
                    alpha = True
                elif not _is_digit(ch) and ch not in "[]":
                    break
                j += 1
            if alpha:
                ident = text[i:j]
                tok = EditorToken(T["IDENTIFIER"], ident, len(ident))
                low = ident.lower()
                if low in _KEYWORDS:
                    tok.type = T[_KEYWORDS[low]]
                elif (low == "self" or low.startswith("self[")) and caller_name:
                    tok.text = caller_name + low[4:]
                toks.append(tok)
            else:
                is_float = at(j) == "."
                if is_float:
                    j += 1
                while "0" <= at(j) <= "9":
                    j += 1
                toks.append(EditorToken(T["FLOAT"] if is_float else T["INTEGER"], text[i:j], j - i))
            i = j
            continue
        elif c in " \t\n\r":
            j = i
            while j < n and text[j] in " \t\n\r":
                j += 1
            toks.append(EditorToken(T_WHITESPACE, text[i:j], 1))    # length 0, then "last token length++"
            i = j
            continue
        else:
            raise TokenizeError(f"unknown character {c!r}")
        i += 1
    return toks


def resolve(toks: list[EditorToken], names: Names, oid: int) -> None:
    """ProcessEdittimeExpressionInformation: tag object/family names and variable owners."""
    last_owner = -1
    for k, t in enumerate(toks):
        if t.type == T["IDENTIFIER"]:
            if k == 0 or toks[k - 1].type != T["DOT"]:
                obj_name, mov_name = t.text, None
                b = t.text.find("[")
                if b > -1:
                    obj_name, mov_name = t.text[:b], t.text[b + 1:-1]
                type_id = names.object_types.get(obj_name.lower())
                if type_id is not None:
                    t.owner = last_owner = type_id
                    if mov_name is not None:
                        t.subtype = TT_MOVEMENTNAME
                        t.id = names.behaviors.get((type_id, mov_name.lower()), 0)
                    else:
                        t.subtype = TT_OBJECTNAME
                elif t.text.lower() in names.families:
                    t.subtype = TT_FAMILYNAME
                    t.owner = last_owner = names.families[t.text.lower()]
        elif t.type == T["VARIABLENAME"]:
            standalone = True
            if k >= 2:
                p1, p2 = toks[k - 1], toks[k - 2]
                if p1.type == T["LEFTBRACKET"] and p2.type == T["IDENTIFIER"] \
                        and p2.subtype in (TT_OBJECTNAME, TT_FAMILYNAME):
                    standalone = False
                if p1.type == T["LEFTBRACKET"] and p2.type == T["IDENTIFIER"] and p2.text.lower() == "global":
                    t.owner = -1
                    t.id = names.global_variables.get(t.text.lower(), t.id)
                    continue
            if k >= 3:
                if toks[k - 1].type == T["LEFTBRACKET"] and toks[k - 2].type == T["IDENTIFIER"] \
                        and toks[k - 3].type == T["DOT"]:
                    if k + 1 < len(toks) and toks[k + 1].type in (T["RIGHTBRACKET"], T["COMMA"]):
                        standalone = False
            t.owner = oid if standalone else last_owner
            t.id = names.private_variables.get((t.owner, t.text.lower()), t.id)


def source_text(tokens: list[dict]) -> str:
    """The text the editor would show for stored tokens (CEditorParam::CreateParamString, names as stored)."""
    out = []
    for t in tokens:
        if t["type"] == T["STRINGLITERAL"]:
            out.append('"' + t["text"].replace("\n", "$n").replace("\t", "$t") + '"')
        elif t["type"] == T["VARIABLENAME"]:
            out.append("'" + t["text"] + "'")
        else:
            out.append(t["text"])
    return "".join(out)
