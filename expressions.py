"""Turn exported expression token streams back into editor syntax.

The exporter tokenises each parameter with the editor's own tokenizer
(IDE/Editors/Expressions/CExpression.cpp :: ParseUserString) and stores only the
tokens.  Rendering them with the spellings that tokenizer accepts gives text that
re-tokenises to the same stream.  Lost for good: whitespace, the original
spelling of synonyms (``floor`` -> ``int``, ``log`` -> ``ln``, ``and`` -> ``&``,
``!=`` -> ``<>``, ``Self`` -> the object name) and colour literals (exported as
plain integers).
"""

from __future__ import annotations

from . import model as m

_TEXT = {
    "LEFTBRACKET": "(", "RIGHTBRACKET": ")", "COMMA": ",", "DOT": ".", "LEFTCURLY": "{",
    "RIGHTCURLY": "}", "AT": "@", "ADD": "+", "SUBTRACT": "-", "MULTIPLY": "*", "DIVIDE": "/",
    "MOD": "%", "POWER": "^", "SIN": "sin", "COS": "cos", "TAN": "tan", "SQRT": "sqrt",
    "FUNC_STR": "str", "FUNC_INT": "int", "FUNC_FLOAT": "float", "EQUAL": "=", "LESS": "<",
    "GREATER": ">", "LESSEQUAL": "<=", "GREATEREQUAL": ">=", "NOTEQUAL": "<>", "CONDITIONAL": "?",
    "COLON": ":", "AND": "&", "OR": "or", "ASIN": "asin", "ACOS": "acos", "ATAN": "atan",
    "ABS": "abs", "EXP": "exp", "LN": "ln", "LOG10": "log10", "FLOOR": "floor", "CEIL": "ceil",
    "ROUND": "round", "RANDOM": "random", "LEN": "len",
}
_SPACED = {"ADD", "SUBTRACT", "MULTIPLY", "DIVIDE", "MOD", "POWER", "EQUAL", "LESS", "GREATER",
           "LESSEQUAL", "GREATEREQUAL", "NOTEQUAL", "CONDITIONAL", "COLON", "AND", "OR", "AT"}


def format_float(v: float) -> str:
    s = repr(float(v))
    if "e" in s or "n" in s:          # exponent / inf / nan: the tokenizer cannot read these
        s = f"{v:.17f}".rstrip("0")
    return s if "." in s else s + ".0"


def string_literal(s: str) -> str:
    # "" escapes a quote; $n/$t are the tokenizer's own newline/tab escapes.
    return '"' + s.replace('"', '""').replace("\n", "$n").replace("\t", "$t") + '"'


def token_text(t: m.Token) -> str:
    if t.type == m.T_INTEGER:
        return str(t.value)
    if t.type == m.T_FLOAT:
        return format_float(t.value)      # type: ignore[arg-type]
    if t.type == m.T_STRINGLITERAL:
        return string_literal(str(t.value))
    if t.type == m.T_IDENTIFIER:
        return str(t.value)
    if t.type == m.T_VARIABLENAME:
        return "'" + str(t.value) + "'"
    return _TEXT.get(t.name, f"<{t.name}>")


def render(tokens: list[m.Token]) -> str:
    """Editor-syntax text for one parameter's tokens."""
    out: list[str] = []
    for i, t in enumerate(tokens):
        s = token_text(t)
        if t.name in _SPACED:
            out.append(f" {s} ")
        elif t.name == "COMMA":
            out.append(", ")
        else:
            prev = tokens[i - 1] if i else None
            # Two operands in a row (e.g. a word operator handled above) need a separator.
            if prev is not None and prev.name not in _SPACED and prev.name not in ("LEFTBRACKET", "COMMA", "DOT",
                                                                                     "LEFTCURLY") \
                    and t.name not in ("RIGHTBRACKET", "DOT", "LEFTBRACKET", "RIGHTCURLY", "COMMA") \
                    and not (prev.type == m.T_IDENTIFIER and t.name == "LEFTBRACKET"):
                out.append(" ")
            out.append(s)
    return "".join(out).strip()


def single_int(tokens: list[m.Token]):
    """The integer if the parameter is exactly one integer token, else None."""
    if len(tokens) == 1 and tokens[0].type == m.T_INTEGER:
        return tokens[0].value
    return None


def varindex(tokens: list[m.Token]):
    """Decode the exporter's ``_varindex(index, oid)`` private-variable form."""
    names = [t.name for t in tokens]
    if names == ["IDENTIFIER", "LEFTBRACKET", "INTEGER", "COMMA", "INTEGER", "RIGHTBRACKET"] \
            and str(tokens[0].value).lower() == "_varindex":
        return tokens[2].value, tokens[4].value
    return None
