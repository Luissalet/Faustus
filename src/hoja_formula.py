"""src/hoja_formula.py -- the formula engine of Hoja (the spreadsheet): tokenizer, parser, evaluator and the
function library (well over a hundred of the most used functions).

* Formulas are accepted with English or Spanish function names (``SUM`` / ``SUMA``) and with either separator
  style: ``=SUM(A1:A3, 2.5)`` or ``=SUMA(A1:A3; 2,5)``. They are stored in the canonical English form
  (``parse`` + ``unparse``), which is also what the .xlsx file holds.
* Errors are values (``Err``): ``#DIV/0!``, ``#N/A``, ``#NAME?``, ``#NUM!``, ``#REF!``, ``#VALUE!`` and ``#CIRC!``
  for a circular reference. They flow through arithmetic and functions the way a sheet's do; ``IFERROR`` and the
  ``IS*`` functions catch them.
* The engine knows nothing about files or windows: the workbook (``src/hoja.py``) gives it a context with
  ``cell``, ``iter_range`` and ``bounds`` and decides what to recalculate (``refs`` lists what a formula reads).

MIT/Apache/BSD engines were considered and rejected: none handles the Spanish names and separators, error
values and the per-cell dependency listing this module gives the workbook, and it is under 1,200 lines.
"""
from __future__ import annotations

import datetime as _dt
import math
import re
import statistics
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from src import hoja_format as F

MAX_ROW = 1_048_576
MAX_COL = 16_384


# ---------------------------------------------------------------------------
# values and errors
# ---------------------------------------------------------------------------
class Err:
    __slots__ = ("code",)

    def __init__(self, code: str):
        self.code = code

    def __repr__(self) -> str:
        return self.code

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Err) and other.code == self.code

    def __hash__(self) -> int:
        return hash(self.code)


DIV0, NA, NAME, NULL, NUM, REF, VALUE, CIRC = (Err(c) for c in ("#DIV/0!", "#N/A", "#NAME?", "#NULL!", "#NUM!", "#REF!", "#VALUE!", "#CIRC!"))
ERRORS = {e.code: e for e in (DIV0, NA, NAME, NULL, NUM, REF, VALUE, CIRC)}


class ErrExc(Exception):
    def __init__(self, err: Err):
        super().__init__(err.code)
        self.err = err


def raise_err(err: Err):
    raise ErrExc(err)


# ---------------------------------------------------------------------------
# cell addresses
# ---------------------------------------------------------------------------
def col_letters(c: int) -> str:
    s = ""
    while c > 0:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return s


