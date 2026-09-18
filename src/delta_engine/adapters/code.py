"""The `code` domain: files, symbols, imports, dependencies and configuration. §9.

An adapter turns a revision into addressable elements and says what differs
between two of them. For code the addressing is the layered one of §9 -- a file
is a path, a symbol is `path#qualified name`, an import is `path#module`, a
dependency is `manifest#name`, a configuration key is `file#dotted key` -- and
the whole value of the layering is that it distinguishes a rename from a
deletion, a body edit from a signature break, and a new dependency from a new
line of prose.

Six rules, each with the failure it prevents:

* **A rename is one observation, not two.** A file whose content hash is
  unchanged at a new path is `moved`. Reported as `missing` + `added` it reads
  as data loss plus unreviewed new code, which is the §28 row "rename/move
  frente a delete+add" and the first mandatory test of this module.

* **`unchanged` is emitted, never omitted.** A file whose hash did not move is
  a real observation at tier `hash`, and it is the only thing that lets a later
  layer say `preserved` with anything behind it. Without those rows the honest
  answer about every untouched file is `unknown`.

* **A regex over line starts is not a parser.** Python symbols come from `ast`
  and carry tier `parser`; every other language comes from
  `repo_map.symbol_lines`, which anchors regexes on line starts, and those
  findings carry tier `algorithm` with `certainty="lexical"` on the element.
  The ladder in §3.2 is a claim about determinism, and a language whose symbols
  were found by pattern matching must not be able to claim identity.

* **A file that does not parse stays in the result, labelled.** Its findings
  carry the `parser_degraded` limitation and drop to tier `algorithm`: the
  bytes still compare, but a comparison that could not read the file's
  structure must not read as a complete one. A degraded file that vanished
  from the output would look exactly like a file nobody touched.

* **The security scan is lexical and says so.** A new import of `subprocess`
  or a new call to `os.system` is a real observation about the TEXT, at tier
  `algorithm` and confidence `medium` at the very most. It is not a proof that
  the code runs, or that the call is reachable, and it is never `exact`.

* **An invariant this adapter cannot check is `unknown`.** `preserved` costs an
  observation (`InvariantResult.parse` refuses it without one), so a property
  no method here measures -- a secret scan, a performance budget -- comes back
  `unknown` with the limitation named, never `preserved`.

Two rules from §9 are written into the comments at the places they apply, since
they are the ones an adapter is most tempted to break: a green test does not
prove the absence of collateral change (see `_coverage`), and behaviour is
never inferred from the name of a function (see `_symbol_findings`).

`registry.py` finds this module by the module-level `ADAPTER_FACTORY` at the
bottom. There is no list to add it to, which is why it cannot be finished and
invisible at the same time.
"""

from __future__ import annotations

import ast
import configparser
import hashlib
import json
import logging
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.context_engine.code_index import CERTAINTY, SYMBOL_KINDS
from src.delta_engine import alignment as alignment_mod
from src.delta_engine import confidence as confidence_mod
from src.delta_engine import coverage as coverage_mod
from src.delta_engine import sources
from src.delta_engine.adapters.base import (
    AdapterError,
    Element,
    Extraction,
    Finding,
    Scope,
    Snapshot,
    unreadable,
)
from src.delta_engine.contracts import (
    Coverage,
    IntentContract,
    InvariantResult,
    RevisionRef,
)
from src.index_walk import is_indexable_file, prune_index_dirs
from src.repo_map import lang_for_path, symbol_lines

logger = logging.getLogger(__name__)

__all__ = [
    "CodeAdapter",
    "ADAPTER_FACTORY",
    "CODE_TREE_MEDIA_TYPE",
    "COMPATIBILITY_INVARIANT",
    "PERMISSIONS_INVARIANT",
    "NETWORK_INVARIANT",
    "SECRETS_INVARIANT",
    "BUDGET_INVARIANT",
    "EXECUTION_SENSITIVE",
    "NETWORK_SENSITIVE",
    "PARSER_DEGRADED",
]


# -- the invariants this adapter can be asked about ------------------------
#
# The ids are the ones `invariants.DOMAIN_DEFAULTS["code"]` and
# `invariants.SECURITY_INVARIANTS` already declare. They are written here as
# constants rather than reached out of those tuples by index, because an index
# is a second thing to keep in step and the tuples are ordered for reading;
# `tests/test_delta_engine_code.py` asserts that every id below is one the
# catalogue actually defines, so a rename over there fails a test here instead
# of quietly producing findings that point at an invariant nobody declared.

#: §9's compatibility property: a public symbol withdrawn, or a signature
#: changed, breaks callers this comparison cannot see.
COMPATIBILITY_INVARIANT = "code.public_symbols_kept"

#: What the code may DO. A new `subprocess`, `pickle`, `eval`, `exec` or
#: `os.system` widens execution and deserialisation reach.
PERMISSIONS_INVARIANT = "security.permissions_not_widened"

#: What the code may REACH. A new `socket`, `requests`, `httpx` or `urllib`
#: widens network reach.
NETWORK_INVARIANT = "security.network_not_widened"

#: Named so it can be refused by name. This adapter does not scan for
#: credentials, and answering `preserved` about secrets because an import scan
#: found nothing would be the exact failure rule 1 of `contracts.py` exists to
#: prevent.
SECRETS_INVARIANT = "security.secrets_not_exposed"

#: The comparison's own budget. Checkable here because it is a fact about this
#: run rather than about the code: either a snapshot was cut or it was not.
BUDGET_INVARIANT = "code.comparison_budget"

#: Tokens whose FIRST appearance widens what the code can do to the machine.
#: `os.system`, `eval` and `exec` are call names; the rest are module roots, so
#: `subprocess.run`, `from subprocess import run` and `import subprocess.x` all
#: match the same token and produce one observation instead of three.
EXECUTION_SENSITIVE: Tuple[str, ...] = ("subprocess", "pickle", "os.system", "eval", "exec")

#: Tokens whose first appearance widens what the code can reach.
NETWORK_SENSITIVE: Tuple[str, ...] = ("socket", "requests", "httpx", "urllib")

#: Both, for an invariant of class `security` or `permissions` whose id this
#: module does not recognise: a violation found by a real observation is still
#: worth reporting, and only `preserved` is withheld from an id we cannot name.
SENSITIVE_TOKENS: Tuple[str, ...] = EXECUTION_SENSITIVE + NETWORK_SENSITIVE

#: Which invariant id this adapter's import-and-call scan actually answers,
#: and with which tokens. An id that is NOT in here gets a violation reported
#: when the scan sees one -- a violation costs an observation and over-reporting
#: one is the safe direction -- but never a `preserved`, because saying a
#: property was held still requires knowing which property was checked.
#: `security.secrets_not_exposed` is deliberately absent: no credential scan
#: runs here, and answering `preserved` about secrets because no import moved
#: would be the exact failure rule 1 of `contracts.py` exists to prevent.
_CHECKED_BY_IMPORT_SCAN: Dict[str, Tuple[str, ...]] = {
    PERMISSIONS_INVARIANT: EXECUTION_SENSITIVE,
    NETWORK_INVARIANT: NETWORK_SENSITIVE,
}

#: Module roots among the sensitive tokens -- what an import is matched on.
_SENSITIVE_MODULES = frozenset(
    token for token in SENSITIVE_TOKENS if "." not in token and token not in ("eval", "exec")
)

#: Call names among them: a bare `eval(...)`/`exec(...)` and the dotted
#: `os.system(...)`. `os` itself is not sensitive -- `os.path.join` is not a
#: shell -- so the call name is matched whole.
_SENSITIVE_CALLS = frozenset(("eval", "exec", "os.system"))

def _widening_invariants(layer: str, name: str) -> Tuple[str, ...]:
    """The security invariants a newly added element is evidence about.

    This function is a JOIN, and it exists because the two halves of this
    adapter were correct and did not reach each other. `check_invariants`
    reported `security.permissions_not_widened: violated, blocking` for a new
    `import subprocess`, and `compare` emitted the import as a bare `added`
    with no `invariant_refs` -- so `classification._violations_for` could not
    connect them (the invariant declares no path, and the finding named no
    invariant), and the row that CAUSED the blocking violation was filed
    `incidental / info`. The delta said both things and its assertions table
    showed a shrug.

    The link belongs here rather than in the classifier: this module is the one
    that knows an import of `subprocess` is a widening, and the classifier is
    deliberately blind to what a path MEANS in any particular domain.

    Only additions. A sensitive module that was already imported and moved
    elsewhere is not a widening -- the same rule `check_invariants` applies by
    comparing token SETS -- and marking it as one would train a reader to
    scroll past the row that matters.
    """
    if layer != "import" or not name:
        return ()
    root = str(name).split(".", 1)[0].strip()
    if not root:
        return ()
    return tuple(
        invariant_id
        for invariant_id, tokens in _CHECKED_BY_IMPORT_SCAN.items()
        if root in tokens or str(name) in tokens
    )


