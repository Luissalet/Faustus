"""Compiling a request into a boundary, and measuring distance to it.

Sections 6 and 12 of `inspiration/PLAN_GREEDY_COMPLETION_ENGINE_FAUSTUS.md`.
`completion_engine/contracts.py` owns the SHAPE of a `ScopeEnvelope`; this
module owns how one is built out of a request, how far a resource sits from
what was asked, and whether a candidate may go anywhere near it.

Four rules are mechanised here, each with the failure it prevents:

1.  **Permission is checked before value, and before scope.** `admits` asks the
    run's `PermissionEnvelope` first and answers `no_permission` without ever
    reading `expected_value`. That order is rule 1 of the contracts docstring:
    a candidate that scored first would get the chance to argue its way past a
    missing permission with a high number, and the number is the one part of a
    candidate that nothing outside the engine can check.

2.  **An instruction only narrows.** `narrow_from_instruction` reads the MODE
    with `agent_profiles.completion.from_instruction` -- which already exists,
    in two languages, and is not restated here -- and reads the SCOPE itself.
    Both readings intersect; neither adds. There is no widening path in this
    module at all, which is Â§1.12.1 enforced by absence rather than by a check
    somebody has to remember to write.

3.  **A relation is read off paths, never off names.** Â§13 and Â§30: a function
    called `validate_user` in a file nobody touched is not `direct` because the
    goal says "validate the user". `relation_of` looks at directories, stems
    and filename conventions, and at nothing else. A name-based relation would
    let a rename change the scope of a run.

4.  **A protected resource is `unrelated`, whatever the allow list says.** That
    is the case a whitelist-only system gets wrong: "fix the login, do not
    touch settings.py" needs both halves, and `settings.py` sitting inside an
    allowed `src/` must come out of `relation_of` as far away as anything
    outside the repository.

Nothing here reads a file, runs a command or calls a model. The envelope is a
statement about a request; deriving it from the state of the disk would make
two runs of the same request answer differently.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Sequence, Tuple

from src.agent_profiles.completion import CompletionPolicy, from_instruction, policy_for
from src.completion_engine.contracts import (
    EFFECTS,
    GREEDY_RELATIONS,
    ImprovementCandidate,
    ScopeEnvelope,
    relation_rank,
)

__all__ = [
    "DEFAULT_DOMAINS",
    "resources_named",
    "compile_envelope",
    "relation_of",
    "admits",
    "narrow_from_instruction",
    "describe",
]


# -- what kind of thing a resource is --------------------------------------

#: Domain -> the markers that identify one of its resources. Three shapes, and
#: the shape says how the marker is matched:
#:
#:   * ending in `/`  -- a directory prefix, at a path boundary;
#:   * starting with `.` -- a filename extension;
#:   * anything else  -- an exact basename.
#:
#: Deliberately NOT a substring rule. `test_` as a substring would file
#: `latest_run.py` under `tests`, and a domain nobody can predict is worse than
#: no domain at all. A resource may belong to several domains -- `setup.py` is
#: `code` and `packaging` -- because it genuinely does, and picking one would
#: mean picking it wrongly for half the callers.
DEFAULT_DOMAINS: Dict[str, Tuple[str, ...]] = {
    "code": (".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx",
             ".go", ".rs", ".java", ".rb", ".php", ".c", ".h", ".cc", ".cpp",
             ".cs", ".swift", ".kt", ".sh", ".ps1"),
    "tests": ("tests/", "test/", "spec/", "__tests__/"),
    "docs": (".md", ".rst", ".txt", ".adoc", "docs/"),
    "config": (".toml", ".ini", ".cfg", ".conf", ".yaml", ".yml", ".json",
               ".env", "config/", "pyproject.toml", "package.json"),
    "web": (".html", ".css", ".scss", ".svg", ".vue", ".svelte", "static/",
            "templates/"),
    "data": (".csv", ".tsv", ".parquet", ".sql", ".ndjson", "data/"),
    "packaging": ("Dockerfile", "Makefile", "requirements.txt", "setup.py",
                  ".lock", ".cfg"),
}

#: Every extension any domain knows. A bare word is only read as a resource
#: when it carries one of these, so "e.g" and "vs.py" are told apart without a
#: filesystem lookup -- which this module is not allowed to do.
_KNOWN_EXTS: Tuple[str, ...] = tuple(sorted({
    marker for markers in DEFAULT_DOMAINS.values() for marker in markers
    if marker.startswith(".")
}))

_TOKEN_RE = re.compile(r"[A-Za-z0-9_.\-]+(?:[/\\][A-Za-z0-9_.\-]+)*[/\\]?")

#: Where a project keeps its tests when it does not keep them beside the code.
#: Two names, not a search: this module does no I/O, and an allowed path that
#: does not exist costs nothing while a missing one costs the run its ability
#: to write the regression test Â§13 lists first under `professional`.
_TEST_ROOTS: Tuple[str, ...] = ("tests", "test")


def _norm(value: Any) -> str:
    """A path in one spelling: forward slashes, no `./`, no trailing slash."""
    target = str(value or "").replace("\\", "/").strip()
    while target.startswith("./"):
        target = target[2:]
    return target.rstrip("/")


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _dirname(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def _stem_path(path: str) -> str:
    """`src/auth.py` -> `src/auth`: the path a package of the same name has."""
    directory, base = _dirname(path), _basename(path)
    stem = _stem(base)
    return f"{directory}/{stem}" if directory else stem


def _within(child: str, parent: str) -> bool:
    """Containment on path boundaries. `src` holds `src/a.py`, not `srcs/a.py`."""
    inner, outer = _norm(child), _norm(parent)
    if not inner or not outer:
        return False
    return inner == outer or inner.startswith(outer + "/")


def _is_absolute(path: str) -> bool:
    target = _norm(path)
    return target.startswith("/") or bool(re.match(r"^[A-Za-z]:/", target))


def _relative(workspace: str, path: str) -> str:
    """A workspace-relative spelling, when the path is inside the workspace.

    The prefix test is case-insensitive because Windows hands back `D:\\Local`
    and `d:\\local` for the same directory, and a case-sensitive test would
    leave an absolute path sitting in a list of relative ones -- where
    `covers()` would then answer `False` for a file that is plainly inside.
    """
    target = _norm(path)
    root = _norm(workspace)
    if root and (target.lower() == root.lower()
                 or target.lower().startswith(root.lower() + "/")):
        return target[len(root) + 1:] or target
    return target


def _looks_like_resource(token: Any) -> bool:
    """Whether a word names a file or a directory rather than an identifier.

    A separator, or an extension this module knows. `validate_user` is neither,
    and rule 3 of the module docstring is the reason that matters: a goal that
    mentions a function must not be able to promote an unrelated file to
    `direct` by sharing a word with it.
    """
    raw = str(token or "").strip()
    if len(raw.strip("/\\")) < 2:
        return False
    if "/" in raw or "\\" in raw:
        return True
    base = _basename(_norm(raw)).lower()
    return any(base.endswith(ext) for ext in _KNOWN_EXTS)


def resources_named(text: str, workspace: str = "") -> Tuple[str, ...]:
    """Every resource a sentence names, in order, deduplicated.

    Deterministic and offline: the sentence is the only evidence. A token that
    does not pass `_looks_like_resource` is dropped rather than guessed at.

    Public because `discovery.py` has to tell a definition-of-done line that
    names a file from one that does not, and the alternative was a second copy
    of this regex in another module -- two readers of the same sentence that
    agree until one of them is edited.
    """
    out: List[str] = []
    for raw in _TOKEN_RE.findall(str(text or "")):
        token = raw.strip(".,;:-")
        if not _looks_like_resource(token):
            continue
        target = _relative(workspace, token)
        if target and target not in out:
            out.append(target)
    return tuple(out)


def _domains_of(resource: str) -> Tuple[str, ...]:
    """Every domain whose markers match, by the grammar `DEFAULT_DOMAINS` states."""
    target = _norm(resource)
    if not target:
        return ()
    lowered = target.lower()
    base = _basename(target)
    found: List[str] = []
    for domain, markers in DEFAULT_DOMAINS.items():
        for marker in markers:
            hit = False
            if marker.endswith("/"):
                prefix = marker.rstrip("/").lower()
                hit = lowered == prefix or lowered.startswith(prefix + "/") \
                    or ("/" + prefix + "/") in lowered
            elif marker.startswith("."):
                hit = lowered.endswith(marker)
            else:
                hit = base == marker
            if hit:
                found.append(domain)
                break
    return tuple(found)


# -- compiling the envelope ------------------------------------------------


def _relations_for(policy: CompletionPolicy) -> Tuple[str, ...]:
    """How far a mode may reach, read OFF the policy rather than off its name.

    Â§6's relations are distance; a completion mode is depth. The two axes meet
    in exactly one place -- a mode that does not explore a frontier has no use
    for `similar_case`, and a mode that may add no finishing layer at all has
    no use for anything but `direct` -- and that meeting is computed from the
    policy's own fields, not from a second table keyed by mode name. A second
    table would agree with `POLICIES` until the day somebody edited one of them.

    `opportunistic` is never reachable, `maximalist` included. GREEDY_RELATIONS
    leaves it out because Â§12 only allows acting unasked on something local,
    reversible, in scope and verifiable, and an opportunistic change is by
    definition not the first of those. That argument is about acting unasked,
    not about depth, so a deeper mode does not escape it -- and letting one
    would be a mode granting reach, which is the shape of thing Â§3.3 forbids.
    """
    if policy.explore_frontier:
        return GREEDY_RELATIONS
    if int(policy.max_extra_layers) <= 0:
        return ("direct",)
    return ("direct", "adjacent", "downstream")


def compile_envelope(*, goal: str, instruction: str = "", owner: str = "",
                     project_id: str = "", workspace: str = "",
                     mode: str = "greedy", touched: Sequence[str] = (),
                     permissions: Any = None, intent_contract_id: str = "",
                     protected: Sequence[str] = ()) -> ScopeEnvelope:
    """The boundary this request implies. Â§6, deterministic and offline.

    The working area is the resources the goal NAMES, plus the ones the run has
    already touched, plus -- for any mode that may reach `adjacent` -- the
    directory each of those lives in, because Â§6 defines `adjacent` as the same
    component and a component is a directory on disk. A request that names no
    resource leaves `allowed_resources` empty, which the contract reads as "this
    envelope did not name a working area": the run is then bounded by its
    permissions and by relation, which is the ordinary case and must not be
    turned into "nothing is allowed".

    `permissions` is mirrored into `forbidden_effects` and never into
    `allowed_effects`. The envelope may restate what the run CANNOT do -- a deny
    list can only ever shrink what is reachable -- and may not restate what it
    can, because `PermissionEnvelope.effects` and `ScopeEnvelope.allowed_effects`
    read empty in opposite directions: empty effects grant nothing, an empty
    allow list is silence. Copying one into the other would invert the meaning
    of the most cautious envelope there is.

    `workspace` is used only to relativise absolute paths. It is not stored:
    `ScopeEnvelope` has no field for it, and `allowed_resources` holds
    workspace-relative paths, so mixing an absolute root in would leave
    `covers()` answering against two coordinate systems at once.

    An unknown `mode` raises `ProfileError` from `policy_for` rather than
    falling back to `greedy`, for the reason that function gives: a silent
    fallback answers a typo by spending an afternoon of GPU on it.
    """
    policy = policy_for(mode)
    relations = _relations_for(policy)
    ambiguities: List[str] = []

    named = resources_named(goal, workspace)
    touched_paths = tuple(_relative(workspace, t) for t in touched if _norm(t))
    resources: List[str] = []
    for candidate in named + touched_paths:
        if candidate and candidate not in resources:
            resources.append(candidate)

    allowed: List[str] = list(resources)
    if "adjacent" in relations:
        for resource in resources:
            parent = _dirname(resource)
            if parent and parent not in allowed:
                allowed.append(parent)
    if allowed and "downstream" in relations:
        # A mode that may reach `downstream` may write the test for what it
        # changed, and it cannot do that if the test roots are outside the
        # area. Opening the roots is safe because the AREA is not what keeps a
        # run honest here: an unrelated test file comes back from
        # `relation_of` as `opportunistic`, which `GREEDY_RELATIONS` excludes,
        # so the relation filter still refuses it. Leaving the roots out
        # instead would make the professional layer structurally impossible.
        for root in _TEST_ROOTS:
            if root not in allowed:
                allowed.append(root)
    if not allowed:
        ambiguities.append(
            "the request named no resource: the envelope bounds this run by "
            "permission and relation, not by a list of paths")

    domains: List[str] = []
    for resource in resources:
        for domain in _domains_of(resource):
            if domain not in domains:
                domains.append(domain)

    forbidden: Tuple[str, ...] = ()
    if permissions is not None:
        forbidden = tuple(e for e in EFFECTS if not permissions.permits_effect(e))

    guarded: List[str] = []
    for resource in protected:
        target = _relative(workspace, resource)
        if target and target not in guarded:
            guarded.append(target)

    text = str(goal or "")
    if len(text) > 2000:
        ambiguities.append("goal recorded truncated at 2000 chars")

    envelope = ScopeEnvelope.parse({
        "goal": text[:2000],
        "intent_contract_id": str(intent_contract_id or "")[:120],
        "allowed_domains": sorted(domains),
        "allowed_resources": allowed[:512],
        "protected_resources": guarded[:512],
        "forbidden_effects": list(forbidden),
        "allowed_relations": list(relations),
        "ambiguities": ambiguities[:256],
        "owner": str(owner or "")[:200],
        "project_id": str(project_id or "")[:200],
    }, "scope")
    if instruction:
        envelope = narrow_from_instruction(envelope, instruction)
    return envelope


# -- reading the scope out of an instruction -------------------------------

#: Words that turn a cue into its opposite. The same guard
#: `agent_profiles.completion` applies to the MODE half of a sentence, restated
#: rather than imported: that name is private to the other package, and the two
#: readings answer different questions about the same words. "no solo cambies
#: eso" is not a request to shrink the envelope.
_NEGATIONS = frozenset({"no", "not", "never", "nunca", "sin", "dont", "doesnt"})

#: "work on X and nothing else". Whole words, and never negated.
_ONLY_CUES: Tuple[str, ...] = (
    "solo", "solamente", "unicamente", "exclusivamente",
    "only", "just", "nothing but",
)

#: "leave X alone". The `no` in "no toques" belongs to the cue, so the negation
#: guard is deliberately NOT applied to this list -- it would cancel every one.
_KEEP_CUES: Tuple[str, ...] = (
    "sin tocar", "sin modificar", "sin cambiar", "no toques", "no modifiques",
    "no cambies", "no edites", "without touching", "without modifying",
    "without changing", "do not touch", "dont touch", "do not modify",
    "dont modify", "do not change", "dont change", "leave alone",
)

_CLAUSE_RE = re.compile(
    r"[,;:()\[\]\n]+|\.\s+|\s+(?:y|and|but|pero|aunque|although)\s+")


def _fold(text: str) -> str:
    """Lowercase, accents stripped, apostrophes dropped -- punctuation KEPT.

    `completion._normalize` folds punctuation to spaces, which is right for
    matching a cue and fatal for reading a path: `src/auth.py` would come back
    as three words. This module needs both views, so it keeps the path
    characters here and flattens them again in `_words` for the cue match.
    """
    folded = unicodedata.normalize("NFKD", str(text or "")).lower()
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    return folded.replace("â€™", "").replace("'", "")


def _words(clause: str) -> str:
    """The clause as ` word word `, for a cue match that cannot land mid-word."""
    flat = re.sub(r"[^a-z0-9]+", " ", clause).strip()
    return f" {flat} " if flat else ""


def _cue_present(padded: str, cue: str, *, guard_negation: bool) -> bool:
    needle = f" {cue} "
    at = padded.find(needle)
    while at != -1:
        if not guard_negation:
            return True
        preceding = padded[:at + 1].split()
        if not preceding or preceding[-1] not in _NEGATIONS:
            return True
        at = padded.find(needle, at + 1)
    return False


def _read_scope(instruction: str, workspace: str = "") -> Tuple[
        Tuple[str, ...], Tuple[str, ...], Tuple[str, ...]]:
    """`(only, protected, unread)` -- the scope half of a sentence.

    Clause by clause, because "arregla el login, sin tocar settings.py" carries
    two different instructions and reading the whole sentence at once would
    apply the second one's object to the first one's verb. A clause that
    carries a cue but names nothing this module can read as a resource is
    returned in `unread`, LITERALLY, for `ambiguities`: Â§6's compiler is not
    allowed to guess which file "esta linea" meant.
    """
    only: List[str] = []
    guarded: List[str] = []
    unread: List[str] = []
    for clause in _CLAUSE_RE.split(_fold(instruction)):
        clause = clause.strip()
        if not clause:
            continue
        padded = _words(clause)
        keeps = any(_cue_present(padded, cue, guard_negation=False)
                    for cue in _KEEP_CUES)
        onlys = any(_cue_present(padded, cue, guard_negation=True)
                    for cue in _ONLY_CUES)
        if not keeps and not onlys:
            continue
        found = resources_named(clause, workspace)
        if not found:
            unread.append(f"unread scope phrase: {clause[:200]}")
            continue
        target = guarded if keeps else only
        for resource in found:
            if resource not in target:
                target.append(resource)
    return tuple(only), tuple(guarded), tuple(unread)


def _intersect_resources(current: Sequence[str],
                         wanted: Sequence[str]) -> Tuple[Tuple[str, ...], str]:
    """A containment-aware intersection, and a note when it could not be made.

    `src` narrowed to `src/auth.py` is `src/auth.py`, not the empty set. The
    plain string intersection that `ScopeEnvelope.narrow` performs collapses
    exactly that case to `()` -- and an empty `allowed_resources` is read by
    `covers()` as "no working area was named", i.e. EVERYTHING. A narrowing
    that widens is the one outcome Â§1.12.1 says cannot happen, so this module
    does its own intersection and does not call `narrow(resources=...)`.
    See `tests/test_completion_engine_scope.py::test_frozen_narrow_widens...`.

    When nothing survives, the current list is kept and the caller is handed a
    note. Refusing to narrow is safe -- the run stays inside the area it
    already had -- while narrowing to nothing would open the whole tree.
    """
    keep_current = tuple(dict.fromkeys(_norm(r) for r in current if _norm(r)))
    keep_wanted = tuple(dict.fromkeys(_norm(r) for r in wanted if _norm(r)))
    if not keep_wanted:
        return keep_current, ""
    if not keep_current:
        return keep_wanted, ""
    out: List[str] = [w for w in keep_wanted
                      if any(_within(w, c) for c in keep_current)]
    out += [c for c in keep_current
            if any(_within(c, w) for w in keep_wanted) and c not in out]
    if not out:
        return keep_current, ("scope phrase named {} , which is outside this "
                              "envelope; not narrowed".format(list(keep_wanted)))
    return tuple(out), ""


def narrow_from_instruction(envelope: ScopeEnvelope,
                            instruction: str) -> ScopeEnvelope:
    """A strictly smaller envelope, read out of one sentence. It never widens.

    Two readings, kept apart on purpose:

    * the MODE, from `agent_profiles.completion.from_instruction` -- which
      already handles Spanish and English, already refuses an ambiguous
      sentence, and is NOT reimplemented here. Its answer is turned into
      relations and intersected, so a sentence asking for a wider mode than the
      envelope already allows changes nothing.
    * the SCOPE, read here: "solo X" narrows the working area, "sin tocar Y"
      adds Y to the protected list. Both are intersections or additions to a
      deny list, and neither can reach a resource the envelope did not already
      hold.

    Everything this reader cannot resolve lands in `ambiguities`, literally.
    Â§6's compiler produces a boundary or says it could not; it does not produce
    a plausible boundary, because a plausible boundary is indistinguishable
    from a correct one at the moment it is written and not afterwards.
    """
    mode = from_instruction(instruction)
    relations = tuple(envelope.allowed_relations)
    notes: List[str] = []
    if mode:
        wanted = _relations_for(policy_for(mode))
        narrowed = tuple(r for r in relations if r in set(wanted))
        if narrowed and narrowed != relations:
            relations = narrowed
            notes.append(f"narrowed to `{mode}` relations by instruction")

    only, guarded, unread = _read_scope(instruction)
    resources, refusal = _intersect_resources(envelope.allowed_resources, only)
    if refusal:
        notes.append(refusal)
    elif only and tuple(resources) != tuple(envelope.allowed_resources):
        notes.append(f"narrowed to {list(resources)} by instruction")

    protected = list(envelope.protected_resources)
    for resource in guarded:
        if resource not in protected:
            protected.append(resource)
            notes.append(f"protected `{resource}` by instruction")
    notes.extend(unread)

    payload = envelope.to_dict()
    payload["allowed_resources"] = list(resources)
    payload["protected_resources"] = protected[:512]
    payload["allowed_relations"] = list(relations) or ["direct"]
    payload["ambiguities"] = list(dict.fromkeys(
        list(envelope.ambiguities) + [n[:500] for n in notes]))[:256]
    return ScopeEnvelope.parse(payload, "scope")


# -- distance to the goal --------------------------------------------------


def _same_component(a: str, b: str) -> bool:
    """Same directory, or a module and the package that replaced it.

    `src/auth.py` and `src/auth/session.py` are the same component through the
    second clause: a module that grew into a package keeps its place in the
    tree, and losing that would make every such split look like a jump to an
    unrelated area of the repository.
    """
    da, db = _dirname(a), _dirname(b)
    if da == db:
        return True
    return da == _stem_path(b) or db == _stem_path(a)


def _is_test_of(target: str, source: str) -> bool:
    """Whether `target` is, by filename convention, the test for `source`.

    `test_x.py`, `x_test.go`, `x.test.js`, `x.spec.ts`. Conventions, which are
    structure -- the same three the project's own `project_tests.py` uses to
    find related tests. Nothing here reads what is inside either file.
    """
    tb, sb = _basename(target), _basename(source)
    ts, ss = _stem(tb), _stem(sb)
    if not ss or not ts or ts == ss:
        return False
    return (ts == f"test_{ss}" or ts == f"{ss}_test"
            or tb.startswith(f"{ss}.test.") or tb.startswith(f"{ss}.spec."))


def _same_pattern(target: str, source: str) -> bool:
    """The same file, somewhere else: `apps/a/views.py` beside `apps/b/views.py`.

    Basename equality across different directories is a structural fact about
    the tree. It is the only `similar_case` signal this function has, and Â§13
    is why there is not a second one guessed from identifiers.
    """
    return (_basename(target) == _basename(source)
            and _dirname(target) != _dirname(source))


def relation_of(resource: str, envelope: ScopeEnvelope, *,
                touched: Sequence[str] = (),
                goal_terms: Sequence[str] = ()) -> str:
    """How far `resource` sits from what this run was asked to do. Â§6.

    Structural, and only structural: directories, stems and filename
    conventions. `goal_terms` are filtered through `_looks_like_resource`
    first, so a goal that names a function cannot promote a file to `direct`
    by sharing a word with it -- rule 3, and the reason Â§30 lists "no afirmar
    required sin relaciÃ³n demostrable" among the things this engine must not do.

    A protected resource is `unrelated` even when it sits inside
    `allowed_resources`. That is the case an allow list on its own gets wrong,
    and the answer has to be the FARTHEST relation rather than a special flag,
    because the frontier filters on relation and a flag is something a caller
    has to remember to read.

    When several relations apply -- a test living beside the file it tests is
    both `adjacent` and `downstream` -- the closest one wins, by the order of
    `RELATIONS`. Both are inside `GREEDY_RELATIONS`, so the choice changes what
    the closeout says and never what the run is allowed to touch.

    Â§6's other half of `downstream`, "something that imports it", is not
    answered here: it needs an import graph this function is not given, and
    inferring it from names is exactly what rule 3 forbids. A caller that has
    the graph passes the importer in `touched` or classifies it itself.
    """
    target = _norm(resource)
    if not target:
        return "unrelated"
    if envelope.protects(target) or not envelope.covers(target):
        return "unrelated"

    named = {_norm(t) for t in goal_terms if _looks_like_resource(t)}
    touched_paths = [_norm(t) for t in touched if _norm(t)]
    marks: List[str] = []
    if target in touched_paths or target in named:
        marks.append("direct")
    for other in touched_paths:
        if other == target:
            continue
        if _same_component(target, other):
            marks.append("adjacent")
        if _is_test_of(target, other):
            marks.append("downstream")
        if _same_pattern(target, other):
            marks.append("similar_case")
    if not marks:
        return "opportunistic"
    return sorted(set(marks), key=relation_rank)[0]


# -- may this candidate run at all -----------------------------------------


def admits(candidate: ImprovementCandidate, envelope: ScopeEnvelope, *,
           permissions: Any = None) -> Tuple[bool, str]:
    """`(ok, reason)`. Permission first, value never. Rule 1.

    The order is fixed and it is the whole point of the function:

        permissions -> forbidden effects -> protected resources
                    -> working area -> relation

    A candidate that fails the first gate is refused with `no_permission` and
    is never scored. Nothing in this function reads `expected_value`,
    `estimated_cost`, `risk` or `confidence`, and a test asserts that by
    reading this module's own syntax tree rather than by observing behaviour --
    behaviour can be right today and a new line can make it wrong tomorrow.

    `permissions=None` means no envelope was supplied, which is NOT "everything
    is allowed": a candidate that needs a tool or an effect is refused, because
    nobody said the run holds it. A candidate that needs nothing passes, which
    is the ordinary read-only case and must not be refused into silence.

    A candidate's `resources` are only checked against `work_roots` when they
    are absolute. `PermissionEnvelope.work_roots` holds absolute filesystem
    roots and a candidate's resources are workspace-relative, so asking
    `permits_root` about a relative path would answer `False` for every
    candidate in every run -- and a check that always fails is not a check,
    it is a way of never running anything and never knowing why.
    """
    if permissions is None:
        if candidate.required_permissions or candidate.required_effects:
            return False, "no_permission"
    else:
        for tool in candidate.required_permissions:
            if not permissions.permits(tool):
                return False, "no_permission"
        for effect in candidate.required_effects:
            if not permissions.permits_effect(effect):
                return False, "no_permission"
        for resource in candidate.resources:
            if _is_absolute(resource) and not permissions.permits_root(resource):
                return False, "no_permission"

    for effect in candidate.required_effects:
        if not envelope.permits_effect(effect):
            return False, "forbidden_effect"
    for resource in candidate.resources:
        if envelope.protects(resource):
            return False, "out_of_scope"
    for resource in candidate.resources:
        if not envelope.covers(resource):
            return False, "out_of_scope"
    if not envelope.permits_relation(candidate.relation):
        return False, "out_of_scope"
    return True, ""


def describe(envelope: ScopeEnvelope) -> str:
    """One line a receipt can carry. Says what is BOUNDED and what is not.

    "anywhere" is printed in full rather than left as an empty list, because an
    empty `allowed_resources` is the widest an envelope gets and a reader
    skimming a receipt would otherwise read the emptiest line as the safest one.
    """
    area = ", ".join(envelope.allowed_resources) if envelope.allowed_resources \
        else "anywhere the permissions reach"
    parts = [f"{envelope.id}: {area}"]
    if envelope.protected_resources:
        parts.append("protected " + ", ".join(envelope.protected_resources))
    parts.append("relations " + "+".join(envelope.allowed_relations))
    if envelope.allowed_effects:
        parts.append("effects " + "+".join(envelope.allowed_effects))
    if envelope.forbidden_effects:
        parts.append(f"{len(envelope.forbidden_effects)} effect(s) denied")
    if envelope.ambiguities:
        parts.append(f"{len(envelope.ambiguities)} ambiguity(ies)")
    return "; ".join(parts)