def col_number(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


_ADDR = re.compile(r"^\$?([A-Za-z]{1,3})\$?(\d{1,7})$")


def addr(r: int, c: int) -> str:
    return f"{col_letters(c)}{r}"


def parse_addr(text: str) -> Optional[Tuple[int, int]]:
    m = _ADDR.match(text.strip())
    if not m:
        return None
    c, r = col_number(m.group(1)), int(m.group(2))
    return (r, c) if 1 <= r <= MAX_ROW and 1 <= c <= MAX_COL else None


def parse_range(text: str) -> Optional[Tuple[int, int, int, int]]:
    """``A1``, ``A1:C5``, ``B:B``, ``3:5`` -> (r0, c0, r1, c1); open ends are MAX_ROW / MAX_COL."""
    t = text.strip().replace("$", "")
    if ":" not in t:
        a = parse_addr(t)
        return (a[0], a[1], a[0], a[1]) if a else None
    x, y = t.split(":", 1)
    a, b = parse_addr(x), parse_addr(y)
    if a and b:
        return (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
    if re.fullmatch(r"[A-Za-z]{1,3}", x) and re.fullmatch(r"[A-Za-z]{1,3}", y):
        c0, c1 = sorted((col_number(x), col_number(y)))
        return (1, c0, MAX_ROW, c1)
    if x.isdigit() and y.isdigit():
        r0, r1 = sorted((int(x), int(y)))
        return (r0, 1, r1, MAX_COL)
    return None


def range_text(r0: int, c0: int, r1: int, c1: int) -> str:
    if r1 >= MAX_ROW and c0 == c1 or (r1 >= MAX_ROW and r0 == 1):
        return f"{col_letters(c0)}:{col_letters(c1)}"
    if c1 >= MAX_COL and c0 == 1:
        return f"{r0}:{r1}"
    return addr(r0, c0) if (r0, c0) == (r1, c1) else f"{addr(r0, c0)}:{addr(r1, c1)}"


# ---------------------------------------------------------------------------
# tokenizer
# ---------------------------------------------------------------------------
class FormulaSyntax(Exception):
    pass


_SHEET = r"(?:'(?:[^']|'')+'|[A-Za-z_À-ɏ][\w.À-ɏ]*)!"
_CELL = r"\$?[A-Za-z]{1,3}\$?\d{1,7}"
_REF_RE = re.compile(rf"(?:{_SHEET})?(?:{_CELL}(?::{_CELL})?|\$?[A-Za-z]{{1,3}}:\$?[A-Za-z]{{1,3}}(?![\w(])|\$?\d{{1,7}}:\$?\d{{1,7}}(?![\w(]))")
_NAME_RE = re.compile(r"[A-Za-z_À-ɏ\\][\w.À-ɏ]*")
_ERR_RE = re.compile(r"#(?:DIV/0!|N/A|NAME\?|NULL!|NUM!|REF!|VALUE!|CIRC!)", re.I)
_OPS = ("<>", "<=", ">=", "=", "<", ">", "+", "-", "*", "/", "^", "&")

TRUE_NAMES = {"TRUE", "VERDADERO"}
FALSE_NAMES = {"FALSE", "FALSO"}


def tokenize(src: str, semi: bool) -> List[Tuple[str, Any]]:
    num_re = re.compile(r"(?:\d+(?:,\d+)?|,\d+)(?:[eE][+-]?\d+)?") if semi else re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
    out: List[Tuple[str, Any]] = []
    i, n = 0, len(src)
    while i < n:
        ch = src[i]
        if ch in " \t\r\n":
            i += 1
            continue
        if ch == '"':
            j, buf = i + 1, []
            while j < n:
                if src[j] == '"':
                    if j + 1 < n and src[j + 1] == '"':
                        buf.append('"')
                        j += 2
                        continue
                    break
                buf.append(src[j])
                j += 1
            else:
                raise FormulaSyntax("unterminated text")
            out.append(("str", "".join(buf)))
            i = j + 1
            continue
        if ch == "#":
            m = _ERR_RE.match(src, i)
            if not m:
                raise FormulaSyntax("bad error literal")
            out.append(("err", ERRORS[m.group(0).upper()]))
            i = m.end()
            continue
        if ch == "'" or ch.isalpha() or ch in "_$\\" or ch.isdigit() or ch == ".":
            # a reference first (it can start with a quote, a letter, $ or a digit)
            m = _REF_RE.match(src, i)
            nm = _NAME_RE.match(src, i) if (ch.isalpha() or ch in "_\\") else None
            if m and not (nm and nm.end() > m.end() and src[m.end():m.end() + 1] != "!"):
                end = m.end()
                nxt = src[end:end + 1]
                if nxt == "(" and not m.group(0).count("!") and not m.group(0).count(":"):
                    out.append(("func", m.group(0)))       # LOG10( , ATAN2( ...
                    i = end
                    continue
                if not (nxt and (nxt.isalnum() or nxt in "_") and not m.group(0).endswith(tuple("0123456789"))):
                    out.append(("ref", m.group(0)))
                    i = end
                    continue
            nm2 = nm
            if ch.isdigit() or ch == ".":
                m2 = num_re.match(src, i)
                if not m2:
                    raise FormulaSyntax(f"bad number near {src[i:i + 8]!r}")
                txt = m2.group(0).replace(",", ".") if semi else m2.group(0)
                out.append(("num", float(txt)))
                i = m2.end()
                continue
            if nm2:
                word = nm2.group(0)
                end = nm2.end()
                if src[end:end + 1] == "(":
                    out.append(("func", word))
                elif word.upper() in TRUE_NAMES:
                    out.append(("bool", True))
                elif word.upper() in FALSE_NAMES:
                    out.append(("bool", False))
                else:
                    out.append(("name", word))
                i = end
                continue
            raise FormulaSyntax(f"unexpected {ch!r}")
        if ch == "(":
            out.append(("(", ch)); i += 1; continue
        if ch == ")":
            out.append((")", ch)); i += 1; continue
        if ch == ";" or (ch == "," and not semi):
            out.append(("sep", ch)); i += 1; continue
        if ch == "%":
            out.append(("pct", ch)); i += 1; continue
        if ch == "{":
            raise FormulaSyntax("array constants are not supported")
        for op in _OPS:
            if src.startswith(op, i):
                out.append(("op", op)); i += len(op); break
        else:
            raise FormulaSyntax(f"unexpected {ch!r}")
    return out


# ---------------------------------------------------------------------------
# parser -> AST tuples
#   ('num', v) ('str', s) ('bool', b) ('err', Err) ('ref', sheet, r0, c0, r1, c1, (ar0, ac0, ar1, ac1))
#   ('un', op, x) ('bin', op, a, b) ('pct', x) ('call', NAME, [args]) ('name', text)
# ---------------------------------------------------------------------------
def _unquote_sheet(s: str) -> str:
    return s[1:-1].replace("''", "'") if s.startswith("'") else s


def _ref_node(text: str):
    sheet = None
    body = text
    if "!" in text:
        sh, body = text.rsplit("!", 1)
        sheet = _unquote_sheet(sh)
    parts = body.split(":")
    flags = []
    cells = []
    for p in parts:
        ac = p.startswith("$")
        q = p.lstrip("$")
        m = re.match(r"^([A-Za-z]+)(\$?)(\d+)$", q)
        if m:
            cells.append((int(m.group(3)), col_number(m.group(1))))
            flags.append((m.group(2) == "$", ac))
        elif q.isalpha():
            cells.append((None, col_number(q)))
            flags.append((False, ac))
        else:
            cells.append((int(q.lstrip("$")), None))
            flags.append((ac, False))
    a = cells[0]
    b = cells[-1]
    r0 = a[0] if a[0] is not None else 1
    c0 = a[1] if a[1] is not None else 1
    r1 = b[0] if b[0] is not None else MAX_ROW
    c1 = b[1] if b[1] is not None else MAX_COL
    if len(parts) == 2:
        if a[0] is None:
            r0, r1 = 1, MAX_ROW
        if a[1] is None:
            c0, c1 = 1, MAX_COL
    fl = (flags[0][0], flags[0][1], flags[-1][0], flags[-1][1])
    if r1 < r0 or c1 < c0:
        r0, r1 = min(r0, r1), max(r0, r1)
        c0, c1 = min(c0, c1), max(c0, c1)
    return ("ref", sheet, r0, c0, r1, c1, fl)


class Parser:
    def __init__(self, tokens: List[Tuple[str, Any]], canon: Callable[[str], str]):
        self.t = tokens
        self.i = 0
        self.canon = canon

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self):
        tok = self.peek()
        self.i += 1
        return tok

    def parse(self):
        node = self.comparison()
        if self.i != len(self.t):
            raise FormulaSyntax("unexpected " + repr(self.t[self.i][1]))
        return node

    def comparison(self):
        a = self.concat()
        while self.peek()[0] == "op" and self.peek()[1] in ("=", "<>", "<", ">", "<=", ">="):
            op = self.take()[1]
            a = ("bin", op, a, self.concat())
        return a

    def concat(self):
        a = self.additive()
        while self.peek() == ("op", "&"):
            self.take()
            a = ("bin", "&", a, self.additive())
        return a

    def additive(self):
        a = self.term()
        while self.peek()[0] == "op" and self.peek()[1] in "+-" and len(self.peek()[1]) == 1:
            op = self.take()[1]
            a = ("bin", op, a, self.term())
        return a

    def term(self):
        a = self.power()
        while self.peek()[0] == "op" and self.peek()[1] in ("*", "/"):
            op = self.take()[1]
            a = ("bin", op, a, self.power())
        return a

    def power(self):
        a = self.unary()
        while self.peek() == ("op", "^"):
            self.take()
            a = ("bin", "^", a, self.unary())
        return a

    def unary(self):
        if self.peek()[0] == "op" and self.peek()[1] in ("-", "+"):
            op = self.take()[1]
            x = self.unary()
            return ("un", op, x)
        return self.postfix()

    def postfix(self):
        a = self.atom()
        while self.peek()[0] == "pct":
            self.take()
            a = ("pct", a)
        return a

    def atom(self):
        kind, val = self.take()
        if kind == "num":
            return ("num", val)
        if kind == "str":
            return ("str", val)
        if kind == "bool":
            return ("bool", val)
        if kind == "err":
            return ("err", val)
        if kind == "ref":
            return _ref_node(val)
        if kind == "name":
            return ("name", val)
        if kind == "(":
            node = self.comparison()
            if self.take()[0] != ")":
                raise FormulaSyntax("missing )")
            return node
        if kind == "func":
            name = self.canon(val)
            if self.take()[0] != "(":
                raise FormulaSyntax("missing (")
            args = []
            if self.peek()[0] == ")":
                self.take()
                return ("call", name, args)
            while True:
                if self.peek()[0] == "sep":          # an omitted argument
                    args.append(("blank",))
                else:
                    args.append(self.comparison())
                tok = self.take()
                if tok[0] == ")":
                    break
                if tok[0] != "sep":
                    raise FormulaSyntax("expected , or )")
                if self.peek()[0] == ")":
                    args.append(("blank",))
                    self.take()
                    break
            return ("call", name, args)
        raise FormulaSyntax("unexpected end of formula" if kind is None else f"unexpected {val!r}")


_ES = {
    "SUMA": "SUM", "SUMAR.SI": "SUMIF", "SUMAR.SI.CONJUNTO": "SUMIFS", "SUMAPRODUCTO": "SUMPRODUCT", "PRODUCTO": "PRODUCT",
    "REDONDEAR": "ROUND", "REDONDEAR.MAS": "ROUNDUP", "REDONDEAR.MENOS": "ROUNDDOWN", "MULTIPLO.SUPERIOR": "CEILING",
    "MULTIPLO.INFERIOR": "FLOOR", "ENTERO": "INT", "RESIDUO": "MOD", "POTENCIA": "POWER", "RAIZ": "SQRT", "SIGNO": "SIGN",
    "TRUNCAR": "TRUNC", "ALEATORIO": "RAND", "ALEATORIO.ENTRE": "RANDBETWEEN", "PROMEDIO": "AVERAGE", "MEDIANA": "MEDIAN",
    "MODA": "MODE", "K.ESIMO.MAYOR": "LARGE", "K.ESIMO.MENOR": "SMALL", "JERARQUIA": "RANK", "CONTAR": "COUNT", "CONTARA": "COUNTA",
    "CONTAR.BLANCO": "COUNTBLANK", "CONTAR.SI": "COUNTIF", "CONTAR.SI.CONJUNTO": "COUNTIFS", "PROMEDIO.SI": "AVERAGEIF",
    "PROMEDIO.SI.CONJUNTO": "AVERAGEIFS", "MAX.SI.CONJUNTO": "MAXIFS", "MIN.SI.CONJUNTO": "MINIFS", "DESVEST": "STDEV",
    "DESVEST.M": "STDEV", "DESVESTP": "STDEVP", "VAR.S": "VAR", "COCIENTE": "QUOTIENT", "M.C.D": "GCD", "M.C.M": "LCM",
    "PERCENTIL": "PERCENTILE", "CUARTIL": "QUARTILE", "COEF.DE.CORREL": "CORREL", "PENDIENTE": "SLOPE",
    "INTERSECCION.EJE": "INTERCEPT", "PRONOSTICO": "FORECAST", "SI": "IF", "SI.CONJUNTO": "IFS", "Y": "AND", "O": "OR", "NO": "NOT",
    "O.EXCLUSIVO": "XOR", "SI.ERROR": "IFERROR", "SI.ND": "IFNA", "VERDADERO": "TRUE", "FALSO": "FALSE", "CAMBIAR": "SWITCH",
    "ESBLANCO": "ISBLANK", "ESNUMERO": "ISNUMBER", "ESTEXTO": "ISTEXT", "ESERROR": "ISERROR", "ESERR": "ISERR", "ESNOD": "ISNA",
    "ESLOGICO": "ISLOGICAL", "ES.PAR": "ISEVEN", "ES.IMPAR": "ISODD", "NOD": "NA", "LARGO": "LEN", "IZQUIERDA": "LEFT",
    "DERECHA": "RIGHT", "EXTRAE": "MID", "MAYUSC": "UPPER", "MINUSC": "LOWER", "NOMPROPIO": "PROPER", "ESPACIOS": "TRIM",
    "CONCATENAR": "CONCATENATE", "UNIRCADENAS": "TEXTJOIN", "ENCONTRAR": "FIND", "HALLAR": "SEARCH", "SUSTITUIR": "SUBSTITUTE",
    "REEMPLAZAR": "REPLACE", "REPETIR": "REPT", "TEXTO": "TEXT", "VALOR": "VALUE", "IGUAL": "EXACT", "CARACTER": "CHAR",
    "CODIGO": "CODE", "LIMPIAR": "CLEAN", "VALOR.NUMERO": "NUMBERVALUE", "HOY": "TODAY", "AHORA": "NOW", "FECHA": "DATE",
    "DIA": "DAY", "MES": "MONTH", "AÑO": "YEAR", "ANO": "YEAR", "DIASEM": "WEEKDAY", "FECHA.MES": "EDATE", "FIN.MES": "EOMONTH",
    "DIAS": "DAYS", "SIFECHA": "DATEDIF", "DIAS.LAB": "NETWORKDAYS", "DIA.LAB": "WORKDAY", "HORA": "HOUR", "MINUTO": "MINUTE",
    "SEGUNDO": "SECOND", "NSHORA": "TIME", "FECHANUMERO": "DATEVALUE", "NUM.DE.SEMANA": "WEEKNUM", "FRAC.AÑO": "YEARFRAC",
    "FRAC.ANO": "YEARFRAC", "BUSCARV": "VLOOKUP", "BUSCARH": "HLOOKUP", "BUSCARX": "XLOOKUP", "BUSCAR": "LOOKUP",
    "COINCIDIR": "MATCH", "INDICE": "INDEX", "ELEGIR": "CHOOSE", "FILA": "ROW", "COLUMNA": "COLUMN", "FILAS": "ROWS",
    "COLUMNAS": "COLUMNS", "INDIRECTO": "INDIRECT", "PAGO": "PMT", "VF": "FV", "VA": "PV", "VNA": "NPV", "TIR": "IRR",
    "TASA": "RATE", "SLN": "SLN", "PAGOINT": "IPMT", "PI": "PI", "ABS": "ABS", "EXP": "EXP", "LN": "LN", "LOG": "LOG",
    "LOG10": "LOG10", "FACT": "FACT", "MAX": "MAX", "MIN": "MIN", "NPER": "NPER", "CONCAT": "CONCAT", "DESVESTA": "STDEV",
    "SUBTOTALES": "SUBTOTAL", "MIN.A": "MIN", "N": "N", "T": "T", "ESPAR": "ISEVEN", "ESIMPAR": "ISODD",
}
FUNCS: Dict[str, Callable] = {}
LAZY: Dict[str, Callable] = {}


def canonical_name(name: str) -> str:
    up = name.upper()
    return _ES.get(up, up)


def detect_semi(src: str) -> bool:
    """A ``;`` outside quotes means the Spanish style (``;`` between arguments, ``,`` as the decimal mark)."""
    inq = False
    for ch in src:
        if ch == '"':
            inq = not inq
        elif ch == ";" and not inq:
            return True
    return False


def parse(formula: str):
    """``=SUMA(A1:A3; 2,5)`` -> AST. Raises FormulaSyntax."""
    src = formula.strip()
    if src.startswith("="):
        src = src[1:]
    if not src.strip():
        raise FormulaSyntax("empty formula")
    return Parser(tokenize(src, detect_semi(src)), canonical_name).parse()


# -- unparse (canonical English, comma separators) ---------------------------------
_PREC = {"=": 1, "<>": 1, "<": 1, ">": 1, "<=": 1, ">=": 1, "&": 2, "+": 3, "-": 3, "*": 4, "/": 4, "^": 5}


def _sheet_text(name: str) -> str:
    return "'" + name.replace("'", "''") + "'" if re.search(r"[^\w.]", name) or not name or name[0].isdigit() else name


def ref_text(node) -> str:
    _, sheet, r0, c0, r1, c1, fl = node
    ar0, ac0, ar1, ac1 = fl
    pre = _sheet_text(sheet) + "!" if sheet else ""
    whole_col = r0 == 1 and r1 >= MAX_ROW
    whole_row = c0 == 1 and c1 >= MAX_COL
    d = lambda f: "$" if f else ""
    if whole_col and not whole_row:
        return f"{pre}{d(ac0)}{col_letters(c0)}:{d(ac1)}{col_letters(c1)}"
    if whole_row and not whole_col:
        return f"{pre}{d(ar0)}{r0}:{d(ar1)}{r1}"
    a = f"{d(ac0)}{col_letters(c0)}{d(ar0)}{r0}"
    if (r0, c0) == (r1, c1):
        return pre + a
    return f"{pre}{a}:{d(ac1)}{col_letters(c1)}{d(ar1)}{r1}"


def unparse(node, parent_prec: int = 0) -> str:
    k = node[0]
    if k == "num":
        v = node[1]
        return F.general(int(v)) if float(v).is_integer() and abs(v) < 1e15 else repr(float(v))
    if k == "str":
        return '"' + node[1].replace('"', '""') + '"'
    if k == "bool":
        return "TRUE" if node[1] else "FALSE"
    if k == "err":
        return node[1].code
    if k == "blank":
        return ""
    if k == "name":
        return node[1]
    if k == "ref":
        return ref_text(node)
    if k == "un":
        return node[1] + unparse(node[2], 6)
    if k == "pct":
        return unparse(node[1], 7) + "%"
    if k == "bin":
        p = _PREC[node[1]]
        s = unparse(node[2], p) + node[1] + unparse(node[3], p + 1)
        return "(" + s + ")" if p < parent_prec else s
    if k == "call":
        return node[1] + "(" + ",".join(unparse(a) for a in node[2]) + ")"
    raise ValueError(k)


def canonical(formula: str) -> str:
    return "=" + unparse(parse(formula))


def walk(node) -> Iterator[Tuple]:
    yield node
    k = node[0]
    if k in ("un", "pct"):
        yield from walk(node[-1])
    elif k == "bin":
        yield from walk(node[2])
        yield from walk(node[3])
    elif k == "call":
        for a in node[2]:
            yield from walk(a)


def refs(node) -> List[Tuple[Optional[str], int, int, int, int]]:
    """Every ``(sheet|None, r0, c0, r1, c1)`` a formula reads (INDIRECT / OFFSET targets are not visible here)."""
    return [(n[1], n[2], n[3], n[4], n[5]) for n in walk(node) if n[0] == "ref"]


def volatile(node) -> bool:
    return any(n[0] == "call" and n[1] in ("TODAY", "NOW", "RAND", "RANDBETWEEN", "INDIRECT", "OFFSET") for n in walk(node))


def transform_refs(node, fn: Callable[[Tuple], Optional[Tuple]]):
    """A copy of the tree with every ref node replaced by ``fn(node)`` (None -> ``#REF!``)."""
    k = node[0]
    if k == "ref":
        out = fn(node)
        return ("err", REF) if out is None else out
    if k in ("un",):
        return ("un", node[1], transform_refs(node[2], fn))
    if k == "pct":
        return ("pct", transform_refs(node[1], fn))
    if k == "bin":
        return ("bin", node[1], transform_refs(node[2], fn), transform_refs(node[3], fn))
    if k == "call":
        return ("call", node[1], [transform_refs(a, fn) for a in node[2]])
    return node


def shift_relative(node, dr: int, dc: int):
    """Copy-and-paste semantics: relative parts move by (dr, dc), absolute ones stay."""
    def fn(n):
        _, sh, r0, c0, r1, c1, fl = n
        ar0, ac0, ar1, ac1 = fl
        whole_col = r0 == 1 and r1 >= MAX_ROW
        whole_row = c0 == 1 and c1 >= MAX_COL
        nr0 = r0 if (ar0 or whole_col) else r0 + dr
        nr1 = r1 if (ar1 or whole_col) else r1 + dr
        nc0 = c0 if (ac0 or whole_row) else c0 + dc
        nc1 = c1 if (ac1 or whole_row) else c1 + dc
        if min(nr0, nr1, nc0, nc1) < 1 or nr1 > MAX_ROW or nc1 > MAX_COL:
            return None
        return ("ref", sh, nr0, nc0, nr1, nc1, fl)
    return transform_refs(node, fn)


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
class Rng:
    """A range as the evaluator sees it: lazy, clamped to what the sheet uses."""
    __slots__ = ("ctx", "tab", "r0", "c0", "r1", "c1")

    def __init__(self, ctx, tab: str, r0: int, c0: int, r1: int, c1: int):
        self.ctx, self.tab = ctx, tab
        mr, mc = ctx.bounds(tab)
        self.r0, self.c0 = r0, c0
        self.r1, self.c1 = (min(r1, max(mr, r0)) if r1 >= MAX_ROW else r1), (min(c1, max(mc, c0)) if c1 >= MAX_COL else c1)

    @property
    def rows(self) -> int:
        return self.r1 - self.r0 + 1

    @property
    def cols(self) -> int:
        return self.c1 - self.c0 + 1

    def matrix(self) -> List[List[Any]]:
        if self.rows * self.cols > 2_000_000:
            raise_err(VALUE)
        cell = self.ctx.cell
        return [[cell(self.tab, r, c) for c in range(self.c0, self.c1 + 1)] for r in range(self.r0, self.r1 + 1)]

    def present(self) -> Iterator[Any]:
        """Values of the cells that exist (sparse)."""
        for _r, _c, v in self.ctx.iter_range(self.tab, self.r0, self.c0, self.r1, self.c1):
            yield v

    def __iter__(self):
        for row in self.matrix():
            yield from row


def is_matrix(v: Any) -> bool:
    return isinstance(v, (Rng, list))


def to_matrix(v: Any) -> List[List[Any]]:
    if isinstance(v, Rng):
        return v.matrix()
    if isinstance(v, list):
        return v if (v and isinstance(v[0], list)) else [list(v)]
    return [[v]]


def scalar(v: Any) -> Any:
    """A range or array in a scalar place: its top-left cell (a sheet would spill; Hoja does not)."""
    if isinstance(v, Rng):
        return v.ctx.cell(v.tab, v.r0, v.c0)
    if isinstance(v, list):
        m = to_matrix(v)
        return m[0][0] if m and m[0] else None
    return v


_NUM_TEXT = re.compile(r"^\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*$")


def to_num(v: Any) -> float:
    v = scalar(v)
    if isinstance(v, Err):
        raise ErrExc(v)
    if v is None or v == "":
        return 0
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        if _NUM_TEXT.match(v):
            f = float(v)
            return int(f) if f.is_integer() and "." not in v and "e" not in v.lower() else f
        d = F.parse_date(v)
        if d is not None:
            return d
        t = v.strip()
        if t.endswith("%") and _NUM_TEXT.match(t[:-1]):
            return float(t[:-1]) / 100
        raise ErrExc(VALUE)
    raise ErrExc(VALUE)


def to_str(v: Any) -> str:
    v = scalar(v)
    if isinstance(v, Err):
        raise ErrExc(v)
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, str):
        return v
    return F.general(v)