#: The prefix every parser-degradation limitation starts with, so a caller can
#: recognise one without matching a sentence that will be reworded. §28 asks
#: for a "parser degradado etiquetado" and this is the label.
PARSER_DEGRADED = "parser_degraded"

#: The media type a caller stashes a whole tree under: a JSON object mapping
#: relative path -> source text. A plain `application/json` literal is ONE
#: document, and guessing between the two shapes would turn the config file
#: `{"a": "b"}` into a source tree holding a file called `a` -- so the caller
#: says which it meant, with `sources.stash(payload, media_type=...)`, and this
#: module never sniffs.
CODE_TREE_MEDIA_TYPE = "application/vnd.odysseus.code-tree+json"

#: Manifests whose dependency lists become `dependency:` elements. Matched on
#: the base name, since a monorepo has one per package.
REQUIREMENTS_FILE = "requirements.txt"
PYPROJECT_FILE = "pyproject.toml"
PACKAGE_JSON_FILE = "package.json"
DEPENDENCY_FILES: Tuple[str, ...] = (REQUIREMENTS_FILE, PYPROJECT_FILE, PACKAGE_JSON_FILE)

#: Suffixes read as configuration, flattened with dots. Kept to the three the
#: plan names: a format nobody parses here would produce `config:` keys out of
#: a guess about its grammar.
CONFIG_SUFFIXES: Tuple[str, ...] = (".json", ".toml", ".ini", ".cfg")

#: Dotted prefixes inside a manifest that ALREADY became `dependency:`
#: elements. Skipped by the configuration layer so that one fact -- httpx moved
#: from 0.27 to 0.28 -- has one address, and a reader is not asked to reconcile
#: two findings that are the same change.
_DEPENDENCY_CONFIG_PREFIXES: Dict[str, Tuple[str, ...]] = {
    PYPROJECT_FILE: ("project.dependencies", "project.optional-dependencies",
                     "tool.poetry.dependencies", "tool.poetry.dev-dependencies"),
    PACKAGE_JSON_FILE: ("dependencies", "devDependencies", "peerDependencies"),
}

#: How many element keys a coverage lists. `Coverage.regions_analyzed` accepts
#: 1024; the cut is made below it and REPORTED, because a list silently clipped
#: at the contract's ceiling reads as the complete set of regions examined.
MAX_LISTED_REGIONS = 512

#: How long a rendered value may be. `DeltaAssertion.parse` accepts 2000 for
#: `before`/`after`, and `Element.value` is documented as a SHORT rendering: a
#: signature, not a body. A value long enough to hold a function body would
#: make a snapshot as expensive as the revision it describes.
MAX_VALUE_CHARS = 300

#: `checkpoint:<sha>` -- the WHOLE checkpoint. `sources.CHECKPOINT_REF` owns
#: the single-file spelling (`checkpoint:<sha>#<path>`) and this pattern
#: deliberately does not match it, so a ref carrying a path still goes through
#: the resolver that knows how to read one file.
CHECKPOINT_TREE_REF = re.compile(r"^checkpoint:(?P<sha>[0-9a-fA-F]{4,64})$")

#: A literal with no usable name is compared under this one. Both ends get the
#: same address, so two stashed strings align by key instead of reading as a
#: deletion plus an addition; a caller that cares about the real path sets
#: `RevisionRef.label` to it.
LITERAL_PATH = "literal"


# -- small helpers ---------------------------------------------------------


def _sha(raw: bytes) -> str:
    """sha256 of some bytes. One spelling, used by every layer here."""
    return hashlib.sha256(raw).hexdigest()


def _text_sha(text: str) -> str:
    """sha256 of a symbol's own source, as UTF-8.

    The symbol's bytes and not the file's: a file whose imports changed leaves
    every function hash untouched, which is what turns "the file changed" into
    "these two functions changed and the other forty did not".
    """
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _short(value: Any) -> str:
    """A rendered value, cut to `MAX_VALUE_CHARS` and saying when it was cut."""
    text = " ".join(str(value or "").split())
    if len(text) <= MAX_VALUE_CHARS:
        return text
    return text[:MAX_VALUE_CHARS - 1] + "…"


def _unique(values: Iterable[Any]) -> Tuple[str, ...]:
    """Order-preserving de-duplication for lists assembled HERE.

    Same rule as `coverage._unique`: `Coverage.parse` refuses duplicates, and
    the repeats this removes are the ones produced by joining two snapshots'
    notes -- both ends cut by the same budget produce the same sentence twice.
    A duplicate inside one snapshot's own list is still that snapshot's
    contradiction and is still refused.
    """
    seen = set()
    out: List[str] = []
    for value in values:
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return tuple(out)


def _rel_path(path: str) -> str:
    """A relative path with forward slashes, whatever the platform wrote.

    `src/auth.py` on both sides or a rename detector on Windows would pair
    nothing: `src\\auth.py` and `src/auth.py` are two addresses for one file,
    and alignment matches on the address.
    """
    return str(path or "").replace("\\", "/").strip("/")


def _decode(raw: bytes) -> Optional[str]:
    """The bytes as text, or `None` when they are not text.

    Strict UTF-8 and a NUL check, the same pair `generic._as_text` uses:
    `errors="replace"` would hand a JPEG to `ast.parse` and file the resulting
    SyntaxError as a degraded parser, which is a sentence about a file that was
    never source code.
    """
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8", "strict")
    except UnicodeDecodeError:
        return None


def _is_public(name: str) -> bool:
    """Whether a qualified symbol name is part of the public surface.

    Every segment has to be public: `_Helper.run` is not public, because the
    only way to reach it is through a name its author marked private, and
    reporting its removal as a compatibility break would bury the removals that
    really do break a caller.
    """
    parts = [part for part in str(name or "").split(".") if part]
    return bool(parts) and all(not part.startswith("_") for part in parts)


def _key_parts(key: str) -> Tuple[str, str, str]:
    """`(layer, path, name)` out of an element key.

    One reader for the five key shapes this adapter emits, so that a `compare`
    branch cannot disagree with a `snapshot` branch about where the path ends
    and the symbol begins -- which would silently mark every symbol as
    belonging to a file called `src/auth.py#consume_state`.
    """
    layer, _, rest = str(key or "").partition(":")
    path, _, name = rest.partition("#")
    return layer, path, name


