"""
skills_runtime/selector.py — hybrid per-turn skill selector (lot S).

`services.memory.skills.SkillsManager.get_relevant_skills` has always
ranked candidate skills by plain Jaccard token overlap between the user's
message and a skill's `name + description + when_to_use + tags +
procedure` text, plus a few boosts (whole-token tag match, a substring hit
on the description, confidence, prior use). That is precise when the
user's wording overlaps the skill's wording and blind the moment it
doesn't — "email my landlord" never matches a skill written as "send
correspondence to a property manager".

This module is a drop-in replacement, `select(...)`, returning the exact
same list-of-dict shape `get_relevant_skills` does (so `render_level1`,
the token budgets and `record_use` all keep working unmodified) but
ranking with three lanes plus an outcome prior:

  * **semantic** — cosine similarity between the query's embedding and a
    skill's own embedding of `name + description + when_to_use + tags`,
    via the app's local, zero-config embedder
    (`src.embedding_lanes._build_fastembed_client`). No network call, no
    API key. If the embedder can't load (missing dependency, corrupt
    cache, ...) the lane turns itself off for the rest of the process —
    logged once — and scoring falls back to lexical + trigger only.
  * **lexical** — the existing Jaccard scoring, factored out of
    `SkillsManager.get_relevant_skills` as `_lexical_score` so this module
    can score an arbitrary skill list without going through that method's
    own threshold filter.
  * **trigger** — token overlap between the query and the skill's explicit
    trigger sentence ("Use when ...", "Use for ...", "Trigger", "Cuando
    ...", "Úsala cuando ..."), extracted by `extract_trigger`.

An **outcome prior** (Laplace-smoothed from the usage sidecar's
`positive`/`negative` counters, `SkillsManager.record_outcome`) then
multiplies the combined score in [0.5, 1.5] — a skill that has recently
worked for this owner outranks an otherwise-equal one that hasn't.

Embedding is the only part of this pipeline that costs anything, so it is
cached twice: in memory keyed by `(owner, skill id, content sha1)` for the
life of the process, and on disk at `<DATA_DIR>/skills/_vectors.json` so a
restart doesn't re-embed a library that hasn't changed. The query itself
is embedded exactly once per `select()` call, never once per skill.

`skill_selector_mode` (`"hybrid"` default, `"lexical"`) is the kill
switch: `"lexical"` calls straight through to `get_relevant_skills` and
returns its result completely unmodified — byte-identical to the old
behaviour, including omitting the `_selector` diagnostics key this module
otherwise adds.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MODE", "DEFAULT_THRESHOLD", "DEFAULT_WEIGHTS",
    "extract_trigger", "select", "explain",
    "record_outcome_from_reaction", "reset_selector_state",
]

DEFAULT_MODE = "hybrid"
DEFAULT_THRESHOLD = 0.22
DEFAULT_WEIGHTS: Dict[str, float] = {"semantic": 0.55, "lexical": 0.3, "trigger": 0.15}

# ── in-memory embedding cache: (owner, skill_id, content_sha1) -> vector ───
_MEM_VECTORS: Dict[Tuple[str, str, str], List[float]] = {}
_embedder_client: Any = None
_embedder_failed: bool = False


def reset_selector_state() -> None:
    """Clear the module-level embedder client, the 'semantic lane is
    unavailable' latch, and the in-memory vector cache. Tests call this
    between cases so a fake/real embedder from one case never leaks into
    the next; production code has no reason to call it (a changed
    embedding backend restarts the process anyway)."""
    global _embedder_client, _embedder_failed
    _embedder_client = None
    _embedder_failed = False
    _MEM_VECTORS.clear()


# ── settings (read at call time, feature off if settings are unavailable) ──

def _get_mode() -> str:
    try:
        from src.settings import get_setting
        return str(get_setting("skill_selector_mode", DEFAULT_MODE) or DEFAULT_MODE).strip().lower()
    except Exception:
        return DEFAULT_MODE


def _get_threshold() -> float:
    try:
        from src.settings import get_setting
        return float(get_setting("skill_selector_threshold", DEFAULT_THRESHOLD))
    except Exception:
        return DEFAULT_THRESHOLD


def _get_weights() -> Dict[str, float]:
    try:
        from src.settings import get_setting
        w = get_setting("skill_selector_weights", DEFAULT_WEIGHTS)
        if isinstance(w, dict):
            return {
                "semantic": float(w.get("semantic", DEFAULT_WEIGHTS["semantic"])),
                "lexical": float(w.get("lexical", DEFAULT_WEIGHTS["lexical"])),
                "trigger": float(w.get("trigger", DEFAULT_WEIGHTS["trigger"])),
            }
    except Exception:
        pass
    return dict(DEFAULT_WEIGHTS)


# ── trigger lane ────────────────────────────────────────────────────────────

_TRIGGER_PREFIXES = (
    "use when", "use for", "trigger",
    "cuando", "úsala cuando", "usala cuando",
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def extract_trigger(text: str) -> str:
    """The first sentence in `text` that opens with an explicit trigger
    phrase ("Use when", "Use for", "Trigger", "Cuando", "Úsala cuando" /
    "Usala cuando" — en/es), returned verbatim (original casing, no
    trailing punctuation stripped beyond what the split already removed).
    Empty string when no sentence in `text` opens with one of those
    phrases — most skills' `description`/`when_to_use` don't, and that is
    a normal, valid answer, not an error.

    Also usable by the skill-import path: a skill whose `description`
    ends in a "Use when ..." clause but has no explicit `## When to Use`
    section can have this called on its description to fill `when_to_use`
    automatically.
    """
    if not text:
        return ""
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        s = sentence.strip()
        if not s:
            continue
        low = s.lower()
        if any(low.startswith(p) for p in _TRIGGER_PREFIXES):
            return s
    return ""


def _trigger_overlap(query_tokens: set, sk: Mapping[str, Any]) -> float:
    from services.memory.skills import _jaccard, _tokenize
    text = " ".join([str(sk.get("description") or ""), str(sk.get("when_to_use") or "")])
    trig = extract_trigger(text)
    if not trig:
        return 0.0
    return _jaccard(query_tokens, _tokenize(trig))


# ── semantic lane ────────────────────────────────────────────────────────────

def _get_embedder():
    """Build (once per process) and return the app's local FastEmbed client,
    or None if it's unavailable — logging the failure exactly once."""
    global _embedder_client, _embedder_failed
    if _embedder_failed:
        return None
    if _embedder_client is not None:
        return _embedder_client
    try:
        from src.embedding_lanes import _build_fastembed_client
        _embedder_client = _build_fastembed_client()
        return _embedder_client
    except Exception as e:  # noqa: BLE001 - any failure just turns the lane off
        _embedder_failed = True
        logger.warning(
            "skill selector: semantic lane unavailable (%s); falling back to "
            "lexical + trigger scoring for the rest of this process", e,
        )
        return None