def to_bool(v: Any) -> bool:
    v = scalar(v)
    if isinstance(v, Err):
        raise ErrExc(v)
    if v is None:
        return False
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        if v.upper() in TRUE_NAMES:
            return True
        if v.upper() in FALSE_NAMES:
            return False
    raise ErrExc(VALUE)


def _cmp_key(v: Any):
    if isinstance(v, bool):
        return (2, 1 if v else 0)
    if isinstance(v, (int, float)):
        return (0, v)
    return (1, str(v).lower())


def compare(a: Any, b: Any) -> int:
    a, b = scalar(a), scalar(b)
    for x in (a, b):
        if isinstance(x, Err):
            raise ErrExc(x)
    if a is None:
        a = "" if isinstance(b, str) else (False if isinstance(b, bool) else 0)
    if b is None:
        b = "" if isinstance(a, str) else (False if isinstance(a, bool) else 0)
    ka, kb = _cmp_key(a), _cmp_key(b)
    return (ka > kb) - (ka < kb)


def _elementwise(fn: Callable[[Any, Any], Any], a: Any, b: Any):
    if not (is_matrix(a) or is_matrix(b)):
        return fn(a, b)
    ma, mb = to_matrix(a), to_matrix(b)
    rows = max(len(ma), len(mb))
    cols = max(len(ma[0]), len(mb[0]))
    if (len(ma) not in (1, rows) and len(ma) != rows) or (len(mb) not in (1, rows)) or len(ma[0]) not in (1, cols) or len(mb[0]) not in (1, cols):
        raise ErrExc(VALUE)
    out = []
    for i in range(rows):
        row = []
        for j in range(cols):
            x = ma[i if len(ma) > 1 else 0][j if len(ma[0]) > 1 else 0]
            y = mb[i if len(mb) > 1 else 0][j if len(mb[0]) > 1 else 0]
            try:
                row.append(fn(x, y))
            except ErrExc as e:
                row.append(e.err)
        out.append(row)
    return out