def _dotted(node: ast.AST) -> str:
    """The dotted name a call points at, or `""` for anything else.

    `os.system` and `subprocess.run` are `Attribute` chains and `eval` is a
    `Name`; a call on the result of another call (`get_module().system`) has no
    static name and gets `""` rather than a guess, because the guess would be
    an assertion about which function runs.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def _sensitive_token(name: str, *, kind: str) -> str:
    """The sensitive token `name` matches, or `""`.

    `kind="import"` matches on the module root, so `urllib.request` and
    `urllib` are one token; `kind="call"` matches the whole dotted name against
    `_SENSITIVE_CALLS` first (`os.system` is sensitive, `os.path.join` is not)
    and then the root against the sensitive modules (`pickle.loads`).
    """
    clean = str(name or "").strip().lstrip(".")
    if not clean:
        return ""
    root = clean.split(".")[0]
    if kind == "call" and clean in _SENSITIVE_CALLS:
        return clean
    if root in _SENSITIVE_MODULES:
        return root
    return ""


def _kind(name: str) -> str:
    """One symbol kind, taken from `code_index.SYMBOL_KINDS` and not respelled.

    Looked up rather than written as a literal because the structural index
    owns this vocabulary: an adapter that emitted `func` beside that index's
    `function` would make two descriptions of one tree incomparable, for a
    reason nobody would find by reading either of them. A word this repository
    does not know is refused HERE, where the cause is one line away, instead of
    travelling into a snapshot and becoming a string in a database.

    Four of the seven kinds are reachable from this adapter. `module`, `route`
    and `tool` need the decorator analysis `code_index` does and this module
    does not, and claiming one of them from a symbol table would be a guess
    about what a function is FOR -- §9's rule against inferring behaviour from
    a name, arriving through the kind instead of through the finding.
    """
    if name not in SYMBOL_KINDS:
        raise AdapterError(
            "element.kind",
            f"is not one of `code_index.SYMBOL_KINDS` ({list(SYMBOL_KINDS)}); "
            f"a second vocabulary for the same things makes two indexes of one "
            f"tree incomparable",
            got=name,
        )
    return name


def _signature_key(element: Element) -> str:
    """What makes two elements the same element: the hash, else the value.

    Hash first because a digest is identity and a rendered value is a
    description. A file whose `value` is "1200 bytes" on both sides has not
    been shown to be unchanged; its hash has to say so.
    """
    return element.hash or element.value


def _render(element: Element) -> str:
    """A short `before`/`after` for one element.

    A file renders as a truncated digest rather than as its size, because two
    files of the same size are the common case and "1200 bytes -> 1200 bytes"
    would read as a row where nothing happened. Everything else renders as its
    value -- a signature, a version constraint, a configuration scalar -- which
    is the thing a reader is trying to compare.
    """
    layer, _path, _name = _key_parts(element.key)
    if layer == "file" and element.hash:
        return f"sha256:{element.hash[:12]}"
    return _short(element.value or (f"sha256:{element.hash[:12]}" if element.hash else ""))


# -- what one file yields --------------------------------------------------


@dataclass(frozen=True)
class _FileRead:
    """Everything one file contributed, including what it could not contribute.

    `parsed` has three values on purpose. `True` means an AST read the file,
    `False` means it is source in a language with an AST here and the AST
    refused it, and `None` means no AST was attempted -- a Go file, a README, a
    PNG. Folding the third into the second would count every non-Python file as
    a parse failure and make the semantic coverage of a JavaScript repository
    read as zero because of a parser nobody claimed to have.
    """

    elements: Tuple[Element, ...] = ()
    risk: Mapping[str, Tuple[str, ...]] = field(default_factory=dict)
    parsed: Optional[bool] = None
    reason: str = ""
    is_code: bool = False
    binary: bool = False


def _render_arguments(args: ast.arguments) -> str:
    """A Python argument list, rendered from the AST and not from the source.

    From the AST because the point of a signature element is that two
    signatures compare equal when they mean the same thing: re-indenting a
    long parameter list across three lines, or reflowing it onto one, is not an
    API change and must not read as one. `ast.unparse` is used on the
    annotations and defaults only, where it is defined; the argument list
    itself is assembled here because `unparse` renders an `arguments` node only
    as part of the function that owns it.
    """
    parts: List[str] = []
    positional = list(args.posonlyargs) + list(args.args)
    defaults = list(args.defaults)
    first_default = len(positional) - len(defaults)
    for index, arg in enumerate(positional):
        rendered = arg.arg
        if arg.annotation is not None:
            rendered += f": {ast.unparse(arg.annotation)}"
        if index >= first_default:
            rendered += f"={ast.unparse(defaults[index - first_default])}"
        parts.append(rendered)
        if args.posonlyargs and index == len(args.posonlyargs) - 1:
            parts.append("/")
    if args.vararg is not None:
        star = f"*{args.vararg.arg}"
        if args.vararg.annotation is not None:
            star += f": {ast.unparse(args.vararg.annotation)}"
        parts.append(star)
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        rendered = arg.arg
        if arg.annotation is not None:
            rendered += f": {ast.unparse(arg.annotation)}"
        if default is not None:
            rendered += f"={ast.unparse(default)}"
        parts.append(rendered)
    if args.kwarg is not None:
        rendered = f"**{args.kwarg.arg}"
        if args.kwarg.annotation is not None:
            rendered += f": {ast.unparse(args.kwarg.annotation)}"
        parts.append(rendered)
    return ", ".join(parts)


def _signature_of(node: ast.AST) -> str:
    """The signature an element carries as its `value`. §9's "firmas".

    This is a rendering of what the caller has to satisfy -- name, parameters,
    defaults, annotations, bases -- and deliberately nothing about the body.
    §9: behaviour is not inferred from the name of a function, and a signature
    is not evidence about what the function does; it is evidence about what
    breaks when it changes.
    """
    if isinstance(node, ast.ClassDef):
        bases = [ast.unparse(base) for base in node.bases]
        bases += [f"{kw.arg}={ast.unparse(kw.value)}" for kw in node.keywords if kw.arg]
        joined = ", ".join(bases)
        return f"class {node.name}({joined})" if joined else f"class {node.name}"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
        return f"{prefix} {node.name}({_render_arguments(node.args)}){returns}"
    return ""


def _segment(text: str, node: ast.AST) -> str:
    """The symbol's own source. Falls back to a line slice, never to the file.

    `ast.get_source_segment` answers `None` when a node carries no end
    position, and returning the whole file there would give every symbol in it
    the same hash -- forty functions that all "changed" the moment one of them
    did.
    """
    try:
        found = ast.get_source_segment(text, node)
    except (TypeError, ValueError, IndexError):
        found = None
    if found is not None:
        return found
    start = max(0, int(getattr(node, "lineno", 1)) - 1)
    end = int(getattr(node, "end_lineno", getattr(node, "lineno", 1)) or start + 1)
    return "\n".join(text.split("\n")[start:end])


# -- layer 2 and 3: symbols and imports ------------------------------------


def _python_file(rel: str, text: str) -> _FileRead:
    """Symbols, imports and risky names of one Python file, via `ast`.

    A `SyntaxError` is a RESULT: the file comes back with `parsed=False` and a
    reason, its symbols are absent and the absence is reported as a degraded
    parser rather than as a file with no functions in it. Those two are
    opposite facts and only one of them is about the code.

    The symbol kinds are `code_index.SYMBOL_KINDS` (`class`, `function`,
    `method`, `constant`) rather than a private vocabulary, so a reader who
    knows the structural index does not have to learn a second set of words for
    the same things.
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError) as exc:
        return _FileRead(parsed=False, is_code=True,
                         reason=f"{type(exc).__name__}: {exc}")

    elements: List[Element] = []
    risk: Dict[str, List[str]] = {}

    def _add_symbol(name: str, kind: str, value: str, node: ast.AST) -> None:
        elements.append(Element(
            key=f"symbol:{rel}#{name}",
            kind=_kind(kind),
            hash=_text_sha(_segment(text, node)),
            value=_short(value),
            # `CERTAINTY[0]` is `exact`: the AST said so. The word is
            # `code_index`'s, not a new one, so the two indexes of this
            # repository grade their evidence on one scale.
            detail={"line": int(getattr(node, "lineno", 0) or 0),
                    "certainty": CERTAINTY[0], "language": "py"},
        ))

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            _add_symbol(node.name, "class", _signature_of(node), node)
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    _add_symbol(f"{node.name}.{child.name}", "method",
                                _signature_of(child), child)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _add_symbol(node.name, "function", _signature_of(node), node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    _add_symbol(target.id, "constant",
                                f"{target.id} = {ast.unparse(node.value)}", node)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                elements.append(_import_element(rel, alias.name))
                _note_risk(risk, _sensitive_token(alias.name, kind="import"), rel)
        elif isinstance(node, ast.ImportFrom):
            module = "." * int(node.level or 0) + (node.module or "")
            elements.append(_import_element(rel, module))
            _note_risk(risk, _sensitive_token(module, kind="import"), rel)
        elif isinstance(node, ast.Call):
            _note_risk(risk, _sensitive_token(_dotted(node.func), kind="call"), rel)

    return _FileRead(elements=tuple(elements),
                     risk={token: tuple(_unique(where)) for token, where in risk.items()},
                     parsed=True, is_code=True)


def _import_element(rel: str, module: str) -> Element:
    """One `import:<path>#<module>` element -- and deliberately no hash.

    Hashless because `alignment.by_hash` groups on the digest, and giving every
    `import os` in the tree the same digest would make the forty of them a
    bucket of identical candidates: forty `uncertain` rows about imports that
    never moved. An import's identity is its address, and by_key is what
    compares addresses.
    """
    return Element(key=f"import:{rel}#{module}", kind="import", value=module)


def _note_risk(risk: Dict[str, List[str]], token: str, rel: str) -> None:
    """Record that `rel` carries `token`. Empty tokens are dropped, not stored."""
    if not token:
        return
    risk.setdefault(token, []).append(rel)


def _lexical_file(rel: str, text: str, lang: str) -> _FileRead:
    """Symbols of a non-Python file, via `repo_map.symbol_lines`.

    `symbol_lines` anchors regexes on line starts, which finds the definitions
    and nothing about what encloses them, so every element here is marked
    `certainty="lexical"` and every finding built on one is tier `algorithm`.
    A regex over line starts is not a parser and the confidence has to say so.

    A symbol's bytes are taken as the lines from its own start to the line
    before the next symbol starts. That is an approximation -- a trailing
    comment or a blank line moves with the symbol above it -- and it is why
    these findings can be `high` at best: the boundary was inferred, not read.
    """
    found = symbol_lines(text, lang)
    if not found:
        return _FileRead(is_code=True, parsed=None)
    lines = text.split("\n")
    starts = sorted({int(line) for _name, line in found} | {len(lines) + 1})
    elements: List[Element] = []
    for name, line in found:
        following = next((start for start in starts if start > line), len(lines) + 1)
        body = "\n".join(lines[max(0, line - 1):following - 1])
        elements.append(Element(
            key=f"symbol:{rel}#{name}",
            # `function` and not a guess between function and class: the regex
            # extractors return a name and a line and nothing that separates
            # the two, and inventing the distinction here would put a word in
            # the element that no extractor observed.
            kind=_kind("function"),
            hash=_text_sha(body),
            # The declaration line is the closest thing to a signature a
            # line-start regex can see, and it is what a reader compares when
            # the parameters change. It is not a parsed signature and the
            # element's `certainty` says which of the two this is.
            value=_short(lines[line - 1] if 0 < line <= len(lines) else name),
            detail={"line": int(line), "certainty": CERTAINTY[2], "language": lang},
        ))
    return _FileRead(elements=tuple(elements), parsed=None, is_code=True)


# -- layer 4 and 5: dependencies and configuration -------------------------

#: Where a requirement's name ends and its version constraint begins.
_REQUIREMENT_SPLIT = re.compile(r"[\s<>=!~;\[\(]")


def _dependency_elements(rel: str, text: str) -> Tuple[Tuple[Element, ...], str]:
    """`dependency:<manifest>#<name>` elements, and the reason none were read.

    Three manifests, three real parsers (`tomllib`, `json`, and a line reader
    for `requirements.txt`), and no guessing between them: the base name of the
    file decides, because a `pyproject.toml` parsed as JSON would produce
    nothing and look exactly like a project with no dependencies.

    A manifest that does not parse returns its reason. That reason becomes a
    `parser_degraded` limitation on the findings about it, for the same purpose
    as a Python file that does not compile: the dependencies it declares were
    not read, and an empty list must not be mistaken for none.
    """
    name = os.path.basename(rel).lower()
    try:
        if name == REQUIREMENTS_FILE:
            found = _requirements(text)
        elif name == PYPROJECT_FILE:
            found = _pyproject(text)
        elif name == PACKAGE_JSON_FILE:
            found = _package_json(text)
        else:
            return (), ""
    except (ValueError, TypeError, tomllib.TOMLDecodeError) as exc:
        return (), f"{type(exc).__name__}: {exc}"
    return tuple(
        Element(key=f"dependency:{rel}#{key}", kind="dependency", value=_short(spec))
        for key, spec in sorted(found.items())
    ), ""


def _requirements(text: str) -> Dict[str, str]:
    """`requirements.txt`: one requirement per line, options ignored.

    Lines starting with `-` are pip options (`-r base.txt`, `--index-url`) and
    are not dependencies; treating one as a package called `-r` would put a
    finding in the delta about a flag.
    """
    out: Dict[str, str] = {}
    for line in text.split("\n"):
        clean = line.split("#", 1)[0].strip()
        if not clean or clean.startswith("-"):
            continue
        split = _REQUIREMENT_SPLIT.search(clean)
        name = (clean[:split.start()] if split else clean).strip()
        if name:
            out[name.lower()] = clean[len(name):].strip() or "*"
    return out


def _pyproject(text: str) -> Dict[str, str]:
    """PEP 621 and Poetry dependency tables out of `pyproject.toml`."""
    data = tomllib.loads(text)
    out: Dict[str, str] = {}
    project = data.get("project") or {}
    for item in project.get("dependencies") or []:
        split = _REQUIREMENT_SPLIT.search(str(item))
        name = (str(item)[:split.start()] if split else str(item)).strip()
        if name:
            out[name.lower()] = str(item)[len(name):].strip() or "*"
    for group, items in (project.get("optional-dependencies") or {}).items():
        for item in items or []:
            split = _REQUIREMENT_SPLIT.search(str(item))
            name = (str(item)[:split.start()] if split else str(item)).strip()
            if name:
                out[f"{name.lower()}[{group}]"] = str(item)[len(name):].strip() or "*"
    poetry = ((data.get("tool") or {}).get("poetry") or {}).get("dependencies") or {}
    for name, spec in poetry.items():
        out[str(name).lower()] = _short(spec if isinstance(spec, str) else json.dumps(spec, sort_keys=True))
    return out


def _package_json(text: str) -> Dict[str, str]:
    """`dependencies` and `devDependencies` out of a `package.json`."""
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("package.json is valid JSON but not an object")
    out: Dict[str, str] = {}
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        for name, spec in (data.get(section) or {}).items():
            key = str(name) if section == "dependencies" else f"{name}[{section}]"
            out[key] = _short(spec)
    return out


def _config_elements(rel: str, text: str) -> Tuple[Tuple[Element, ...], str]:
    """`config:<file>#<dotted key>` elements, and the reason none were read.

    The keys that already became `dependency:` elements are skipped, so that
    one fact -- httpx moved from 0.27 to 0.28 -- has one address. Two findings
    for one change is not thoroughness: it is a reader reconciling a delta
    against itself.
    """
    suffix = os.path.splitext(rel)[1].lower()
    if suffix not in CONFIG_SUFFIXES:
        return (), ""
    try:
        if suffix == ".json":
            loaded = json.loads(text)
        elif suffix == ".toml":
            loaded = tomllib.loads(text)
        else:
            loaded = _ini(text)
    except (ValueError, TypeError, tomllib.TOMLDecodeError,
            configparser.Error) as exc:
        return (), f"{type(exc).__name__}: {exc}"
    skip = _DEPENDENCY_CONFIG_PREFIXES.get(os.path.basename(rel).lower(), ())
    out: List[Element] = []
    for key, value in _flatten(loaded):
        if any(key == prefix or key.startswith(prefix + ".") for prefix in skip):
            continue
        out.append(Element(key=f"config:{rel}#{key}", kind="config", value=_short(value)))
    return tuple(out), ""


def _ini(text: str) -> Dict[str, Any]:
    """An `.ini`/`.cfg` as a nested mapping, with interpolation switched off.

    `RawConfigParser` and not `ConfigParser`: a `%` in a value is a format
    string to the second one, and a config file holding a printf template would
    raise instead of being read -- a parse failure caused entirely by the
    reader's own feature.
    """
    parser = configparser.RawConfigParser()
    parser.read_string(text)
    out: Dict[str, Any] = {}
    for section in parser.sections():
        out[section] = {option: parser.get(section, option)
                        for option in parser.options(section)}
    defaults = dict(parser.defaults())
    if defaults:
        out["DEFAULT"] = defaults
    return out


def _flatten(value: Any, prefix: str = "") -> Iterable[Tuple[str, str]]:
    """`(dotted key, rendered scalar)` for every leaf. §9's "config".

    List indices join with a dot like every other segment, so
    `hosts.0` addresses the first host: a second separator would mean the
    caller reading a delta has to know which of two spellings this adapter
    chose for a list.

    Scalars are rendered with `json.dumps`, so `"1"` and `1` are different
    values in the output -- which they are, and a config change from a number
    to a string is exactly the kind of quiet break a delta exists to show.
    """
    if isinstance(value, Mapping):
        for key in sorted(value, key=str):
            child = f"{prefix}.{key}" if prefix else str(key)
            yield from _flatten(value[key], child)
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            child = f"{prefix}.{index}" if prefix else str(index)
            yield from _flatten(item, child)
    else:
        try:
            rendered = json.dumps(value, sort_keys=True, default=str)
        except (TypeError, ValueError):
            rendered = str(value)
        yield prefix or "<root>", rendered


# -- the adapter -----------------------------------------------------------


class CodeAdapter:
    """The `code` domain. Implements the `DeltaAdapter` Protocol.

    Three revision shapes reach `snapshot`: a whole checkpoint
    (`checkpoint:<sha>` plus `scope.workspace`), one file (`file`, or
    `checkpoint:<sha>#<path>`), and a literal the caller stashed. Everything
    else -- an artifact, a document store entry -- is refused by
    `sources.resolve` with the reason that module owns, and refusing is the
    right answer: a reader guessed from the shape of a ref returns plausible
    bytes from the wrong place.
    """

    domain = "code"
    version = "1"

    def available(self) -> bool:
        """Always true: `ast` and `repo_map` are stdlib and in-repo.

        There is nothing to probe for. A language without an extractor here
        lowers the SEMANTIC COVERAGE of the comparison and is reported there;
        it does not make the adapter unavailable, and a `False` would send the
        request to a registry that refuses the domain outright -- a much bigger
        answer than "this repository has Go in it".
        """
        return True

    # -- reading one end ---------------------------------------------------

    def snapshot(self, revision: RevisionRef, *, scope: Scope) -> Snapshot:
        """One revision as files, symbols, imports, dependencies and config.

        An unreadable end is `unreadable(...)` with the resolver's own reason
        and never an exception: "the checkpoint is gone" is an ANSWER, it makes
        the delta `inconclusive`, and raising would discard the other end's
        work and say nothing about which end failed.

        The element budget cuts symbols, imports, dependencies and config keys
        and NEVER the `file:` elements. Those carry the content hashes every
        later step reads -- the rename detector included -- so a budget that
        removed one would make a large tree indistinguishable from a tree
        where that file does not exist.
        """
        files, notes, excluded, reason = self._read(revision, scope=scope)
        if files is None:
            return unreadable(revision, reason, tier="parser")

        anchors: List[Element] = []
        rest: List[Element] = []
        risk: Dict[str, List[str]] = {}
        failures: Dict[str, str] = {}
        code_files = 0
        parsed_files = 0
        seen: set = set()

        for rel in sorted(files):
            read = self._elements_for(rel, files[rel])
            if read.is_code:
                code_files += 1
            if read.parsed is True:
                parsed_files += 1
            if read.reason:
                failures[rel] = read.reason
            for token, where in read.risk.items():
                risk.setdefault(token, []).extend(where)
            for element in read.elements:
                if element.key in seen:
                    # Two elements at one address make alignment arbitrary --
                    # `Snapshot.index()` keeps the last and logs it. Keeping the
                    # first and saying so here is the same decision made where
                    # the cause is visible: a class and a function with one name
                    # in one file, which is legal Python and one address.
                    logger.warning("code adapter: duplicate element address %r "
                                   "in %s; keeping the first", element.key, rel)
                    notes.append(f"two elements share the address {element.key}; "
                                 f"only the first was kept")
                    continue
                seen.add(element.key)
                (anchors if element.key.startswith("file:") else rest).append(element)

        elements, cut = self._budgeted(anchors, rest, scope=scope)
        if cut:
            excluded.append(cut)
        if failures:
            notes.append(f"{len(failures)} file(s) could not be parsed and "
                         f"contributed no symbols, imports or keys; their "
                         f"findings are labelled {PARSER_DEGRADED}")

        return Snapshot(
            revision=revision,
            elements=elements,
            readable=True,
            # `parser` and not `hash`: the addressing above is what an AST and
            # three manifest readers produced. The findings that rest on a
            # regex extractor lower themselves to `algorithm` one at a time,
            # which is where that distinction belongs -- per finding, not per
            # snapshot.
            tier="parser",
            excluded=tuple(_unique(excluded)),
            notes=tuple(_unique(notes)),
            truncated=bool(cut),
            detail={
                "files": len(files),
                "code_files": code_files,
                "ast_parsed": parsed_files,
                "parse_failures": dict(sorted(failures.items())),
                "risk": {token: sorted(set(where)) for token, where in sorted(risk.items())},
            },
        )

    def _budgeted(self, anchors: List[Element], rest: List[Element], *,
                  scope: Scope) -> Tuple[Tuple[Element, ...], str]:
        """The elements, cut to `budget.max_elements`, and the sentence for it.

        Anchors (`file:`) are spent first and are never dropped. What the cut
        removes is the deeper layers, in address order, so the cut is
        deterministic: two runs over the same revision produce the same
        snapshot, which is what the §22 cache key depends on.
        """
        limit = scope.budget.max_elements
        ordered = sorted(anchors, key=lambda item: item.key) + sorted(rest, key=lambda item: item.key)
        if limit is None or len(ordered) <= limit:
            return tuple(ordered), ""
        kept = ordered[:max(limit, len(anchors))]
        dropped = len(ordered) - len(kept)
        return tuple(kept), (
            f"{dropped} element(s) below the file layer were not addressed; "
            f"`budget.max_elements` ({limit}) stopped the snapshot and nothing "
            f"past that point was compared")

    def _elements_for(self, rel: str, raw: bytes) -> _FileRead:
        """Every element one file contributes, plus what it could not give.

        The `file:` element is emitted for every file including the ones that
        do not decode as text: a PNG in a source tree still has a content hash,
        and a hash is what turns moving it into a `moved` instead of a deletion
        plus an addition.
        """
        lang = lang_for_path(rel)
        file_element = Element(
            key=f"file:{rel}", kind="file", hash=_sha(raw),
            value=f"{len(raw)} bytes",
            detail={"language": lang, "bytes": len(raw)},
        )
        text = _decode(raw)
        if text is None:
            # A degradation is only claimed where an extractor was expected. A
            # PNG in a source tree is not a parser failure -- nobody was going
            # to parse it -- and labelling it one would fill the delta with
            # `parser_degraded` rows about files whose hash comparison is
            # complete, which is how a real degradation gets scrolled past.
            expected = lang not in ("", "none") or os.path.basename(rel).lower() in DEPENDENCY_FILES
            return _FileRead(elements=(file_element,), binary=True,
                             is_code=lang not in ("", "none"), parsed=None,
                             reason=("the bytes are not UTF-8 text, so nothing "
                                     "below the file layer was read") if expected else "")
        if lang == "py":
            read = _python_file(rel, text)
        elif lang not in ("", "none"):
            read = _lexical_file(rel, text, lang)
        else:
            read = _FileRead()
        dependencies, dependency_reason = _dependency_elements(rel, text)
        config, config_reason = _config_elements(rel, text)
        reason = "; ".join(part for part in (read.reason, dependency_reason,
                                             config_reason) if part)
        return _FileRead(
            elements=(file_element,) + read.elements + dependencies + config,
            risk=read.risk,
            parsed=read.parsed,
            reason=reason,
            is_code=read.is_code or lang not in ("", "none"),
        )


    def _read(self, revision: RevisionRef, *, scope: Scope
              ) -> Tuple[Optional[Dict[str, bytes]], List[str], List[str], str]:
        """`(files, notes, excluded, reason)` -- `files is None` means unreadable.

        A tuple rather than an exception for the same reason `sources.resolve`
        returns one: which end failed and why is information the delta has to
        carry, and an exception here would throw away the other end.
        """
        notes: List[str] = []
        excluded: List[str] = []
        matched = CHECKPOINT_TREE_REF.match(revision.ref or "")
        if revision.kind == "checkpoint" and matched:
            return self._read_checkpoint(matched.group("sha"), scope=scope,
                                         notes=notes, excluded=excluded)

        resolved = sources.resolve(revision, scope=scope)
        if not resolved.readable:
            return None, notes, excluded, resolved.reason
        if resolved.truncated:
            excluded.append(resolved.reason)

        if resolved.media_type == CODE_TREE_MEDIA_TYPE:
            try:
                payload = resolved.mapping()
            except sources.SourceError as exc:
                return None, notes, excluded, str(exc)
            files: Dict[str, bytes] = {}
            for path, body in payload.items():
                if not isinstance(body, str):
                    return None, notes, excluded, (
                        f"the tree literal maps {path!r} to a "
                        f"{type(body).__name__} and not to source text; a tree "
                        f"is {{path: text}} and nothing else")
                files[_rel_path(path)] = body.encode("utf-8")
            return files, notes, excluded, ""

        return {self._single_path(revision, scope=scope): resolved.data()}, notes, excluded, ""

    def _single_path(self, revision: RevisionRef, *, scope: Scope) -> str:
        """The address one file is compared under. Stable across both ends.

        The address is what alignment matches on, so two ends of the same file
        must produce the same string or an unchanged file reads as a deletion
        plus an addition. A `file` revision uses its path relative to the
        workspace, a `checkpoint:<sha>#<path>` uses the path in the ref, and a
        literal uses its label when the caller set one to a path-shaped value.

        `sources.stash` fills `label` with "123 bytes of text/plain" for a
        caller that named nothing, and that string differs between two ends of
        different sizes -- so a label containing whitespace is refused and both
        ends fall back to one constant. Wrong in the safe direction: a shared
        address compares two revisions, a differing one compares neither.
        """
        ref = revision.ref or ""
        if revision.kind == "checkpoint":
            matched = sources.CHECKPOINT_REF.match(ref)
            if matched:
                return _rel_path(matched.group("path"))
        if revision.kind == "file":
            workspace = str(scope.workspace or "")
            if workspace and os.path.isabs(ref):
                try:
                    return _rel_path(os.path.relpath(ref, workspace))
                except ValueError:
                    return _rel_path(os.path.basename(ref))
            return _rel_path(ref)
        label = str(revision.label or "").strip()
        if label and not any(char.isspace() for char in label):
            return _rel_path(label)
        return LITERAL_PATH

    def _read_checkpoint(self, sha: str, *, scope: Scope, notes: List[str],
                         excluded: List[str]
                         ) -> Tuple[Optional[Dict[str, bytes]], List[str], List[str], str]:
        """Every file as it was at one checkpoint of `scope.workspace`.

        `scope.workspace` is the ABSOLUTE PATH and never the display name:
        `workspace_checkpoints` keys its shadow repositories on the realpath of
        the root, so a display name resolves to a repository that does not
        exist and the answer that comes back -- "no such checkpoint" -- is
        indistinguishable from a real one.

        `has_checkpoint` is asked before anything is read, for the reason its
        own docstring gives: every read there answers an unknown sha with the
        same empty result it gives for "the file did not exist", and those are
        opposite facts.

        The file list is the working tree, corrected by `changed_since`: a path
        the checkpoint does not have answers `None` from `file_at` and is
        dropped, and a path deleted since the checkpoint is added back from the
        `D` rows. There is no "list every blob at this sha" in
        `workspace_checkpoints`, and shelling out to `git ls-tree` from here
        would be a second git integration in a repository that already has one.
        """
        # Imported inside the function, as `sources` does it: importing the
        # delta package walks every adapter module, and a git-shaped module
        # with its subprocess machinery should not be dragged in for callers
        # that never resolve a checkpoint.
        from src import workspace_checkpoints

        workspace = str(scope.workspace or "").strip()
        if not workspace:
            return None, notes, excluded, (
                "the scope names no workspace, and a checkpoint only exists "
                "relative to one; `scope.workspace` is the absolute path of "
                "the workspace and not its display name")
        if not workspace_checkpoints.git_available():
            return None, notes, excluded, (
                "git is not available here, so this workspace has no shadow "
                "repository to read a checkpoint out of")
        if not workspace_checkpoints.has_checkpoint(workspace, sha):
            return None, notes, excluded, (
                f"this workspace's shadow repository does not know checkpoint "
                f"{sha}; it may belong to another machine or another data "
                f"directory, or a reset threw it away")

        wanted = self._tree_paths(workspace)
        for row in workspace_checkpoints.changed_since(workspace, sha):
            if str(row.get("status") or "").upper() == "D":
                wanted.add(_rel_path(row.get("path") or ""))

        files: Dict[str, bytes] = {}
        budget = scope.budget.max_bytes
        spent = 0
        for rel in sorted(path for path in wanted if path):
            raw = workspace_checkpoints.file_at(workspace, sha, rel)
            if raw is None:
                continue
            if budget is not None and spent + len(raw) > budget:
                excluded.append(
                    f"{rel} and every path after it were not read; "
                    f"`budget.max_bytes` ({budget}) stopped the snapshot and "
                    f"nothing past that point was compared")
                break
            spent += len(raw)
            files[rel] = raw
        if not files:
            notes.append(f"checkpoint {sha} yielded no readable file under "
                         f"this scope")
        return files, notes, excluded, ""

    def _tree_paths(self, workspace: str) -> set:
        """The working tree's files, relative and slash-separated.

        `index_walk` decides what a sweep skips -- hidden directories,
        `node_modules`, `__pycache__`, `venv` -- so that this adapter and the
        two indexes of this repository cannot drift about which files exist.
        The drift is what once left one index sweeping `.git/` while the other
        did not.
        """
        out: set = set()
        for dirpath, dirnames, filenames in os.walk(workspace, topdown=True):
            prune_index_dirs(dirnames)
            for name in filenames:
                if not is_indexable_file(name):
                    continue
                full = os.path.join(dirpath, name)
                out.add(_rel_path(os.path.relpath(full, workspace)))
        return out


    # -- comparing two ends ------------------------------------------------

    def compare(self, source: Snapshot, target: Snapshot, *,
                scope: Scope) -> Extraction:
        """What differs between two code revisions, one element at a time.

        `alignment.align` decides what corresponds to what BEFORE anything is
        said about change, which is what turns a rename into one `moved` row
        instead of a `missing` and an `added` that read as data loss plus
        unreviewed new code.

        An unreadable end produces no findings at all rather than a `missing`
        for every element: "we could not open it" and "it was emptied" are
        opposite facts, and a list of removals says the second. The coverage is
        still built, because `verdict.assess` reads `both_readable` first and
        answers `inconclusive` from it.
        """
        limitations: List[str] = [
            "symbols, imports, dependencies and configuration keys are "
            "compared; behaviour is not. No test was run here, and a green "
            "test would not prove the absence of collateral change either",
        ]
        if not (source.readable and target.readable):
            which = "source" if not source.readable else "target"
            limitations.append(
                f"the {which} revision could not be read, so nothing was "
                f"compared; this is not a report that nothing changed")
            return Extraction(
                findings=(),
                coverage=self._coverage(source, target, aligned=0.0, ratio=0.0,
                                        readable=False, regions=()),
                invariants=(),
                extractor_versions=self._versions(),
                limitations=tuple(_unique(limitations)),
            )

        result = alignment_mod.align(
            source, target, max_elements=scope.budget.max_elements or 5000)
        degraded = self._degraded(source, target)
        findings: List[Finding] = []
        paired = 0
        regions: List[str] = []
        for left, right, relation in result.pairs:
            if left is not None and right is not None:
                paired += 2
            for element in (left, right):
                if element is not None:
                    regions.append(element.key)
            findings.extend(self._findings_for(left, right, relation, degraded))

        total = len(source.elements) + len(target.elements)
        aligned = 0.0 if total == 0 else round(paired / total, 4)
        if result.truncated:
            limitations.append(
                "the alignment was cut by `budget.max_elements`, so some "
                "elements were never considered and their absence from this "
                "list is not evidence that they did not change")
        if degraded:
            limitations.append(
                f"{len(degraded)} file(s) did not parse; their symbols, "
                f"imports and keys were not compared and every finding about "
                f"them is labelled {PARSER_DEGRADED}")

        return Extraction(
            findings=tuple(findings),
            coverage=self._coverage(source, target, aligned=aligned,
                                    ratio=result.coverage_ratio, readable=True,
                                    regions=tuple(_unique(regions))),
            invariants=(),
            extractor_versions=self._versions(),
            limitations=tuple(_unique(limitations)),
        )

    def _versions(self) -> Dict[str, str]:
        """The §22 cache key's extractor half.

        The Python version is in here because `ast` is part of it: a signature
        this adapter renders can change between two minor releases (positional
        only parameters, `match` statements), and a cached delta computed with
        the older one would keep answering for a comparison nobody would make
        the same way today.
        """
        return {
            self.domain: self.version,
            "python_ast": f"{sys.version_info.major}.{sys.version_info.minor}",
            "repo_map_symbols": "1",
        }

    def _degraded(self, source: Snapshot, target: Snapshot) -> Dict[str, str]:
        """`path -> reason` for every file either end could not parse.

        Both ends together: a file that parsed in the source and not in the
        target is exactly the case where the comparison is weakest, and reading
        only one side's failures would let a broken target look like a clean
        comparison against an intact source.
        """
        out: Dict[str, str] = {}
        for snapshot in (source, target):
            failures = dict(snapshot.detail or {}).get("parse_failures") or {}
            for path, reason in failures.items():
                out[str(path)] = str(reason)
        return out

    def _method_for(self, element: Element) -> Tuple[str, str]:
        """`(method, tier)` for one element, out of `confidence.METHOD_TIERS`.

        Read from the table rather than written here so that one place decides
        what a method is worth. The only element that chooses between two
        methods is a symbol: one found by `ast` is `ast_diff` at tier `parser`,
        one found by a line-start regex is `line_diff` at tier `algorithm`, and
        the element's own `certainty` -- `code_index`'s word -- is what says
        which of the two produced it.
        """
        layer, _path, _name = _key_parts(element.key)
        if layer == "symbol":
            lexical = str(dict(element.detail or {}).get("certainty") or "") == CERTAINTY[2]
            method = "line_diff" if lexical else "ast_diff"
        else:
            method = {
                "file": "content_hash",
                "import": "ast_diff",
                "dependency": "manifest_field",
                "config": "schema_diff",
            }.get(layer, "element_key")
        return method, confidence_mod.tier_of(method)


    def _findings_for(self, left: Optional[Element], right: Optional[Element],
                      relation: Any, degraded: Mapping[str, str]) -> List[Finding]:
        """Every observation about one aligned pair. Never a classification.

        `unchanged` is emitted and not skipped: it says we LOOKED, which is a
        different statement from the absence of a row, and it is the only thing
        a later layer can turn into `preserved` with an observation behind it.
        Without these rows the honest answer about an untouched file is
        `unknown`, and a delta full of `unknown` about files nobody touched is
        a delta nobody reads.

        §9: behaviour is never inferred from the name of a function. Nothing
        below reads a symbol's name for anything except whether it is public --
        a question about the SURFACE, not about what the code does -- and a
        renamed function is a `missing` plus an `added` unless its bytes say
        otherwise, because two functions with related names are not evidence of
        the same behaviour.
        """
        element = left if left is not None else right
        if element is None:  # pragma: no cover - align() never yields two Nones
            return []
        layer, rel, name = _key_parts(element.key)
        method, tier = self._method_for(element)
        limitations: List[str] = []
        if rel and rel in degraded:
            limitations.append((
                f"{PARSER_DEGRADED}: {rel} did not parse ({degraded[rel]}); its "
                f"symbols, imports and keys were not compared, and their "
                f"absence from this delta is not evidence that they are gone"
            )[:480])
            # Demotion, never promotion. The bytes were still hashed, so the
            # METHOD is what it was; what the result is worth dropped, because
            # the structure behind those bytes was never read. §3.2 forbids a
            # later layer raising an earlier one's certainty and says nothing
            # against lowering it.
            tier = "algorithm"
        confidence = confidence_mod.propagate("exact", alignment=relation,
                                              limitations=limitations)
        shared: Dict[str, Any] = {
            "method": method, "tier": tier, "confidence": confidence,
            "alignment": relation, "element_kind": element.kind,
            "limitations": tuple(limitations),
        }

        if relation.relation == "moved" and left is not None and right is not None:
            # The whole point of the layer. A rename is ONE observation about a
            # file that was never deleted; `missing` + `added` is two, and the
            # first of them reads as data loss.
            return [Finding(path=right.key, operation="moved",
                            before=left.key, after=right.key,
                            detail="identical content at a different address",
                            **{**shared, "method": "content_hash",
                               "tier": "algorithm" if limitations else "hash"})]

        if left is not None and right is not None:
            if _signature_key(left) == _signature_key(right) and left.value == right.value:
                return [Finding(path=element.key, operation="unchanged",
                                before=_render(left), after=_render(right), **shared)]
            findings = [Finding(path=element.key, operation="modified",
                                before=_render(left), after=_render(right),
                                **shared)]
            findings.extend(self._signature_findings(left, right, shared))
            return findings

        if left is not None:
            refs: Tuple[str, ...] = ()
            if layer == "symbol" and _is_public(name):
                # A withdrawn export breaks callers this comparison cannot see,
                # which is what makes it a compatibility question and not a
                # matter of taste. A private symbol carries no such promise.
                refs = (COMPATIBILITY_INVARIANT,)
            return [Finding(path=left.key, operation="missing",
                            before=_render(left), invariant_refs=refs, **shared)]

        return [Finding(path=right.key, operation="added", after=_render(right),
                        invariant_refs=_widening_invariants(layer, name), **shared)]

    def _signature_findings(self, left: Element, right: Element,
                            shared: Mapping[str, Any]) -> List[Finding]:
        """The separate row a changed signature gets. §9's "firmas".

        Separate from the `modified` about the symbol because they are two
        different facts with two different audiences: the body changed (whoever
        reviews this repository) and the CONTRACT changed (everyone who calls
        it). Folding them into one row means the second is read as the first
        and the compatibility invariant never gets pointed at.
        """
        layer, _path, _name = _key_parts(left.key)
        if layer != "symbol" or left.value == right.value or not (left.value and right.value):
            return []
        return [Finding(
            path=f"{left.key}#signature",
            operation="modified",
            before=_short(left.value),
            after=_short(right.value),
            invariant_refs=(COMPATIBILITY_INVARIANT,),
            detail="the signature changed; a caller written against the old "
                   "one does not compile against the new one",
            **{**dict(shared), "element_kind": "signature"},
        )]


    # -- how much of it we actually compared -------------------------------

    def _coverage(self, source: Snapshot, target: Snapshot, *, aligned: float,
                  ratio: float, readable: bool, regions: Sequence[str]) -> Coverage:
        """The three axes this adapter can speak to, and one it cannot.

        `structural` is the fraction of elements that found a partner, lowered
        by whatever alignment itself could not consider: an element nobody
        aligned was compared against nothing, and counting it as covered would
        make an added-only delta look exhaustively compared.

        `semantic` is the fraction of code files an AST actually read. A Go or
        JavaScript file counts in the denominator and not in the numerator --
        its symbols came from a line-start regex, which finds definitions and
        nothing about what encloses them. That gap is a real limit on what this
        delta can mean and it belongs in a number, not in a footnote.

        `behavioral` is 0.0, declared and not omitted: `Coverage.ratio`
        distinguishes "not measured" from "measured and covered none", and this
        adapter ran no test at all. §9's rule sits underneath that zero -- a
        green test does not prove the absence of collateral change -- so even a
        caller who hands over a passing suite has not bought behavioural
        coverage of the elements this delta lists.
        """
        left = dict(source.detail or {})
        right = dict(target.detail or {})
        code_files = int(left.get("code_files") or 0) + int(right.get("code_files") or 0)
        parsed = int(left.get("ast_parsed") or 0) + int(right.get("ast_parsed") or 0)
        structural = round(min(aligned, ratio), 4) if readable else 0.0
        semantic = 0.0 if code_files == 0 else round(min(1.0, parsed / code_files), 4)

        notes = [
            "behavioural coverage is 0.0: no test result was handed to this "
            "adapter, and a passing test would not prove the absence of "
            "collateral change either",
            f"{parsed} of {code_files} code file(s) across both revisions were "
            f"read by an AST; the rest were read by line-start regexes, whose "
            f"findings are tier `algorithm`",
        ]
        if not readable:
            which = "source" if not source.readable else "target"
            notes.append(f"the {which} revision could not be read, so no "
                         f"element of it was compared")
        listed = tuple(regions)[:MAX_LISTED_REGIONS]
        if len(regions) > len(listed):
            notes.append(f"{len(regions) - len(listed)} further element keys "
                         f"were compared and are not listed here")
        return coverage_mod.build(
            source_readable=source.readable,
            target_readable=target.readable,
            dimensions={"structural": structural, "semantic": semantic,
                        "behavioral": 0.0},
            regions=listed,
            excluded=_unique(list(source.excluded) + list(target.excluded)),
            notes=_unique(notes + list(source.notes) + list(target.notes)),
        )

    # -- invariants --------------------------------------------------------

    def _symbols(self, snapshot: Snapshot) -> Dict[str, Element]:
        """The symbol elements of one end, by key."""
        return {key: element for key, element in snapshot.index().items()
                if _key_parts(key)[0] == "symbol"}

    def _new_risk(self, source: Snapshot, target: Snapshot) -> Dict[str, Tuple[str, ...]]:
        """Sensitive tokens the target carries and the source did not.

        Compared as a SET over the whole revision and not per file: moving an
        existing `subprocess` call from one module to another does not widen
        what the code can do, and reporting it as a new permission would put a
        blocking row on a refactor. What this cannot see is a capability that
        was already there being used in a new place, and that limitation is
        named in the result rather than fixed by over-reporting.
        """
        before = dict(dict(source.detail or {}).get("risk") or {})
        after = dict(dict(target.detail or {}).get("risk") or {})
        return {token: tuple(str(item) for item in (after.get(token) or ()))
                for token in sorted(after) if token not in before}

    def _withdrawn_public_symbols(self, source: Snapshot, target: Snapshot,
                                  degraded: Mapping[str, str]) -> Tuple[str, ...]:
        """Public symbols in the source that are nowhere in the target.

        "Nowhere" and not "not at the same address": a symbol whose bytes moved
        to another file is still exported, and calling that a withdrawal would
        make every refactor a compatibility violation until nobody reads the
        row any more.

        A symbol whose FILE failed to parse on either end is skipped, and this
        is the important half. Such a symbol is missing from the snapshot
        because nobody could read the file, not because anybody withdrew it,
        and reporting it as a violation would turn a gap in the extraction into
        evidence about the code -- a `violated` naming the wrong cause, which
        sends a reader to look for a deletion that never happened. The gap
        becomes `unknown` in `_compatibility_result` instead, where it says so.
        """
        target_symbols = self._symbols(target)
        target_hashes = {element.hash for element in target_symbols.values() if element.hash}
        out: List[str] = []
        for key, element in sorted(self._symbols(source).items()):
            _layer, path, name = _key_parts(key)
            if not _is_public(name) or path in degraded:
                continue
            if key in target_symbols or (element.hash and element.hash in target_hashes):
                continue
            out.append(key)
        return tuple(out)

    def _signature_changes(self, source: Snapshot, target: Snapshot,
                           degraded: Mapping[str, str]) -> Tuple[str, ...]:
        """Symbols at the same address whose signature is not the same string.

        Rendered from the AST on both sides, so re-indenting a parameter list
        is not a change: the comparison is between two renderings of the same
        shape, which is what keeps a formatter from producing a wall of
        compatibility violations.

        A file that did not parse contributes nothing here either, for the same
        reason: an absent signature is an absent reading, not a changed API.
        """
        target_symbols = self._symbols(target)
        out: List[str] = []
        for key, element in sorted(self._symbols(source).items()):
            _layer, path, _name = _key_parts(key)
            if path in degraded:
                continue
            other = target_symbols.get(key)
            if other is None or not element.value or not other.value:
                continue
            if element.value != other.value:
                out.append(key)
        return tuple(out)


    def check_invariants(self, intent: IntentContract, source: Snapshot,
                         target: Snapshot, *,
                         scope: Scope) -> Tuple[InvariantResult, ...]:
        """One result per declared invariant, and only three ways to say more
        than `unknown`.

        * **compatibility** -- a public symbol nowhere in the target, or a
          signature that is not the same string: `violated`, at tier `parser`,
          because an AST said so on both sides.
        * **permissions / security** -- a sensitive import or call the source
          did not have: `violated` at tier `algorithm` and confidence `medium`
          AT MOST. This is a lexical scan over an AST: it sees the token, not
          the execution, and it cannot tell a reachable `subprocess.run` from
          one inside a branch nobody takes. Calling it `exact` would turn a
          reading of the text into a proof about the behaviour, and no caller
          who read `exact` would go and check.
        * **budget** -- whether a snapshot was cut. A fact about this run
          rather than about the code, and the only one here that is exact.

        Everything else is `unknown` WITH the limitation named. `preserved`
        costs an observation and a threshold (`InvariantResult.parse` refuses
        it without one), and the whole subsystem exists to keep "we did not
        look" from being rendered as "it is fine".
        """
        readable = source.readable and target.readable
        degraded = self._degraded(source, target)
        complete = bool(readable and not source.truncated and not target.truncated
                        and not degraded)
        new_risk = self._new_risk(source, target)
        withdrawn = self._withdrawn_public_symbols(source, target, degraded)
        signatures = self._signature_changes(source, target, degraded)
        gap = self._gap(source, target, degraded)

        results: List[InvariantResult] = []
        for invariant in intent.invariants:
            payload: Dict[str, Any] = {
                "invariant_id": invariant.id,
                "severity": invariant.severity,
            }
            if not readable:
                which = "source" if not source.readable else "target"
                payload.update({
                    "status": "unknown", "confidence": "unknown", "tier": "parser",
                    "method": "element_key",
                    "limitations": [f"the {which} revision could not be read, so "
                                    f"no property of it was observed"],
                })
            elif invariant.klass == "budget":
                payload.update(self._budget_result(source, target))
            elif invariant.klass == "compatibility":
                payload.update(self._compatibility_result(
                    withdrawn, signatures, complete=complete, gap=gap, source=source))
            elif invariant.klass in ("permissions", "security"):
                payload.update(self._risk_result(
                    invariant.id, new_risk, complete=complete, gap=gap))
            else:
                payload.update({
                    "status": "unknown", "confidence": "unknown", "tier": "parser",
                    "method": "element_key",
                    "limitations": [
                        f"this adapter has no method for a `{invariant.klass}` "
                        f"property; it compares files, symbols, imports, "
                        f"dependencies and configuration keys, and nothing here "
                        f"observed that property"],
                })
            results.append(InvariantResult.parse(
                payload, f"invariant_result[{invariant.id}]"))
        return tuple(results)

    def _gap(self, source: Snapshot, target: Snapshot,
             degraded: Mapping[str, str]) -> str:
        """The one sentence naming why a check could not be complete.

        Built once and reused by every `unknown`, so that three invariants that
        went unanswered for one reason say the same thing about it -- three
        wordings of one gap read as three gaps.
        """
        if source.truncated or target.truncated:
            return ("a budget cut at least one snapshot, so the part nobody "
                    "addressed is not evidence that nothing happened there")
        if degraded:
            return (f"{len(degraded)} file(s) did not parse ("
                    f"{', '.join(sorted(degraded)[:3])}), so their symbols, "
                    f"imports and calls were never read")[:480]
        return ""

    def _budget_result(self, source: Snapshot, target: Snapshot) -> Dict[str, Any]:
        """Whether the comparison stayed inside its budget. Exact, and ours.

        Tier `parser` and confidence `exact` for a claim about our own two
        snapshots: either they carry `truncated` or they do not, and there is
        no margin in reading a boolean this module set itself. It is the one
        invariant here that is not a statement about the code.
        """
        cut = bool(source.truncated or target.truncated)
        excluded = list(source.excluded) + list(target.excluded)
        if cut:
            return {
                "status": "violated", "confidence": "exact", "tier": "parser",
                "method": "element_key",
                "observations": [_short(item) for item in _unique(excluded)][:8]
                                or ["a snapshot was truncated"],
            }
        return {
            "status": "preserved", "confidence": "exact", "tier": "parser",
            "method": "element_key",
            "observations": [
                f"neither snapshot was truncated: "
                f"{len(source.elements)} source and {len(target.elements)} "
                f"target elements were addressed within the budget"],
        }

    def _compatibility_result(self, withdrawn: Sequence[str],
                              signatures: Sequence[str], *, complete: bool,
                              gap: str, source: Snapshot) -> Dict[str, Any]:
        """The public surface: nothing withdrawn, nothing re-signed. §9's API layer."""
        broken = list(withdrawn) + list(signatures)
        if broken:
            observations = (
                [f"public symbol withdrawn: {key}" for key in withdrawn][:8]
                + [f"signature changed: {key}" for key in signatures][:8])
            return {
                "status": "violated",
                # `parser` because an AST rendered both signatures and both
                # symbol tables. `high` rather than `exact` when a file did not
                # parse: the violation was seen, and the SET it was seen in was
                # incomplete.
                "confidence": "exact" if complete else "high",
                "tier": "parser", "method": "symbol_table",
                "observations": [item[:480] for item in observations],
                "limitations": [gap] if gap else [],
            }
        if not complete:
            return {
                "status": "unknown", "confidence": "unknown", "tier": "parser",
                "method": "symbol_table",
                "limitations": [gap or "the comparison was not complete"],
            }
        public = sum(1 for key in self._symbols(source)
                     if _is_public(_key_parts(key)[2]))
        return {
            "status": "preserved", "confidence": "exact", "tier": "parser",
            "method": "symbol_table",
            "observations": [
                f"{public} public symbol(s) in the source; every one of them is "
                f"present in the target",
                "no signature at a shared address differs between the two"],
        }

    def _risk_result(self, invariant_id: str,
                     new_risk: Mapping[str, Tuple[str, ...]], *, complete: bool,
                     gap: str) -> Dict[str, Any]:
        """The lexical permission and network scan. Never better than `medium`.

        The ceiling is the point of the method. `subprocess` appearing in the
        target and not in the source is an observation about the text of the
        program; whether that line ever runs is a question about execution, and
        nothing here ran anything. `medium` is what §3.2 calls a measurement
        through a proxy for the property rather than of the property, and this
        is one -- so the confidence says `medium`, the tier says `algorithm`,
        and neither of them ever says `exact`.
        """
        if invariant_id == SECRETS_INVARIANT:
            return {
                "status": "unknown", "confidence": "unknown", "tier": "algorithm",
                "method": "line_diff",
                "limitations": [
                    "no credential scan runs in this adapter; it reads imports, "
                    "calls, dependencies and configuration keys, and a key "
                    "added inside a string literal would not appear in any of "
                    "them"],
            }
        tokens = _CHECKED_BY_IMPORT_SCAN.get(invariant_id)
        recognised = tokens is not None
        watched = tokens if tokens is not None else SENSITIVE_TOKENS
        hits = {token: where for token, where in new_risk.items() if token in watched}
        if hits:
            return {
                "status": "violated", "confidence": "medium", "tier": "algorithm",
                "method": "line_diff",
                "observations": [
                    (f"the target imports or calls `{token}` and the source did "
                     f"not ({', '.join(where[:3]) or 'no path recorded'})")[:480]
                    for token, where in sorted(hits.items())][:8],
                "limitations": [
                    "this is a lexical scan over an AST: it sees the token, not "
                    "the execution, and it does not prove the call is reached"]
                    + ([gap] if gap else []),
            }
        if not recognised:
            return {
                "status": "unknown", "confidence": "unknown", "tier": "algorithm",
                "method": "line_diff",
                "limitations": [
                    f"this adapter does not know what `{invariant_id}` names; it "
                    f"scanned for {', '.join(SENSITIVE_TOKENS)} and found "
                    f"nothing new, which is not the same as having checked the "
                    f"property this invariant declares"],
            }
        if not complete:
            return {
                "status": "unknown", "confidence": "unknown", "tier": "algorithm",
                "method": "line_diff",
                "limitations": [gap or "the comparison was not complete"],
            }
        return {
            "status": "preserved", "confidence": "medium", "tier": "algorithm",
            "method": "line_diff",
            "observations": [
                (f"no new import of or call to {', '.join(watched)} appears in "
                 f"the target")[:480],
                "every code file on both ends was read before saying so"],
            "limitations": [
                "a capability the source already had, used somewhere new, is "
                "not visible to a scan that compares token sets"],
        }


#: How `registry.py` finds this adapter. A module-level factory and NOT a line
#: in a tuple somewhere else: `state_mirror/adapters/__init__.py` records what
#: the tuple cost -- five correct adapters that did not exist as far as the
#: running system was concerned, with green tests and no warning, because
#: nobody added them to it. Writing this line IS the registration.
ADAPTER_FACTORY = CodeAdapter