def _encode(client: Any, texts: Sequence[str]) -> List[List[float]]:
    vecs = client.encode(list(texts), normalize_embeddings=True)
    return vecs.tolist() if hasattr(vecs, "tolist") else [list(v) for v in vecs]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))


def _skill_id(sk: Mapping[str, Any]) -> str:
    sid = sk.get("id") or sk.get("name")
    return str(sid) if sid else ""


def _embed_text_for_skill(sk: Mapping[str, Any]) -> str:
    return " ".join([
        str(sk.get("name") or ""),
        str(sk.get("description") or ""),
        str(sk.get("when_to_use") or ""),
        " ".join(str(t) for t in (sk.get("tags") or [])),
    ])


def _content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _vectors_path(data_dir: str) -> str:
    import os
    return os.path.join(data_dir, "skills", "_vectors.json")


def _load_vector_cache(data_dir: str) -> Dict[str, Dict[str, Any]]:
    import os
    path = _vectors_path(data_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_vector_cache(data_dir: str, cache: Dict[str, Dict[str, Any]]) -> None:
    try:
        from core.atomic_io import atomic_write_json
        atomic_write_json(_vectors_path(data_dir), cache)
    except Exception:
        logger.debug("skill selector: could not persist vector cache", exc_info=True)


def _semantic_scores(owner: Optional[str], query: str, skills: Sequence[Mapping[str, Any]],
                     client: Any) -> Dict[str, float]:
    """cosine similarity per skill id (0.0..1.0, clamped), using the
    (owner, skill id, content sha1) in-memory cache and the
    `<DATA_DIR>/skills/_vectors.json` disk cache so an unchanged skill is
    never re-embedded. `client` must already be non-None (callers check
    `_get_embedder()` once for the whole batch, not per skill)."""
    if not skills:
        return {}
    from src.constants import DATA_DIR

    disk_cache = _load_vector_cache(DATA_DIR)
    dirty = False
    vectors: Dict[str, List[float]] = {}
    to_embed_texts: List[str] = []
    to_embed_keys: List[Tuple[str, str]] = []  # (skill_id, content_hash)

    owner_key = owner or ""
    for sk in skills:
        sid = _skill_id(sk)
        if not sid:
            continue
        text = _embed_text_for_skill(sk)
        h = _content_hash(text)
        mem_key = (owner_key, sid, h)
        vec = _MEM_VECTORS.get(mem_key)
        if vec is None:
            disk_entry = disk_cache.get(f"{owner_key}::{sid}")
            if (isinstance(disk_entry, dict) and disk_entry.get("hash") == h
                    and isinstance(disk_entry.get("vector"), list)):
                vec = disk_entry["vector"]
                _MEM_VECTORS[mem_key] = vec
        if vec is None:
            to_embed_texts.append(text)
            to_embed_keys.append((sid, h))
        else:
            vectors[sid] = vec

    if to_embed_texts:
        try:
            encoded = _encode(client, to_embed_texts)
        except Exception as e:  # noqa: BLE001
            logger.warning("skill selector: embedding skills failed for this turn: %s", e)
            encoded = []
        for (sid, h), vec in zip(to_embed_keys, encoded):
            vectors[sid] = vec
            _MEM_VECTORS[(owner_key, sid, h)] = vec
            disk_cache[f"{owner_key}::{sid}"] = {"hash": h, "vector": vec}
            dirty = True

    if dirty:
        _save_vector_cache(DATA_DIR, disk_cache)
    if not vectors:
        return {}

    try:
        q_vec = _encode(client, [query])[0]
    except Exception as e:  # noqa: BLE001
        logger.warning("skill selector: embedding the query failed for this turn: %s", e)
        return {}
    return {sid: max(0.0, _cosine(q_vec, vec)) for sid, vec in vectors.items()}


# ── outcome prior ────────────────────────────────────────────────────────────

# Which skills were surfaced on the LAST turn of each session, so the user's
# next message can be scored as a reaction to them. Process-local and
# bounded: a restart simply forgets the last turn's list (no outcome is then
# recorded, which is the neutral case), and the table never grows past
# `_SURFACED_MAX` sessions.
_SURFACED: Dict[Tuple[str, str], List[str]] = {}
_SURFACED_MAX = 512


def remember_surfaced(session_id: Optional[str], owner: Optional[str], skill_ids: Sequence[str]) -> None:
    """Record the skills surfaced this turn (empty list = none) for `session_id`."""
    if not session_id:
        return
    key = (str(session_id), str(owner or ""))
    if key not in _SURFACED and len(_SURFACED) >= _SURFACED_MAX:
        _SURFACED.pop(next(iter(_SURFACED)))
    _SURFACED[key] = [str(s) for s in skill_ids if s]


def last_surfaced(session_id: Optional[str], owner: Optional[str]) -> List[str]:
    """The skills surfaced on the previous turn of `session_id` ([] if none/unknown)."""
    if not session_id:
        return []
    return list(_SURFACED.get((str(session_id), str(owner or "")), []))


def _outcome_prior(usage_entry: Mapping[str, Any]) -> float:
    """Laplace-smoothed positive rate `(positive+1)/(positive+negative+2)`
    (in [0, 1]) mapped onto a [0.5, 1.5] multiplier — no evidence at all
    (`positive == negative == 0`) gives exactly 1.0, i.e. no change to the
    combined score."""
    positive = int(usage_entry.get("positive", 0) or 0)
    negative = int(usage_entry.get("negative", 0) or 0)
    p = (positive + 1) / (positive + negative + 2)
    return 0.5 + p


def record_outcome_from_reaction(owner: Optional[str], skill_ids: Sequence[str],
                                 user_message: str) -> None:
    """For the skills surfaced on the PREVIOUS turn (`skill_ids`), classify
    the user's very next message (`user_message`) with
    `sleep_optimize.classify_reaction` and, only on a clear positive/
    negative read (neutral records nothing), bump each skill's outcome
    counters via `SkillsManager.record_outcome`. See `S_wiring.md` for
    where the integrator calls this (agent_loop, reading the previous
    assistant message's `metadata.surfaced_skills`)."""
    if not skill_ids:
        return
    try:
        from src.skills_runtime.sleep_optimize import classify_reaction
    except Exception:
        logger.debug("skill selector: classify_reaction unavailable", exc_info=True)
        return
    reaction = classify_reaction(user_message)
    if reaction not in ("positive", "negative"):
        return
    try:
        from services.memory.skills import SkillsManager
        from src.constants import DATA_DIR
        sm = SkillsManager(DATA_DIR)
    except Exception:
        logger.debug("skill selector: could not build SkillsManager", exc_info=True)
        return
    positive = reaction == "positive"
    for sid in skill_ids:
        if not sid:
            continue
        try:
            sm.record_outcome(str(sid), owner=owner, positive=positive)
        except Exception:
            logger.debug("skill selector: record_outcome failed for %s", sid, exc_info=True)


# ── hybrid ranking ───────────────────────────────────────────────────────────

def _select_hybrid(sm, owner: Optional[str], query: str, skills: List[Dict],
                   threshold: Optional[float], max_items: int,
                   min_confidence: float) -> List[Dict]:
    from services.memory.skills import _tokenize

    weights = _get_weights()
    thr = threshold if threshold is not None else _get_threshold()

    candidates = sm._filter_candidates(skills, min_confidence)
    if not candidates or not (query or "").strip():
        return []

    client = _get_embedder()
    if client is None:
        # Semantic lane off for the whole call: renormalize the remaining
        # two lanes so a strong lexical/trigger match isn't quietly capped
        # by the weight the (unavailable) semantic lane would have carried.
        denom = weights["semantic"] * 0 + weights["lexical"] + weights["trigger"]
        if denom > 0:
            eff_weights = {
                "semantic": 0.0,
                "lexical": weights["lexical"] / denom,
                "trigger": weights["trigger"] / denom,
            }
        else:
            eff_weights = {"semantic": 0.0, "lexical": 1.0, "trigger": 0.0}
        sem_scores: Dict[str, float] = {}
    else:
        eff_weights = weights
        sem_scores = _semantic_scores(owner, query, candidates, client)

    query_tokens = _tokenize(query)
    from services.memory.skills import _lexical_score

    usage = sm._load_usage()
    scored: List[Tuple[float, Dict]] = []
    for sk in candidates:
        sid = _skill_id(sk)
        sem = sem_scores.get(sid, 0.0)
        lex = _lexical_score(query, query_tokens, sk)
        trig = _trigger_overlap(query_tokens, sk)
        base = (eff_weights["semantic"] * sem + eff_weights["lexical"] * lex
                + eff_weights["trigger"] * trig)
        usage_entry = sm._usage_entry(usage, sk.get("name", ""), sk.get("owner") or owner)
        prior = _outcome_prior(usage_entry)
        score = base * prior
        if score >= thr:
            out = dict(sk)
            out["_selector"] = {
                "semantic": sem, "lexical": lex, "trigger": trig,
                "prior": prior, "score": score,
            }
            scored.append((score, out))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [sk for _, sk in scored[:max(0, int(max_items))]]


def select(
    sm,
    owner: Optional[str],
    query: str,
    *,
    skills: Optional[List[Dict]] = None,
    threshold: Optional[float] = None,
    max_items: int = 5,
    min_confidence: float = 0.0,
) -> List[Dict]:
    """Drop-in replacement for `SkillsManager.get_relevant_skills(query,
    skills, threshold, max_items, min_confidence)` returning the same
    list-of-dict shape.

    `sm` is the caller's `SkillsManager` instance (so this never opens a
    second one against a different data dir than the caller is using).
    `skills` defaults to `sm.load(owner=owner)` exactly like the old
    function's `self.load_all()` default did for the unfiltered case.

    Mode (`skill_selector_mode` setting) `"lexical"` calls straight
    through to `get_relevant_skills` and returns its result completely
    unmodified. Any other value (including the default, `"hybrid"`) ranks
    with the semantic + lexical + trigger lanes described in this module's
    docstring and adds a `_selector` diagnostics key to each returned
    dict.
    """
    if skills is None:
        skills = sm.load(owner=owner)
    if not (query or "").strip() or not skills:
        return []

    mode = _get_mode()
    if mode == "lexical":
        thr = threshold if threshold is not None else 0.3
        return sm.get_relevant_skills(
            query, skills=skills, threshold=thr,
            max_items=max_items, min_confidence=min_confidence,
        )
    return _select_hybrid(sm, owner, query, skills, threshold, max_items, min_confidence)


def explain(query: str, owner: Optional[str] = None, *, max_items: int = 20) -> List[Dict]:
    """Module-level entry point for `routes/skill_selector_routes.py`: rank
    every skill visible to `owner` against `query` with the full hybrid
    pipeline (ignoring `skill_selector_mode` — this is a diagnostics view,
    not the live selection path) and return the top `max_items` with their
    `_selector` breakdown, threshold-unfiltered."""
    from services.memory.skills import SkillsManager
    from src.constants import DATA_DIR

    sm = SkillsManager(DATA_DIR)
    skills = sm.load(owner=owner)
    return _select_hybrid(sm, owner, query, skills, 0.0, max_items, 0.0)