def _binop(op: str, a: Any, b: Any) -> Any:
    if op == "+":
        return to_num(a) + to_num(b)
    if op == "-":
        return to_num(a) - to_num(b)
    if op == "*":
        return to_num(a) * to_num(b)
    if op == "/":
        d = to_num(b)
        if d == 0:
            raise ErrExc(DIV0)
        return to_num(a) / d
    if op == "^":
        x, y = to_num(a), to_num(b)
        if x == 0 and y < 0:
            raise ErrExc(DIV0)
        try:
            r = x ** y
        except (OverflowError, ZeroDivisionError):
            raise ErrExc(NUM) from None
        if isinstance(r, complex):
            raise ErrExc(NUM)
        return r
    if op == "&":
        return to_str(a) + to_str(b)
    c = compare(a, b)
    return {"=": c == 0, "<>": c != 0, "<": c < 0, ">": c > 0, "<=": c <= 0, ">=": c >= 0}[op]


def _tidy(v: Any) -> Any:
    if isinstance(v, float):
        if v != v or v in (math.inf, -math.inf):
            raise ErrExc(NUM)
        if abs(v) > 1e300:
            raise ErrExc(NUM)
        r = round(v, 14) if abs(v) < 1e15 else v
        return int(r) if r.is_integer() and abs(r) < 1e15 else r
    return v


def evaluate(node, ctx) -> Any:
    """Evaluate an AST; returns a value, ``Err``, a ``Rng`` (for a bare range) or a matrix."""
    k = node[0]
    if k == "num":
        return node[1] if not float(node[1]).is_integer() else int(node[1])
    if k == "str":
        return node[1]
    if k == "bool":
        return node[1]
    if k == "err":
        return node[1]
    if k == "blank":
        return None
    if k == "name":
        return NAME
    if k == "ref":
        _, sheet, r0, c0, r1, c1, _fl = node
        tab = ctx.resolve_tab(sheet) if sheet else ctx.cur_tab
        if tab is None:
            return REF
        if (r0, c0) == (r1, c1):
            return ctx.cell(tab, r0, c0)
        return Rng(ctx, tab, r0, c0, r1, c1)
    try:
        if k == "un":
            x = evaluate(node[2], ctx)
            if node[1] == "-":
                return _elementwise(lambda a, _b: -to_num(a), x, 0) if is_matrix(x) else -to_num(x)
            return x
        if k == "pct":
            x = evaluate(node[1], ctx)
            return _elementwise(lambda a, _b: to_num(a) / 100, x, 0) if is_matrix(x) else to_num(x) / 100
        if k == "bin":
            a = evaluate(node[2], ctx)
            b = evaluate(node[3], ctx)
            op = node[1]
            if is_matrix(a) or is_matrix(b):
                return _elementwise(lambda x, y: _tidy(_binop(op, x, y)), a, b)
            return _tidy(_binop(op, a, b))
        if k == "call":
            name = node[1]
            if name in LAZY:
                return LAZY[name](ctx, node[2])
            f = FUNCS.get(name)
            if f is None:
                return NAME
            return f(ctx, *[evaluate(a, ctx) for a in node[2]])
    except ErrExc as exc:
        return exc.err
    except RecursionError:
        return CIRC
    raise ValueError(k)


def final_value(v: Any) -> Any:
    """What a cell stores for a formula's result."""
    v = scalar(v)
    if isinstance(v, float):
        try:
            return _tidy(v)
        except ErrExc as e:
            return e.err
    return v


# ---------------------------------------------------------------------------
# function library
# ---------------------------------------------------------------------------
def fn(*names: str, lazy: bool = False):
    def deco(f):
        for n in names:
            (LAZY if lazy else FUNCS)[n] = f
        return f
    return deco


def _check(v: Any) -> Any:
    if isinstance(v, Err):
        raise ErrExc(v)
    return v


def numbers_in(args: Sequence[Any], *, direct_strict: bool = True) -> List[float]:
    """The numbers SUM / AVERAGE / MAX take: numbers inside ranges and arrays (text, blanks and booleans there are
    ignored), but scalars given directly are coerced (``SUM("3", TRUE)`` is 4)."""
    out: List[float] = []
    for a in args:
        if isinstance(a, Rng):
            for v in a.present():
                if isinstance(v, Err):
                    raise ErrExc(v)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out.append(v)
        elif isinstance(a, list):
            for row in to_matrix(a):
                for v in row:
                    if isinstance(v, Err):
                        raise ErrExc(v)
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        out.append(v)
        else:
            if isinstance(a, Err):
                raise ErrExc(a)
            if a is None:
                continue
            out.append(to_num(a))
    return out


def values_in(arg: Any) -> List[Any]:
    if isinstance(arg, Rng):
        return list(arg.present())
    if isinstance(arg, list):
        return [v for row in to_matrix(arg) for v in row if v is not None]
    return [] if arg is None else [arg]


def all_values(arg: Any) -> List[Any]:
    """Every cell including blanks, row by row."""
    return [v for row in to_matrix(arg) for v in row]


def _int_arg(v: Any, default: Optional[int] = None) -> int:
    if v is None and default is not None:
        return default
    return int(math.floor(to_num(v))) if to_num(v) >= 0 else int(math.ceil(to_num(v)))


# -- math & aggregates -------------------------------------------------------------
@fn("SUM")
def f_sum(ctx, *a):
    return _tidy(float(math.fsum(numbers_in(a))))


@fn("PRODUCT")
def f_product(ctx, *a):
    p = 1.0
    for x in numbers_in(a):
        p *= x
    return _tidy(p)


@fn("AVERAGE")
def f_average(ctx, *a):
    xs = numbers_in(a)
    if not xs:
        raise ErrExc(DIV0)
    return _tidy(math.fsum(xs) / len(xs))


@fn("MAX")
def f_max(ctx, *a):
    xs = numbers_in(a)
    return max(xs) if xs else 0


@fn("MIN")
def f_min(ctx, *a):
    xs = numbers_in(a)
    return min(xs) if xs else 0


@fn("MEDIAN")
def f_median(ctx, *a):
    xs = numbers_in(a)
    if not xs:
        raise ErrExc(NUM)
    return _tidy(statistics.median(xs))


@fn("MODE")
def f_mode(ctx, *a):
    xs = numbers_in(a)
    best, cnt = None, 1
    seen: Dict[float, int] = {}
    for x in xs:
        seen[x] = seen.get(x, 0) + 1
    for x in xs:
        if seen[x] > cnt:
            best, cnt = x, seen[x]
    if best is None:
        raise ErrExc(NA)
    return best


@fn("STDEV")
def f_stdev(ctx, *a):
    xs = numbers_in(a)
    if len(xs) < 2:
        raise ErrExc(DIV0)
    return _tidy(statistics.stdev(xs))


@fn("STDEVP")
def f_stdevp(ctx, *a):
    xs = numbers_in(a)
    if not xs:
        raise ErrExc(DIV0)
    return _tidy(statistics.pstdev(xs))


@fn("VAR")
def f_var(ctx, *a):
    xs = numbers_in(a)
    if len(xs) < 2:
        raise ErrExc(DIV0)
    return _tidy(statistics.variance(xs))


@fn("LARGE")
def f_large(ctx, rng, k):
    xs = sorted(numbers_in([rng]), reverse=True)
    i = _int_arg(k)
    if not 1 <= i <= len(xs):
        raise ErrExc(NUM)
    return xs[i - 1]


@fn("SMALL")
def f_small(ctx, rng, k):
    xs = sorted(numbers_in([rng]))
    i = _int_arg(k)
    if not 1 <= i <= len(xs):
        raise ErrExc(NUM)
    return xs[i - 1]


@fn("RANK")
def f_rank(ctx, x, rng, order=None):
    v = to_num(x)
    xs = numbers_in([rng])
    if v not in xs:
        raise ErrExc(NA)
    asc = order is not None and to_num(order) != 0
    return 1 + sum(1 for y in xs if (y < v if asc else y > v))


@fn("PERCENTILE")
def f_percentile(ctx, rng, k):
    xs = sorted(numbers_in([rng]))
    p = to_num(k)
    if not xs or not 0 <= p <= 1:
        raise ErrExc(NUM)
    pos = p * (len(xs) - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, len(xs) - 1)
    return _tidy(xs[lo] + (xs[hi] - xs[lo]) * (pos - lo))


@fn("QUARTILE")
def f_quartile(ctx, rng, q):
    qi = _int_arg(q)
    if not 0 <= qi <= 4:
        raise ErrExc(NUM)
    return f_percentile(ctx, rng, qi / 4)


@fn("COUNT")
def f_count(ctx, *a):
    n = 0
    for x in a:
        if isinstance(x, (Rng, list)):
            n += sum(1 for v in values_in(x) if isinstance(v, (int, float)) and not isinstance(v, bool))
        elif isinstance(x, (int, float)) and not isinstance(x, bool):
            n += 1
        elif isinstance(x, str):
            try:
                to_num(x)
                n += 1
            except ErrExc:
                pass
    return n


@fn("COUNTA")
def f_counta(ctx, *a):
    n = 0
    for x in a:
        n += sum(1 for v in values_in(x) if v is not None and v != "") if isinstance(x, (Rng, list)) else (0 if x is None else 1)
    return n


@fn("COUNTBLANK")
def f_countblank(ctx, rng):
    m = to_matrix(rng)
    return sum(1 for row in m for v in row if v is None or v == "")


@fn("ABS")
def f_abs(ctx, x):
    return abs(to_num(x))


@fn("SIGN")
def f_sign(ctx, x):
    v = to_num(x)
    return (v > 0) - (v < 0)


def _round_half_away(x: float, d: int) -> float:
    m = 10 ** d
    return math.copysign(math.floor(abs(x) * m + 0.5 + 1e-9) / m, x)


@fn("ROUND")
def f_round(ctx, x, d=0):
    return _tidy(_round_half_away(to_num(x), _int_arg(d, 0)))


@fn("ROUNDUP")
def f_roundup(ctx, x, d=0):
    v, n = to_num(x), _int_arg(d, 0)
    m = 10 ** n
    return _tidy(math.copysign(math.ceil(abs(v) * m - 1e-9) / m, v))


@fn("ROUNDDOWN", "TRUNC")
def f_rounddown(ctx, x, d=0):
    v, n = to_num(x), _int_arg(d, 0)
    m = 10 ** n
    return _tidy(math.copysign(math.floor(abs(v) * m + 1e-9) / m, v))


@fn("CEILING")
def f_ceiling(ctx, x, sig=1):
    v, s = to_num(x), to_num(sig)
    if s == 0:
        return 0
    return _tidy(math.ceil(v / s - 1e-12) * s)


@fn("FLOOR")
def f_floor(ctx, x, sig=1):
    v, s = to_num(x), to_num(sig)
    if s == 0:
        raise ErrExc(DIV0)
    return _tidy(math.floor(v / s + 1e-12) * s)


@fn("INT")
def f_int(ctx, x):
    return int(math.floor(to_num(x)))


@fn("MOD")
def f_mod(ctx, x, y):
    a, b = to_num(x), to_num(y)
    if b == 0:
        raise ErrExc(DIV0)
    return _tidy(a - b * math.floor(a / b))


@fn("QUOTIENT")
def f_quotient(ctx, x, y):
    b = to_num(y)
    if b == 0:
        raise ErrExc(DIV0)
    return int(to_num(x) / b)


@fn("POWER")
def f_power(ctx, x, y):
    return _tidy(_binop("^", x, y))


@fn("SQRT")
def f_sqrt(ctx, x):
    v = to_num(x)
    if v < 0:
        raise ErrExc(NUM)
    return _tidy(math.sqrt(v))


@fn("EXP")
def f_exp(ctx, x):
    try:
        return _tidy(math.exp(to_num(x)))
    except OverflowError:
        raise ErrExc(NUM) from None


@fn("LN")
def f_ln(ctx, x):
    v = to_num(x)
    if v <= 0:
        raise ErrExc(NUM)
    return _tidy(math.log(v))


@fn("LOG")
def f_log(ctx, x, base=10):
    v, b = to_num(x), to_num(base)
    if v <= 0 or b <= 0 or b == 1:
        raise ErrExc(NUM)
    return _tidy(math.log(v, b))


@fn("LOG10")
def f_log10(ctx, x):
    v = to_num(x)
    if v <= 0:
        raise ErrExc(NUM)
    return _tidy(math.log10(v))


@fn("PI")
def f_pi(ctx):
    return math.pi


@fn("FACT")
def f_fact(ctx, x):
    n = _int_arg(x)
    if n < 0 or n > 170:
        raise ErrExc(NUM)
    return math.factorial(n)


@fn("GCD")
def f_gcd(ctx, *a):
    xs = [int(x) for x in numbers_in(a)]
    return math.gcd(*xs) if xs else 0


@fn("LCM")
def f_lcm(ctx, *a):
    xs = [int(x) for x in numbers_in(a)]
    out = 1
    for x in xs:
        out = abs(out * x) // math.gcd(out, x) if x else 0
    return out


@fn("RAND")
def f_rand(ctx):
    return ctx.random()


@fn("RANDBETWEEN")
def f_randbetween(ctx, lo, hi):
    a, b = _int_arg(lo), _int_arg(hi)
    if a > b:
        raise ErrExc(NUM)
    return a + int(ctx.random() * (b - a + 1))


@fn("SUMPRODUCT")
def f_sumproduct(ctx, *a):
    mats = [to_matrix(x) for x in a]
    if not mats:
        raise ErrExc(VALUE)
    shape = (len(mats[0]), len(mats[0][0]))
    if any((len(m), len(m[0])) != shape for m in mats):
        raise ErrExc(VALUE)
    total = 0.0
    for i in range(shape[0]):
        for j in range(shape[1]):
            p = 1.0
            for m in mats:
                v = m[i][j]
                if isinstance(v, Err):
                    raise ErrExc(v)
                p *= v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
            total += p
    return _tidy(total)


@fn("SUBTOTAL")
def f_subtotal(ctx, code, *a):
    c = _int_arg(code) % 100
    table = {1: "AVERAGE", 2: "COUNT", 3: "COUNTA", 4: "MAX", 5: "MIN", 6: "PRODUCT", 7: "STDEV", 8: "STDEVP", 9: "SUM", 10: "VAR"}
    if c not in table:
        raise ErrExc(VALUE)
    return FUNCS[table[c]](ctx, *a)


@fn("CORREL")
def f_correl(ctx, a, b):
    xs, ys = _pairs(a, b)
    if len(xs) < 2:
        raise ErrExc(DIV0)
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx, syy = sum((x - mx) ** 2 for x in xs), sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        raise ErrExc(DIV0)
    return _tidy(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sxx * syy))


def _pairs(a, b):
    ma, mb = [v for row in to_matrix(a) for v in row], [v for row in to_matrix(b) for v in row]
    if len(ma) != len(mb):
        raise ErrExc(NA)
    xs, ys = [], []
    for x, y in zip(ma, mb):
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) and not isinstance(x, bool) and not isinstance(y, bool):
            xs.append(float(x))
            ys.append(float(y))
    return xs, ys


def _linfit(known_y, known_x):
    ys, xs = _pairs(known_y, known_x)
    if len(xs) < 2:
        raise ErrExc(DIV0)
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        raise ErrExc(DIV0)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return slope, my - slope * mx


@fn("SLOPE")
def f_slope(ctx, ky, kx):
    return _tidy(_linfit(ky, kx)[0])


@fn("INTERCEPT")
def f_intercept(ctx, ky, kx):
    return _tidy(_linfit(ky, kx)[1])


@fn("FORECAST")
def f_forecast(ctx, x, ky, kx):
    s, i = _linfit(ky, kx)
    return _tidy(i + s * to_num(x))


# -- conditional aggregates --------------------------------------------------------
_CRIT = re.compile(r"^(<>|>=|<=|=|>|<)?(.*)$", re.S)


def make_criterion(c: Any) -> Callable[[Any], bool]:
    c = scalar(c)
    if isinstance(c, Err):
        raise ErrExc(c)
    if isinstance(c, (int, float)) and not isinstance(c, bool):
        return lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and v == c
    if isinstance(c, bool):
        return lambda v: v is c
    s = "" if c is None else str(c)
    op, rest = _CRIT.match(s).groups()
    op = op or "="
    try:
        num = float(rest) if _NUM_TEXT.match(rest) else None
    except ValueError:
        num = None
    if num is None:
        d = F.parse_date(rest)
        num = float(d) if d is not None and rest.strip() else None
    pat = None
    if num is None and ("*" in rest or "?" in rest):
        pat = re.compile("^" + "".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in rest) + "$", re.I | re.S)

    def test(v: Any) -> bool:
        if isinstance(v, Err):
            return False
        if num is not None:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                x = float(v)
                return {"=": x == num, "<>": x != num, ">": x > num, "<": x < num, ">=": x >= num, "<=": x <= num}[op]
            return op == "<>"
        t = "" if v is None else (v if isinstance(v, str) else to_str(v))
        if op in ("=", "<>"):
            hit = bool(pat.match(t)) if pat else t.lower() == rest.lower()
            if rest == "" and op == "=":
                hit = v is None or t == ""
            return hit if op == "=" else not hit
        if not isinstance(v, str):
            return False
        a, b = t.lower(), rest.lower()
        return {">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]
    return test


def _cond_positions(pairs: Sequence[Tuple[Any, Any]]) -> List[Tuple[int, int]]:
    mats = [(to_matrix(r), make_criterion(c)) for r, c in pairs]
    rows, cols = len(mats[0][0]), len(mats[0][0][0])
    for m, _ in mats:
        if len(m) != rows or len(m[0]) != cols:
            raise ErrExc(VALUE)
    return [(i, j) for i in range(rows) for j in range(cols) if all(t(m[i][j]) for m, t in mats)]


def _pick(rng: Any, pos: List[Tuple[int, int]]) -> List[Any]:
    m = to_matrix(rng)
    return [m[i][j] for i, j in pos if i < len(m) and j < len(m[0])]


def _num_only(vals: Iterable[Any]) -> List[float]:
    out = []
    for v in vals:
        if isinstance(v, Err):
            raise ErrExc(v)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out.append(v)
    return out


@fn("COUNTIF")
def f_countif(ctx, rng, crit):
    return len(_cond_positions([(rng, crit)]))


@fn("COUNTIFS")
def f_countifs(ctx, *a):
    if len(a) < 2 or len(a) % 2:
        raise ErrExc(VALUE)
    return len(_cond_positions(list(zip(a[::2], a[1::2]))))


@fn("SUMIF")
def f_sumif(ctx, rng, crit, sum_rng=None):
    pos = _cond_positions([(rng, crit)])
    return _tidy(math.fsum(_num_only(_pick(sum_rng if sum_rng is not None else rng, pos))))


@fn("SUMIFS")
def f_sumifs(ctx, sum_rng, *a):
    pos = _cond_positions(list(zip(a[::2], a[1::2])))
    return _tidy(math.fsum(_num_only(_pick(sum_rng, pos))))


@fn("AVERAGEIF")
def f_averageif(ctx, rng, crit, avg_rng=None):
    xs = _num_only(_pick(avg_rng if avg_rng is not None else rng, _cond_positions([(rng, crit)])))
    if not xs:
        raise ErrExc(DIV0)
    return _tidy(math.fsum(xs) / len(xs))


@fn("AVERAGEIFS")
def f_averageifs(ctx, avg_rng, *a):
    xs = _num_only(_pick(avg_rng, _cond_positions(list(zip(a[::2], a[1::2])))))
    if not xs:
        raise ErrExc(DIV0)
    return _tidy(math.fsum(xs) / len(xs))


@fn("MAXIFS")
def f_maxifs(ctx, rng, *a):
    xs = _num_only(_pick(rng, _cond_positions(list(zip(a[::2], a[1::2])))))
    return max(xs) if xs else 0


@fn("MINIFS")
def f_minifs(ctx, rng, *a):
    xs = _num_only(_pick(rng, _cond_positions(list(zip(a[::2], a[1::2])))))
    return min(xs) if xs else 0


# -- logic ---------------------------------------------------------------------------
@fn("IF", lazy=True)
def f_if(ctx, args):
    if len(args) < 2:
        raise ErrExc(VALUE)
    try:
        cond = to_bool(evaluate(args[0], ctx))
    except ErrExc as e:
        return e.err
    if cond:
        return evaluate(args[1], ctx)
    return evaluate(args[2], ctx) if len(args) > 2 else False


@fn("IFS", lazy=True)
def f_ifs(ctx, args):
    for i in range(0, len(args) - 1, 2):
        try:
            if to_bool(evaluate(args[i], ctx)):
                return evaluate(args[i + 1], ctx)
        except ErrExc as e:
            return e.err
    return NA


@fn("IFERROR", lazy=True)
def f_iferror(ctx, args):
    v = scalar(evaluate(args[0], ctx))
    return evaluate(args[1], ctx) if isinstance(v, Err) else v


@fn("IFNA", lazy=True)
def f_ifna(ctx, args):
    v = scalar(evaluate(args[0], ctx))
    return evaluate(args[1], ctx) if v == NA else v


@fn("SWITCH", lazy=True)
def f_switch(ctx, args):
    target = scalar(evaluate(args[0], ctx))
    if isinstance(target, Err):
        return target
    rest = args[1:]
    for i in range(0, len(rest) - 1, 2):
        v = scalar(evaluate(rest[i], ctx))
        try:
            if compare(target, v) == 0:
                return evaluate(rest[i + 1], ctx)
        except ErrExc as e:
            return e.err
    return evaluate(rest[-1], ctx) if len(rest) % 2 else NA


@fn("CHOOSE", lazy=True)
def f_choose(ctx, args):
    try:
        i = _int_arg(evaluate(args[0], ctx))
    except ErrExc as e:
        return e.err
    if not 1 <= i < len(args):
        return VALUE
    return evaluate(args[i], ctx)


def _bools(a) -> List[bool]:
    out = []
    for x in a:
        if isinstance(x, (Rng, list)):
            for v in values_in(x):
                if isinstance(v, Err):
                    raise ErrExc(v)
                if isinstance(v, bool) or isinstance(v, (int, float)):
                    out.append(bool(v))
        else:
            out.append(to_bool(x))
    return out


@fn("AND")
def f_and(ctx, *a):
    xs = _bools(a)
    if not xs:
        raise ErrExc(VALUE)
    return all(xs)


@fn("OR")
def f_or(ctx, *a):
    xs = _bools(a)
    if not xs:
        raise ErrExc(VALUE)
    return any(xs)


@fn("XOR")
def f_xor(ctx, *a):
    return sum(_bools(a)) % 2 == 1


@fn("NOT")
def f_not(ctx, x):
    return not to_bool(x)


@fn("TRUE")
def f_true(ctx):
    return True


@fn("FALSE")
def f_false(ctx):
    return False


@fn("NA")
def f_na(ctx):
    return NA


@fn("ISBLANK")
def f_isblank(ctx, x):
    return x is None if not isinstance(x, (Rng, list)) else False


@fn("ISNUMBER")
def f_isnumber(ctx, x):
    x = scalar(x)
    return isinstance(x, (int, float)) and not isinstance(x, bool)


@fn("ISTEXT")
def f_istext(ctx, x):
    return isinstance(scalar(x), str)


@fn("ISLOGICAL")
def f_islogical(ctx, x):
    return isinstance(scalar(x), bool)


@fn("ISERROR")
def f_iserror(ctx, x):
    return isinstance(scalar(x), Err)


@fn("ISERR")
def f_iserr(ctx, x):
    x = scalar(x)
    return isinstance(x, Err) and x != NA


@fn("ISNA")
def f_isna(ctx, x):
    return scalar(x) == NA


@fn("ISEVEN")
def f_iseven(ctx, x):
    return int(math.floor(abs(to_num(x)))) % 2 == 0


@fn("ISODD")
def f_isodd(ctx, x):
    return int(math.floor(abs(to_num(x)))) % 2 == 1


@fn("N")
def f_n(ctx, x):
    x = scalar(x)
    if isinstance(x, Err):
        raise ErrExc(x)
    return x if isinstance(x, (int, float)) and not isinstance(x, bool) else (1 if x is True else 0)


@fn("T")
def f_t(ctx, x):
    x = scalar(x)
    return x if isinstance(x, str) else ""


# -- text -----------------------------------------------------------------------------
@fn("LEN")
def f_len(ctx, x):
    return len(to_str(x))


@fn("LEFT")
def f_left(ctx, x, n=1):
    k = _int_arg(n, 1)
    if k < 0:
        raise ErrExc(VALUE)
    return to_str(x)[:k]


@fn("RIGHT")
def f_right(ctx, x, n=1):
    k = _int_arg(n, 1)
    if k < 0:
        raise ErrExc(VALUE)
    s = to_str(x)
    return s[len(s) - k:] if k else ""


@fn("MID")
def f_mid(ctx, x, start, n):
    s, i, k = to_str(x), _int_arg(start), _int_arg(n)
    if i < 1 or k < 0:
        raise ErrExc(VALUE)
    return s[i - 1:i - 1 + k]


@fn("UPPER")
def f_upper(ctx, x):
    return to_str(x).upper()


@fn("LOWER")
def f_lower(ctx, x):
    return to_str(x).lower()


@fn("PROPER")
def f_proper(ctx, x):
    return re.sub(r"[A-Za-zÀ-ɏ]+", lambda m: m.group(0)[:1].upper() + m.group(0)[1:].lower(), to_str(x))


@fn("TRIM")
def f_trim(ctx, x):
    return re.sub(r" +", " ", to_str(x).strip(" "))


@fn("CLEAN")
def f_clean(ctx, x):
    return "".join(ch for ch in to_str(x) if ord(ch) >= 32)


@fn("CONCATENATE", "CONCAT")
def f_concat(ctx, *a):
    out = []
    for x in a:
        if isinstance(x, (Rng, list)):
            out.extend(to_str(v) for v in all_values(x) if v is not None)
        else:
            out.append(to_str(x))
    return "".join(out)


@fn("TEXTJOIN")
def f_textjoin(ctx, delim, skip_empty, *a):
    parts = []
    for x in a:
        for v in (all_values(x) if isinstance(x, (Rng, list)) else [x]):
            s = to_str(v)
            if s == "" and to_bool(skip_empty):
                continue
            parts.append(s)
    return to_str(delim).join(parts)


@fn("FIND")
def f_find(ctx, needle, text, start=1):
    n, t, s = to_str(needle), to_str(text), _int_arg(start, 1)
    i = t.find(n, s - 1)
    if s < 1 or i < 0:
        raise ErrExc(VALUE)
    return i + 1


@fn("SEARCH")
def f_search(ctx, needle, text, start=1):
    n, t, s = to_str(needle), to_str(text), _int_arg(start, 1)
    pat = re.compile("".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in n), re.I | re.S)
    m = pat.search(t, max(0, s - 1))
    if s < 1 or not m:
        raise ErrExc(VALUE)
    return m.start() + 1


@fn("SUBSTITUTE")
def f_substitute(ctx, text, old, new, nth=None):
    t, o, n = to_str(text), to_str(old), to_str(new)
    if o == "":
        return t
    if nth is None:
        return t.replace(o, n)
    k = _int_arg(nth)
    idx = -1
    for _ in range(k):
        idx = t.find(o, idx + 1)
        if idx < 0:
            return t
    return t[:idx] + n + t[idx + len(o):]


@fn("REPLACE")
def f_replace(ctx, text, start, n, new):
    t, i, k = to_str(text), _int_arg(start), _int_arg(n)
    if i < 1 or k < 0:
        raise ErrExc(VALUE)
    return t[:i - 1] + to_str(new) + t[i - 1 + k:]


@fn("REPT")
def f_rept(ctx, text, n):
    k = _int_arg(n)
    if k < 0 or k * max(1, len(to_str(text))) > 32767:
        raise ErrExc(VALUE)
    return to_str(text) * k


@fn("TEXT")
def f_text(ctx, v, fmt):
    v = scalar(v)
    if isinstance(v, Err):
        raise ErrExc(v)
    if isinstance(v, str) and _NUM_TEXT.match(v):
        v = float(v)
    return F.format_value(v, F.format_code(to_str(fmt)), "en")


@fn("VALUE")
def f_value(ctx, x):
    return _tidy(to_num(x)) if not isinstance(scalar(x), str) or scalar(x).strip() else 0


@fn("NUMBERVALUE")
def f_numbervalue(ctx, x, dec=".", grp=","):
    s = to_str(x).replace(to_str(grp), "").replace(to_str(dec), ".").replace(" ", "")
    return _tidy(to_num(s))


@fn("EXACT")
def f_exact(ctx, a, b):
    return to_str(a) == to_str(b)


@fn("CHAR")
def f_char(ctx, x):
    n = _int_arg(x)
    if not 1 <= n <= 255:
        raise ErrExc(VALUE)
    return chr(n)


@fn("CODE")
def f_code(ctx, x):
    s = to_str(x)
    if not s:
        raise ErrExc(VALUE)
    return ord(s[0])


# -- dates ----------------------------------------------------------------------------
def _d(v: Any) -> _dt.date:
    n = to_num(v)
    if n < 0:
        raise ErrExc(NUM)
    return F.date_from_serial(n)


@fn("TODAY")
def f_today(ctx):
    return F.serial_from_date(ctx.today())


@fn("NOW")
def f_now(ctx):
    return F.serial_from_datetime(ctx.now())


@fn("DATE")
def f_date(ctx, y, m, d):
    yy, mm, dd = _int_arg(y), _int_arg(m), _int_arg(d)
    if 0 <= yy < 1900:
        yy += 1900
    yy += (mm - 1) // 12
    mm = (mm - 1) % 12 + 1
    try:
        return F.serial_from_date(_dt.date(yy, mm, 1)) + dd - 1
    except ValueError:
        raise ErrExc(NUM) from None


@fn("DAY")
def f_day(ctx, x):
    return _d(x).day


@fn("MONTH")
def f_month(ctx, x):
    return _d(x).month


@fn("YEAR")
def f_year(ctx, x):
    return _d(x).year


@fn("WEEKDAY")
def f_weekday(ctx, x, kind=1):
    wd = _d(x).weekday()      # Monday = 0
    k = _int_arg(kind, 1)
    if k == 1:
        return (wd + 1) % 7 + 1
    if k == 2:
        return wd + 1
    if k == 3:
        return wd
    raise ErrExc(NUM)


@fn("WEEKNUM")
def f_weeknum(ctx, x, kind=1):
    d = _d(x)
    jan1 = _dt.date(d.year, 1, 1)
    start = (jan1.weekday() + 1) % 7 if _int_arg(kind, 1) == 1 else jan1.weekday()
    return (d.timetuple().tm_yday - 1 + start) // 7 + 1


def _add_months(d: _dt.date, k: int) -> _dt.date:
    y, m = divmod(d.month - 1 + k, 12)
    y += d.year
    m += 1
    last = (_dt.date(y + (m == 12), m % 12 + 1, 1) - _dt.timedelta(days=1)).day
    return _dt.date(y, m, min(d.day, last))


@fn("EDATE")
def f_edate(ctx, x, k):
    return F.serial_from_date(_add_months(_d(x), _int_arg(k)))


@fn("EOMONTH")
def f_eomonth(ctx, x, k):
    d = _add_months(_d(x).replace(day=1), _int_arg(k))
    return F.serial_from_date(_add_months(d, 1) - _dt.timedelta(days=1))


@fn("DAYS")
def f_days(ctx, end, start):
    return int(to_num(end)) - int(to_num(start))


@fn("DATEDIF")
def f_datedif(ctx, a, b, unit):
    d1, d2, u = _d(a), _d(b), to_str(unit).upper()
    if d2 < d1:
        raise ErrExc(NUM)
    months = (d2.year - d1.year) * 12 + d2.month - d1.month - (1 if d2.day < d1.day else 0)
    if u == "D":
        return (d2 - d1).days
    if u == "M":
        return months
    if u == "Y":
        return months // 12
    if u == "MD":
        return (d2 - _add_months(d1, months)).days
    if u == "YM":
        return months % 12
    if u == "YD":
        e = _add_months(d1, months // 12 * 12)
        return (d2 - e).days
    raise ErrExc(NUM)


def _holidays(h: Any) -> set:
    return {int(v) for v in numbers_in([h])} if h is not None else set()


@fn("NETWORKDAYS")
def f_networkdays(ctx, a, b, hol=None):
    s, e = int(to_num(a)), int(to_num(b))
    sign = 1 if e >= s else -1
    lo, hi = (s, e) if sign == 1 else (e, s)
    hs = _holidays(hol)
    n = sum(1 for k in range(lo, hi + 1) if F.date_from_serial(k).weekday() < 5 and k not in hs)
    return n * sign


@fn("WORKDAY")
def f_workday(ctx, a, k, hol=None):
    cur, left, hs = int(to_num(a)), _int_arg(k), _holidays(hol)
    step = 1 if left >= 0 else -1
    left = abs(left)
    while left:
        cur += step
        if F.date_from_serial(cur).weekday() < 5 and cur not in hs:
            left -= 1
    return cur


@fn("DATEVALUE")
def f_datevalue(ctx, x):
    d = F.parse_date(to_str(x))
    if d is None:
        raise ErrExc(VALUE)
    return d


@fn("HOUR")
def f_hour(ctx, x):
    return F.datetime_from_serial(to_num(x)).hour


@fn("MINUTE")
def f_minute(ctx, x):
    return F.datetime_from_serial(to_num(x)).minute


@fn("SECOND")
def f_second(ctx, x):
    return F.datetime_from_serial(to_num(x)).second


@fn("TIME")
def f_time(ctx, h, m, s):
    return _tidy(((_int_arg(h) * 3600 + _int_arg(m) * 60 + _int_arg(s)) % 86400) / 86400.0)


@fn("YEARFRAC")
def f_yearfrac(ctx, a, b, basis=0):
    d1, d2 = sorted((_d(a), _d(b)))
    if _int_arg(basis, 0) == 3:
        return (d2 - d1).days / 365.0
    days = (d2.year - d1.year) * 360 + (d2.month - d1.month) * 30 + (min(d2.day, 30) - min(d1.day, 30))
    return _tidy(days / 360.0)


# -- lookup -----------------------------------------------------------------------------
def _lookup_eq(a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        return a.lower() == b.lower()
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return a == b and type(a) == type(b)


def _match_pos(key: Any, seq: List[Any], mode: int) -> int:
    """0-based position or -1. mode 0 exact (wildcards for text), 1 largest <= key (sorted ascending), -1 smallest >= key."""
    key = scalar(key)
    if isinstance(key, Err):
        raise ErrExc(key)
    if mode == 0:
        if isinstance(key, str) and ("*" in key or "?" in key):
            pat = re.compile("^" + "".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in key) + "$", re.I | re.S)
            for i, v in enumerate(seq):
                if isinstance(v, str) and pat.match(v):
                    return i
            return -1
        for i, v in enumerate(seq):
            if _lookup_eq(key, v):
                return i
        return -1
    best = -1
    for i, v in enumerate(seq):
        if v is None or isinstance(v, Err) or type(v) != type(key) and not (isinstance(v, (int, float)) and isinstance(key, (int, float))):
            continue
        c = compare(v, key)
        if mode == 1 and c <= 0 and (best < 0 or compare(v, seq[best]) >= 0):
            best = i
        if mode == -1 and c >= 0 and (best < 0 or compare(v, seq[best]) <= 0):
            best = i
    return best


@fn("MATCH")
def f_match(ctx, key, rng, mode=1):
    seq = all_values(rng) if (rows_cols(rng)[0] == 1 or rows_cols(rng)[1] == 1) else None
    if seq is None:
        raise ErrExc(NA)
    i = _match_pos(key, seq, _int_arg(mode, 1))
    if i < 0:
        raise ErrExc(NA)
    return i + 1


def rows_cols(v: Any) -> Tuple[int, int]:
    m = to_matrix(v)
    return len(m), len(m[0]) if m else 0


@fn("VLOOKUP")
def f_vlookup(ctx, key, table, col, approx=True):
    m = to_matrix(table)
    c = _int_arg(col)
    if c < 1 or c > len(m[0]):
        raise ErrExc(REF)
    i = _match_pos(key, [row[0] for row in m], 1 if (approx is None or to_bool(approx)) else 0)
    if i < 0:
        raise ErrExc(NA)
    return m[i][c - 1]


@fn("HLOOKUP")
def f_hlookup(ctx, key, table, row, approx=True):
    m = to_matrix(table)
    r = _int_arg(row)
    if r < 1 or r > len(m):
        raise ErrExc(REF)
    i = _match_pos(key, m[0], 1 if (approx is None or to_bool(approx)) else 0)
    if i < 0:
        raise ErrExc(NA)
    return m[r - 1][i]


@fn("XLOOKUP")
def f_xlookup(ctx, key, look, ret, if_missing=None, match_mode=0, *rest):
    lv = all_values(look)
    mm = _int_arg(match_mode, 0)
    i = _match_pos(key, lv, 0 if mm == 0 else (1 if mm == -1 else -1) if mm in (-1, 1) else 0)
    if i < 0:
        if if_missing is not None:
            return if_missing
        raise ErrExc(NA)
    rm = to_matrix(ret)
    if len(rm) == 1 and len(rm[0]) == len(lv):
        return rm[0][i]
    return rm[i][0] if len(rm[0]) == 1 else rm[i]


@fn("LOOKUP")
def f_lookup(ctx, key, look, ret=None):
    lv = all_values(look)
    i = _match_pos(key, lv, 1)
    if i < 0:
        raise ErrExc(NA)
    rv = all_values(ret) if ret is not None else lv
    return rv[i]


@fn("INDEX")
def f_index(ctx, rng, row=None, col=None):
    m = to_matrix(rng)
    r = _int_arg(row, 0) if row is not None else 0
    c = _int_arg(col, 0) if col is not None else 0
    if len(m) == 1 and col is None and row is not None:
        r, c = 0, r
    if r > len(m) or c > len(m[0]) or r < 0 or c < 0:
        raise ErrExc(REF)
    if r == 0 and c == 0:
        return m
    if r == 0:
        return [[row_[c - 1]] for row_ in m]
    if c == 0:
        return [m[r - 1]]
    return m[r - 1][c - 1]


@fn("ROWS")
def f_rows(ctx, x):
    return rows_cols(x)[0]


@fn("COLUMNS")
def f_columns(ctx, x):
    return rows_cols(x)[1]


@fn("ROW", lazy=True)
def f_row(ctx, args):
    if not args:
        return ctx.cur_pos[0]
    n = args[0]
    return n[2] if n[0] == "ref" else VALUE


@fn("COLUMN", lazy=True)
def f_column(ctx, args):
    if not args:
        return ctx.cur_pos[1]
    n = args[0]
    return n[3] if n[0] == "ref" else VALUE


@fn("INDIRECT")
def f_indirect(ctx, text, a1=True):
    t = to_str(text)
    sheet = None
    if "!" in t:
        sh, t = t.rsplit("!", 1)
        sheet = _unquote_sheet(sh)
    tab = ctx.resolve_tab(sheet) if sheet else ctx.cur_tab
    pr = parse_range(t)
    if tab is None or pr is None:
        raise ErrExc(REF)
    r0, c0, r1, c1 = pr
    return ctx.cell(tab, r0, c0) if (r0, c0) == (r1, c1) else Rng(ctx, tab, r0, c0, r1, c1)


# -- financial ----------------------------------------------------------------------------
@fn("PMT")
def f_pmt(ctx, rate, nper, pv, fv=0, typ=0):
    r, n, p, f, t = to_num(rate), to_num(nper), to_num(pv), to_num(fv or 0), to_num(typ or 0)
    if n == 0:
        raise ErrExc(NUM)
    if r == 0:
        return _tidy(-(p + f) / n)
    k = (1 + r) ** n
    return _tidy(-(r * (p * k + f)) / ((1 + r * t) * (k - 1)))


@fn("FV")
def f_fv(ctx, rate, nper, pmt, pv=0, typ=0):
    r, n, p, v, t = to_num(rate), to_num(nper), to_num(pmt), to_num(pv or 0), to_num(typ or 0)
    if r == 0:
        return _tidy(-(v + p * n))
    k = (1 + r) ** n
    return _tidy(-(v * k + p * (1 + r * t) * (k - 1) / r))


@fn("PV")
def f_pv(ctx, rate, nper, pmt, fv=0, typ=0):
    r, n, p, f, t = to_num(rate), to_num(nper), to_num(pmt), to_num(fv or 0), to_num(typ or 0)
    if r == 0:
        return _tidy(-(f + p * n))
    k = (1 + r) ** n
    return _tidy(-(f + p * (1 + r * t) * (k - 1) / r) / k)


@fn("NPER")
def f_nper(ctx, rate, pmt, pv, fv=0, typ=0):
    r, p, v, f, t = to_num(rate), to_num(pmt), to_num(pv), to_num(fv or 0), to_num(typ or 0)
    if r == 0:
        if p == 0:
            raise ErrExc(NUM)
        return _tidy(-(v + f) / p)
    a = p * (1 + r * t) - f * r
    b = p * (1 + r * t) + v * r
    if a <= 0 or b <= 0 or a / b <= 0:
        raise ErrExc(NUM)
    return _tidy(math.log(a / b) / math.log(1 + r))


@fn("NPV")
def f_npv(ctx, rate, *a):
    r = to_num(rate)
    xs = numbers_in(a)
    return _tidy(sum(x / (1 + r) ** (i + 1) for i, x in enumerate(xs)))


def _solve(fx, x0: float, x1: float = None) -> float:
    for guess in (x0, 0.05, 0.2, -0.2, 0.5):
        x = guess
        for _ in range(100):
            y = fx(x)
            h = 1e-7
            d = (fx(x + h) - y) / h
            if d == 0:
                break
            nx = x - y / d
            if abs(nx - x) < 1e-12:
                return nx
            x = nx
            if abs(x) > 1e6:
                break
    raise ErrExc(NUM)


@fn("IRR")
def f_irr(ctx, vals, guess=0.1):
    xs = numbers_in([vals])
    if not (any(x > 0 for x in xs) and any(x < 0 for x in xs)):
        raise ErrExc(NUM)
    return _tidy(_solve(lambda r: sum(x / (1 + r) ** i for i, x in enumerate(xs)), to_num(guess or 0.1)))


@fn("RATE")
def f_rate(ctx, nper, pmt, pv, fv=0, typ=0, guess=0.1):
    n, p, v, f, t = to_num(nper), to_num(pmt), to_num(pv), to_num(fv or 0), to_num(typ or 0)
    def fx(r):
        if abs(r) < 1e-12:
            return v + p * n + f
        k = (1 + r) ** n
        return v * k + p * (1 + r * t) * (k - 1) / r + f
    return _tidy(_solve(fx, to_num(guess or 0.1)))


@fn("IPMT")
def f_ipmt(ctx, rate, per, nper, pv, fv=0, typ=0):
    r, k, n = to_num(rate), _int_arg(per), to_num(nper)
    pmt = f_pmt(ctx, rate, nper, pv, fv, typ)
    bal = to_num(pv)
    for _ in range(1, k):
        bal = bal * (1 + r) + pmt
    return _tidy(-(bal * r))


@fn("SLN")
def f_sln(ctx, cost, salvage, life):
    n = to_num(life)
    if n == 0:
        raise ErrExc(DIV0)
    return _tidy((to_num(cost) - to_num(salvage)) / n)


# ---------------------------------------------------------------------------
class Ctx:
    """Base evaluation context; the workbook subclasses it."""
    cur_tab: str = ""
    cur_pos: Tuple[int, int] = (1, 1)

    def cell(self, tab: str, r: int, c: int) -> Any: ...
    def bounds(self, tab: str) -> Tuple[int, int]: ...
    def iter_range(self, tab: str, r0: int, c0: int, r1: int, c1: int) -> Iterator[Tuple[int, int, Any]]: ...
    def resolve_tab(self, name: str) -> Optional[str]: ...

    def today(self) -> _dt.date:
        return _dt.date.today()

    def now(self) -> _dt.datetime:
        return _dt.datetime.now()

    def random(self) -> float:
        import random
        return random.random()


def function_names() -> List[str]:
    return sorted(set(FUNCS) | set(LAZY))
