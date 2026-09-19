"""src/bench/memory_recall.py — deterministic regression benchmark for
MEMORY RECALL.

Unlike ``src/bench/suites.py``/``runner.py`` (which benchmark raw LLM
output), this module never calls a model. It exercises the real recall
path a change to ``src/memory_engine.py``, ``src/memory_curator.py``,
``src/memory_conflicts.py``, ``src/two_tier_search.py`` or the memory
vector store would move: a fixed, hand-authored corpus is loaded into an
isolated ``memory_engine`` (its own temp SQLite file, never the app's real
``DATA_DIR``), a fixed set of queries is run through
``memory_engine.search()``, and the ranked output is scored against each
query's expected/forbidden item keys.

Determinism
-----------
Everything that could vary between two runs of the same code is pinned:

* **Data dir**: ``memory_engine.DATA_DIR`` is pointed at a fresh
  ``tempfile.mkdtemp()`` for the run and restored afterwards — the module
  comment on ``memory_engine.DATA_DIR`` says exactly this is what that
  seam is for ("Module-level so tests can point the store somewhere
  disposable").
* **Item ids**: ``memory_engine.add_item`` generates a random ``uuid4``
  per item, and ``search()`` breaks score ties on the id string. Two runs
  with different random ids could therefore reorder a tie differently.
  ``_deterministic_ids()`` patches ``memory_engine.uuid.uuid4`` to a plain
  counter for the duration of corpus loading, so ids — and every tie-break
  built on them — are identical every run.
* **Clock**: every item's ``created_at``/``updated_at``/``valid_from`` is
  authored as an offset in days from a fixed ``ANCHOR`` timestamp (never
  ``datetime.utcnow()``), and every ``search()`` call in this module is
  given ``now=ANCHOR`` explicitly — freshness decay and the
  valid-until/valid-from window are then a pure function of the corpus,
  never of wall-clock time.
* **Embeddings — "auto" means the hash fallback, always.** A real
  embedding model (fastembed/Chroma) is not guaranteed to be present or
  network-reachable, and even when it is, its output is a moving target
  this suite cannot pin. So this suite never touches
  ``memory_engine.vector_store()``'s own auto-detect path at all: it
  installs a tiny in-process store (``_HashVectorStore``) built on
  :mod:`src.hash_embed` — the repo's documented, model-free, no-network
  fallback embedder — via ``memory_engine.set_vector_store()``, the exact
  seam the engine already exposes for this. The semantic lane is therefore
  genuinely exercised (not skipped/degraded-to-lexical-only), and it is
  100% reproducible across machines. ``embedder="auto"`` and
  ``embedder="hash"`` both resolve to this; no other value is accepted.
  **Header note for the record: this environment's real embedding model
  is not used by this suite by design (see above), so every reported
  number here is the hash-fallback number, not a real-embedder number.**

Corpus
------
``CORPUS`` (~58 items) and ``QUERIES`` (~36 queries) below are the fixed
regression fixture: preferences, facts, project conventions and
decisions, three near-duplicate pairs, three contradictions resolved by
recency (the older item is authored ``status="deprecated"``, exactly the
outcome memory_conflicts resolution is expected to produce, and MUST stay
out of every result), four items expired via ``valid_until`` (must stay
out of every result), six items belonging to a different owner/project
that must never leak into another owner's results, and a handful of
Spanish items/queries mixed with the English ones — the same mixed-
language corpus a real user's memory store looks like.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import statistics
import tempfile
import time
import uuid as _uuid_mod
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from src import hash_embed
from src import memory_engine

__all__ = [
    "ANCHOR", "CORPUS", "QUERIES", "run", "compare", "score_ranked",
]

# A fixed instant, never wall-clock time — see module docstring.
ANCHOR = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)

OWNER = "alice"
PROJECT = "atlas"
OTHER_OWNER = "bob"
OTHER_PROJECT = "zephyr"


def _dt(days_offset: float) -> datetime:
    return ANCHOR + timedelta(days=days_offset)


# ---------------------------------------------------------------------------
# The fixed corpus. Each row: key (stable test handle), text, owner, project,
# level, type, status, trust_class, created_offset (days from ANCHOR, so
# negative = older), valid_until_offset (days from ANCHOR; None = never
# expires).
# ---------------------------------------------------------------------------

CORPUS: Tuple[Dict[str, Any], ...] = (
    # -- preferences (owner-scoped) -----------------------------------------
    dict(key="pref_editor", text="Alice prefers using Neovim as her code editor.",
         owner=OWNER, project="", type="preference"),
    dict(key="pref_theme", text="Alice prefers dark mode in all applications.",
         owner=OWNER, project="", type="preference"),
    dict(key="pref_lang_response", text="Alice prefers responses in English by default.",
         owner=OWNER, project="", type="preference"),
    dict(key="pref_commit_style", text="Alice prefers concise, present-tense commit messages.",
         owner=OWNER, project=PROJECT, type="preference"),
    dict(key="pref_notifications", text="Alice does not want push notifications after 9pm.",
         owner=OWNER, project="", type="preference"),
    dict(key="pref_indent", text="Alice prefers 2-space indentation for JavaScript and TypeScript.",
         owner=OWNER, project=PROJECT, type="preference"),
    dict(key="pref_test_runner", text="Alice prefers pytest over unittest for Python tests.",
         owner=OWNER, project=PROJECT, type="preference"),
    dict(key="pref_spanish", text="A Alice le gusta recibir resumenes cortos en espanol los viernes.",
         owner=OWNER, project="", type="preference"),

    # -- facts ----------------------------------------------------------------
    dict(key="fact_timezone", text="Alice's timezone is Europe/Madrid.",
         owner=OWNER, project=""),
    dict(key="fact_birthday", text="Alice's birthday is on March 3rd.",
         owner=OWNER, project=""),
    dict(key="fact_role", text="Alice is the lead backend engineer on the Atlas project.",
         owner=OWNER, project=PROJECT),
    dict(key="fact_company", text="Alice works at a company named Faustus Labs.",
         owner=OWNER, project=""),
    dict(key="fact_stack", text="The Atlas project's backend is written in Python with FastAPI.",
         owner=OWNER, project=PROJECT),
    dict(key="fact_db", text="The Atlas project uses PostgreSQL as its primary database.",
         owner=OWNER, project=PROJECT),
    dict(key="fact_family_es", text="Alice tiene dos hijos y vive en Madrid.",
         owner=OWNER, project=""),
    dict(key="fact_manager", text="Alice's manager is named Diana.",
         owner=OWNER, project=""),

    # -- project conventions (procedural: no time decay) ----------------------
    dict(key="conv_branch", text="Atlas branch names must follow the pattern feature/<ticket-id>-slug.",
         owner=OWNER, project=PROJECT, type="procedure", level="procedural"),
    dict(key="conv_pr_review", text="Every pull request on Atlas needs at least one approving review before merge.",
         owner=OWNER, project=PROJECT, type="procedure", level="procedural"),
    dict(key="conv_lint", text="Atlas uses ruff for Python linting with line length 100.",
         owner=OWNER, project=PROJECT, type="procedure", level="procedural"),
    dict(key="conv_test_cov", text="Atlas requires at least 80% test coverage on new modules.",
         owner=OWNER, project=PROJECT, type="procedure", level="procedural"),
    dict(key="conv_commit_lang", text="Atlas commit messages must be written in English.",
         owner=OWNER, project=PROJECT, type="procedure", level="procedural"),
    dict(key="conv_secrets", text="Atlas secrets are stored in the encrypted vault, never in .env files committed to git.",
         owner=OWNER, project=PROJECT, type="procedure", level="procedural"),
    dict(key="conv_deploy_window", text="Atlas deployments to production only happen on weekdays before 4pm.",
         owner=OWNER, project=PROJECT, type="procedure", level="procedural"),
    dict(key="es_conv_standup", text="El equipo de Atlas hace standup todos los dias laborables a las 9:30.",
         owner=OWNER, project=PROJECT, type="procedure", level="procedural"),

    # -- decisions --------------------------------------------------------------
    dict(key="dec_db_choice", text="The Atlas team decided to use PostgreSQL over MySQL for better JSON support.",
         owner=OWNER, project=PROJECT, type="decision"),
    dict(key="dec_queue", text="The Atlas team decided to use Redis as the task queue instead of RabbitMQ.",
         owner=OWNER, project=PROJECT, type="decision"),
    dict(key="dec_auth", text="The Atlas team decided to use JWT tokens for API authentication.",
         owner=OWNER, project=PROJECT, type="decision"),
    dict(key="dec_deploy", text="The Atlas team decided to deploy on a single VPS instead of Kubernetes for now.",
         owner=OWNER, project=PROJECT, type="decision"),
    dict(key="dec_frontend", text="The Atlas team decided to build the frontend with React instead of Vue.",
         owner=OWNER, project=PROJECT, type="decision"),
    dict(key="dec_monitoring", text="The Atlas team decided to use Grafana and Prometheus for monitoring.",
         owner=OWNER, project=PROJECT, type="decision"),
    dict(key="dec_monorepo_es", text="El equipo de Atlas decidio usar un monorepo en lugar de repositorios separados.",
         owner=OWNER, project=PROJECT, type="decision"),

    # -- near-duplicate pairs ----------------------------------------------------
    dict(key="dup_db_a", text="Atlas's primary database is PostgreSQL.",
         owner=OWNER, project=PROJECT),
    dict(key="dup_db_b", text="The main database for the Atlas project is Postgres.",
         owner=OWNER, project=PROJECT),
    dict(key="dup_standup_a", text="The Atlas team holds a daily standup meeting at 9:30am.",
         owner=OWNER, project=PROJECT),
    dict(key="dup_standup_b", text="Atlas has a daily 9:30am standup with the whole team.",
         owner=OWNER, project=PROJECT),
    dict(key="dup_review_a", text="Atlas code reviews should be completed within 24 hours.",
         owner=OWNER, project=PROJECT),
    dict(key="dup_review_b", text="Reviewers on Atlas are expected to review pull requests within one day.",
         owner=OWNER, project=PROJECT),

    # -- contradictions resolved by recency (older is deprecated) ---------------
    dict(key="contra_old_hosting", text="The Atlas project is hosted on AWS.",
         owner=OWNER, project=PROJECT, status="deprecated", created_offset=-200),
    dict(key="contra_new_hosting", text="The Atlas project is hosted on Hetzner Cloud (migrated off AWS).",
         owner=OWNER, project=PROJECT, created_offset=-5),
    dict(key="contra_old_lead", text="The Atlas backend lead is Carol.",
         owner=OWNER, project=PROJECT, status="deprecated", created_offset=-250),
    dict(key="contra_new_lead", text="The Atlas backend lead is now Alice (Carol moved to the mobile team).",
         owner=OWNER, project=PROJECT, created_offset=-3),
    dict(key="contra_old_style", text="Atlas used tabs for Python indentation.",
         owner=OWNER, project=PROJECT, status="deprecated", created_offset=-180,
         type="procedure", level="procedural"),
    dict(key="contra_new_style", text="Atlas now uses 4 spaces for Python indentation (switched from tabs).",
         owner=OWNER, project=PROJECT, created_offset=-2, type="procedure", level="procedural"),

    # -- expired via valid_until (must never be recalled) ------------------------
    dict(key="exp_promo", text="There is a 20% discount promo code valid through end of last month.",
         owner=OWNER, project="", level="episodic", valid_until_offset=-10),
    dict(key="exp_oncall", text="Alice is on call for the Atlas project this week.",
         owner=OWNER, project=PROJECT, level="episodic", valid_until_offset=-30),
    dict(key="exp_meeting", text="There is a project kickoff meeting scheduled for last quarter.",
         owner=OWNER, project=PROJECT, level="episodic", valid_until_offset=-90),
    dict(key="exp_trial", text="Alice's trial license for the profiler tool expires soon.",
         owner=OWNER, project="", level="episodic", valid_until_offset=-1),

    # -- another owner/project: must never leak into Alice/Atlas results --------
    dict(key="other_editor", text="Bob prefers using VS Code as his editor.",
         owner=OTHER_OWNER, project="", type="preference"),
    dict(key="other_db", text="The Zephyr project uses MongoDB as its database.",
         owner=OTHER_OWNER, project=OTHER_PROJECT),
    dict(key="other_conv_branch", text="Zephyr branch names follow the pattern bugfix/<id>.",
         owner=OTHER_OWNER, project=OTHER_PROJECT, type="procedure", level="procedural"),
    dict(key="other_dec_auth", text="The Zephyr team decided to use API keys instead of JWT for authentication.",
         owner=OTHER_OWNER, project=OTHER_PROJECT, type="decision"),
    dict(key="other_fact_role", text="Bob is the lead backend engineer on the Zephyr project.",
         owner=OTHER_OWNER, project=OTHER_PROJECT),
    dict(key="other_pref_theme", text="Bob prefers light mode in all applications.",
         owner=OTHER_OWNER, project="", type="preference"),

    # -- more Spanish/English mixed items -----------------------------------
    dict(key="es_fact_holiday", text="El proximo festivo en la oficina es el uno de mayo.",
         owner=OWNER, project=""),
    dict(key="es_pref_notify", text="Alice no quiere recibir notificaciones despues de las nueve de la noche.",
         owner=OWNER, project="", type="preference"),
    dict(key="fact_language_pref", text="Alice speaks both English and Spanish fluently.",
         owner=OWNER, project=""),
)

_CORPUS_KEYS = {row["key"] for row in CORPUS}
assert len(_CORPUS_KEYS) == len(CORPUS), "duplicate key in CORPUS"


def _keys(*keys: str) -> frozenset:
    unknown = set(keys) - _CORPUS_KEYS
    if unknown:
        raise AssertionError(f"query references unknown corpus key(s): {sorted(unknown)}")
    return frozenset(keys)


# ---------------------------------------------------------------------------
# The fixed query set. Each row: key, query text, owner/project the query is
# asked from, expected (item keys a good recall MUST surface — empty means
# this is a pure negative probe, scored only for forbidden-leak/latency),
# forbidden (item keys that must NEVER come back for this query).
# ---------------------------------------------------------------------------

QUERIES: Tuple[Dict[str, Any], ...] = (
    dict(key="q_editor", query="What code editor does Alice prefer?",
         owner=OWNER, project="", expected=_keys("pref_editor"), forbidden=_keys("other_editor")),
    dict(key="q_theme", query="Which theme does Alice like, dark or light?",
         owner=OWNER, project="", expected=_keys("pref_theme"), forbidden=_keys("other_pref_theme")),
    dict(key="q_notify", query="Does Alice want notifications late at night?",
         owner=OWNER, project="", expected=_keys("pref_notifications", "es_pref_notify")),
    dict(key="q_indent", query="How many spaces does Alice use for JS indentation?",
         owner=OWNER, project=PROJECT, expected=_keys("pref_indent")),
    dict(key="q_test_runner", query="What test runner does Alice use for Python?",
         owner=OWNER, project=PROJECT, expected=_keys("pref_test_runner")),
    dict(key="q_resumen_es", query="Que resumen le gusta recibir a Alice los viernes?",
         owner=OWNER, project="", expected=_keys("pref_spanish")),
    dict(key="q_timezone", query="What is Alice's timezone?",
         owner=OWNER, project="", expected=_keys("fact_timezone")),
    dict(key="q_birthday", query="When is Alice's birthday?",
         owner=OWNER, project="", expected=_keys("fact_birthday")),
    dict(key="q_company", query="What company does Alice work at?",
         owner=OWNER, project="", expected=_keys("fact_company")),
    dict(key="q_manager", query="Who is Alice's manager?",
         owner=OWNER, project="", expected=_keys("fact_manager")),
    dict(key="q_db", query="What database does the Atlas project use?",
         owner=OWNER, project=PROJECT,
         expected=_keys("fact_db", "dup_db_a", "dup_db_b", "dec_db_choice"),
         forbidden=_keys("other_db")),
    dict(key="q_stack", query="What backend framework and language does Atlas use?",
         owner=OWNER, project=PROJECT, expected=_keys("fact_stack")),
    dict(key="q_family_es", query="Cuantos hijos tiene Alice y donde vive?",
         owner=OWNER, project="", expected=_keys("fact_family_es")),
    dict(key="q_branch", query="What is the branch naming convention on Atlas?",
         owner=OWNER, project=PROJECT, expected=_keys("conv_branch"), forbidden=_keys("other_conv_branch")),
    dict(key="q_pr_review", query="How many approvals does a pull request need on Atlas?",
         owner=OWNER, project=PROJECT, expected=_keys("conv_pr_review")),
    dict(key="q_lint", query="What linter does Atlas use for Python and what line length?",
         owner=OWNER, project=PROJECT, expected=_keys("conv_lint")),
    dict(key="q_test_cov", query="What test coverage percentage does Atlas require?",
         owner=OWNER, project=PROJECT, expected=_keys("conv_test_cov")),
    dict(key="q_commit_lang", query="What language should Atlas commit messages be written in?",
         owner=OWNER, project=PROJECT, expected=_keys("conv_commit_lang")),
    dict(key="q_secrets", query="Where does Atlas store its secrets?",
         owner=OWNER, project=PROJECT, expected=_keys("conv_secrets")),
    dict(key="q_deploy_window", query="When does Atlas deploy to production?",
         owner=OWNER, project=PROJECT, expected=_keys("conv_deploy_window")),
    dict(key="q_db_choice_why", query="Why did the Atlas team choose PostgreSQL over MySQL?",
         owner=OWNER, project=PROJECT, expected=_keys("dec_db_choice")),
    dict(key="q_queue", query="What task queue did the Atlas team decide to use?",
         owner=OWNER, project=PROJECT, expected=_keys("dec_queue")),
    dict(key="q_auth", query="How does Atlas authenticate API requests?",
         owner=OWNER, project=PROJECT, expected=_keys("dec_auth"), forbidden=_keys("other_dec_auth")),
    dict(key="q_k8s", query="Does Atlas deploy on Kubernetes?",
         owner=OWNER, project=PROJECT, expected=_keys("dec_deploy")),
    dict(key="q_frontend", query="Which frontend framework did Atlas choose, React or Vue?",
         owner=OWNER, project=PROJECT, expected=_keys("dec_frontend")),
    dict(key="q_monorepo_es", query="Por que decidio el equipo de Atlas usar un monorepo?",
         owner=OWNER, project=PROJECT, expected=_keys("dec_monorepo_es")),
    dict(key="q_monitoring", query="What monitoring stack does Atlas use?",
         owner=OWNER, project=PROJECT, expected=_keys("dec_monitoring")),
    dict(key="q_standup", query="What time does the Atlas team's daily standup happen?",
         owner=OWNER, project=PROJECT,
         expected=_keys("dup_standup_a", "dup_standup_b", "es_conv_standup")),
    dict(key="q_review_turnaround", query="How quickly should Atlas pull requests be reviewed?",
         owner=OWNER, project=PROJECT, expected=_keys("dup_review_a", "dup_review_b")),
    dict(key="q_hosting_now", query="Where is the Atlas project hosted right now?",
         owner=OWNER, project=PROJECT, expected=_keys("contra_new_hosting"),
         forbidden=_keys("contra_old_hosting")),
    dict(key="q_lead_now", query="Who is the current Atlas backend lead?",
         owner=OWNER, project=PROJECT, expected=_keys("contra_new_lead"),
         forbidden=_keys("contra_old_lead")),
    dict(key="q_indent_now", query="How does Atlas indent Python code now?",
         owner=OWNER, project=PROJECT, expected=_keys("contra_new_style"),
         forbidden=_keys("contra_old_style")),
    dict(key="q_oncall_probe", query="Is Alice on call for Atlas this week?",
         owner=OWNER, project=PROJECT, forbidden=_keys("exp_oncall")),
    dict(key="q_promo_probe", query="Is there a discount promo running right now?",
         owner=OWNER, project="", forbidden=_keys("exp_promo")),
    dict(key="q_cross_tenant_a", query="What database does Zephyr use?",
         owner=OWNER, project=PROJECT, forbidden=_keys("other_db")),
    dict(key="q_cross_tenant_b", query="What editor does Alice use?",
         owner=OTHER_OWNER, project=OTHER_PROJECT, forbidden=_keys("pref_editor")),
)


# ---------------------------------------------------------------------------
# Hash-fallback semantic lane: memory_engine's own vector_store() contract
# (`add(id, text)` / `remove(id)` / `search(query, k) -> [{"memory_id","score"}]`)
# implemented over src.hash_embed. See module docstring for why.
# ---------------------------------------------------------------------------

class _HashVectorStore:
    def __init__(self) -> None:
        self._vectors: Dict[str, List[float]] = {}

    def add(self, memory_id: Any, text: Any) -> None:
        self._vectors[str(memory_id)] = hash_embed.embed(text)

    def remove(self, memory_id: Any) -> None:
        self._vectors.pop(str(memory_id), None)

    def search(self, query: Any, k: int = 8) -> List[Dict[str, Any]]:
        query_vector = hash_embed.embed(query)
        if not any(query_vector):
            return []
        scored: List[Tuple[str, float]] = []
        for memory_id, vector in self._vectors.items():
            score = hash_embed.similarity(query_vector, vector)
            if score > 0.0:
                scored.append((memory_id, score))
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        limit = max(1, int(k or 8))
        return [{"memory_id": mid, "score": score} for mid, score in scored[:limit]]


@contextlib.contextmanager
def _deterministic_ids(seed: int = 0):
    """Patches ``memory_engine.uuid.uuid4`` to a plain counter for the
    duration of the block — see "Item ids" in the module docstring."""
    counter = {"n": seed}
    original = memory_engine.uuid.uuid4

    def _next_uuid() -> _uuid_mod.UUID:
        counter["n"] += 1
        return _uuid_mod.UUID(int=counter["n"])

    memory_engine.uuid.uuid4 = _next_uuid
    try:
        yield
    finally:
        memory_engine.uuid.uuid4 = original


@contextlib.contextmanager
def _isolated_engine():
    """Points ``memory_engine`` at a throwaway temp dir and a deterministic
    hash-embed vector store for the duration of the block, then restores
    everything — never touches the app's real data dir or vector store."""
    tmp_dir = tempfile.mkdtemp(prefix="faustus_bench_memory_recall_")
    original_data_dir = memory_engine.DATA_DIR
    try:
        memory_engine.DATA_DIR = tmp_dir
        memory_engine.set_vector_store(_HashVectorStore())
        yield tmp_dir
    finally:
        memory_engine.DATA_DIR = original_data_dir
        memory_engine.reset_vector_store()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _insert_corpus() -> Dict[str, str]:
    """Loads ``CORPUS`` into the (already isolated) engine; returns
    ``{corpus_key: item_id}``."""
    key_to_id: Dict[str, str] = {}
    with _deterministic_ids():
        for row in CORPUS:
            created_at = _dt(float(row.get("created_offset", 0)))
            valid_until_offset = row.get("valid_until_offset")
            valid_until = _dt(float(valid_until_offset)).isoformat() if valid_until_offset is not None else None
            item = memory_engine.add_item(
                row["text"],
                owner=row.get("owner", ""),
                project=row.get("project", ""),
                level=row.get("level", "semantic"),
                type=row.get("type"),
                trust_class=row.get("trust_class", "agent_assertion"),
                status=row.get("status", "active"),
                valid_until=valid_until,
                now=created_at,
            )
            key_to_id[row["key"]] = item["id"]
    return key_to_id


# ---------------------------------------------------------------------------
# Scoring — pure, no I/O, unit-testable on a fabricated ranked list. This is
# also what "inject a leaking fake recall function" exercises in the tests:
# a fake recall function's ranked-id output goes straight through this.
# ---------------------------------------------------------------------------

def score_ranked(ranked_ids: Sequence[str], expected_ids: Set[str],
                  forbidden_ids: Set[str], k_values: Sequence[int]) -> Dict[str, Any]:
    """Metrics for one query given its full ranked id list (best first).

    ``expected_ids`` empty means this query is a pure negative probe: it
    never contributes to hit@k/recall@k/MRR (``"scored"`` is False), but its
    forbidden ids are still checked.
    """
    ranked_ids = [str(i) for i in ranked_ids]
    leaked = [rid for rid in ranked_ids if rid in forbidden_ids]
    reciprocal_rank = 0.0
    for idx, rid in enumerate(ranked_ids, start=1):
        if rid in expected_ids:
            reciprocal_rank = 1.0 / idx
            break
    per_k: Dict[int, Dict[str, float]] = {}
    for k in k_values:
        top = set(ranked_ids[:k])
        hit = 1 if (expected_ids and (expected_ids & top)) else 0
        recall = (len(expected_ids & top) / len(expected_ids)) if expected_ids else 0.0
        per_k[k] = {"hit": hit, "recall": round(recall, 6)}
    return {
        "scored": bool(expected_ids),
        "leaked_ids": leaked,
        "leak_count": len(leaked),
        "reciprocal_rank": round(reciprocal_rank, 6),
        "per_k": per_k,
    }


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    idx = min(len(ordered) - 1, max(0, round(pct / 100.0 * (len(ordered) - 1))))
    return ordered[idx]


RecallFn = Callable[[str, str, str, int, datetime, Dict[str, str]], List[str]]


def _default_recall(query: str, owner: str, project: str, k: int,
                     now: datetime, key_to_id: Dict[str, str]) -> List[str]:
    """The production recall path: ``memory_engine.search()``. Not touched
    by ``key_to_id`` — it is only there so a test's fake recall function can
    look keys up without knowing the engine's internal ids."""
    del key_to_id
    results = memory_engine.search(query, owner=owner, project=project, k=k,
                                    now=now, touch_hits=False)
    return [str(row["id"]) for row in results]


def run(k_values: Sequence[int] = (1, 3, 5), embedder: str = "auto",
        recall_fn: Optional[RecallFn] = None) -> Dict[str, Any]:
    """Runs the full suite and returns a JSON-serialisable report.

    ``recall_fn(query, owner, project, k, now, key_to_id) -> [item_id, ...]``
    (ranked, best first) can replace the default engine-backed recall — this
    is the seam the tests use to inject a fake/leaking recall function
    without needing a fabricated ranked list by hand.
    """
    if embedder not in ("auto", "hash"):
        raise ValueError(
            "embedder must be 'auto' or 'hash' — this suite never uses a real "
            "embedding model, by design (see module docstring)"
        )
    k_values = tuple(sorted({int(k) for k in k_values}))
    if not k_values:
        raise ValueError("k_values must not be empty")
    max_k = max(k_values)
    leak_k = max(30, max_k)
    recall = recall_fn or _default_recall

    with _isolated_engine():
        key_to_id = _insert_corpus()
        id_to_key = {v: k for k, v in key_to_id.items()}

        per_query: List[Dict[str, Any]] = []
        latencies: List[float] = []
        eval_n = 0
        mrr_sum = 0.0
        leak_total = 0
        hit_sum = {k: 0 for k in k_values}
        recall_sum = {k: 0.0 for k in k_values}

        for q in QUERIES:
            expected_ids = {key_to_id[key] for key in q.get("expected", ())}
            forbidden_ids = {key_to_id[key] for key in q.get("forbidden", ())}

            started = time.perf_counter()
            ranked_ids = recall(q["query"], q.get("owner", ""), q.get("project", ""),
                                 leak_k, ANCHOR, key_to_id)
            latency_ms = (time.perf_counter() - started) * 1000.0
            latencies.append(latency_ms)

            scored = score_ranked(ranked_ids, expected_ids, forbidden_ids, k_values)
            leak_total += scored["leak_count"]
            if scored["scored"]:
                eval_n += 1
                mrr_sum += scored["reciprocal_rank"]
                for k in k_values:
                    hit_sum[k] += scored["per_k"][k]["hit"]
                    recall_sum[k] += scored["per_k"][k]["recall"]

            per_query.append({
                "key": q["key"],
                "query": q["query"],
                "owner": q.get("owner", ""),
                "project": q.get("project", ""),
                "expected": sorted(q.get("expected", ())),
                "forbidden": sorted(q.get("forbidden", ())),
                "ranked_keys": [id_to_key.get(rid, rid) for rid in ranked_ids[:max_k]],
                "leaked_keys": [id_to_key.get(rid, rid) for rid in scored["leaked_ids"]],
                "reciprocal_rank": scored["reciprocal_rank"],
                "latency_ms": round(latency_ms, 3),
                "per_k": {str(k): v for k, v in scored["per_k"].items()},
            })

        aggregate = {
            "n_queries": len(QUERIES),
            "n_scored_queries": eval_n,
            "mrr": round(mrr_sum / eval_n, 6) if eval_n else 0.0,
            "forbidden_leak_count": leak_total,
            "latency_p50_ms": round(_percentile(latencies, 50), 3),
            "latency_p95_ms": round(_percentile(latencies, 95), 3),
            "per_k": {
                str(k): {
                    "hit_at_k": round(hit_sum[k] / eval_n, 6) if eval_n else 0.0,
                    "recall_at_k": round(recall_sum[k] / eval_n, 6) if eval_n else 0.0,
                }
                for k in k_values
            },
        }

    return {
        "suite": "memory_recall",
        "embedder": "hash_fallback",
        "embedder_note": (
            "no real embedding model is used by this suite by design; the "
            "semantic lane is src.hash_embed, deterministic and offline "
            "(see module docstring)"
        ),
        "k_values": list(k_values),
        "corpus_size": len(CORPUS),
        "aggregate": aggregate,
        "per_query": per_query,
    }


def compare(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    """Pure: deltas of ``b``'s aggregate metrics relative to ``a``'s. Never
    mutates either input, never touches the engine."""
    agg_a = a.get("aggregate", {}) or {}
    agg_b = b.get("aggregate", {}) or {}
    keys = sorted(set(agg_a.get("per_k", {})) | set(agg_b.get("per_k", {})),
                  key=lambda x: int(x))
    per_k_delta = {}
    for key in keys:
        pa = agg_a.get("per_k", {}).get(key, {})
        pb = agg_b.get("per_k", {}).get(key, {})
        per_k_delta[key] = {
            "hit_at_k_delta": round(pb.get("hit_at_k", 0.0) - pa.get("hit_at_k", 0.0), 6),
            "recall_at_k_delta": round(pb.get("recall_at_k", 0.0) - pa.get("recall_at_k", 0.0), 6),
        }
    return {
        "mrr_delta": round(agg_b.get("mrr", 0.0) - agg_a.get("mrr", 0.0), 6),
        "forbidden_leak_delta": agg_b.get("forbidden_leak_count", 0) - agg_a.get("forbidden_leak_count", 0),
        "latency_p50_ms_delta": round(agg_b.get("latency_p50_ms", 0.0) - agg_a.get("latency_p50_ms", 0.0), 3),
        "latency_p95_ms_delta": round(agg_b.get("latency_p95_ms", 0.0) - agg_a.get("latency_p95_ms", 0.0), 3),
        "per_k": per_k_delta,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_report(report: Dict[str, Any]) -> None:
    agg = report.get("aggregate", {})
    print(f"memory_recall — embedder={report.get('embedder')} "
          f"corpus={report.get('corpus_size')} "
          f"queries={agg.get('n_queries')} (scored={agg.get('n_scored_queries')})")
    print(f"{'k':>3}  {'hit@k':>8}  {'recall@k':>10}")
    for k in report.get("k_values", []):
        row = agg.get("per_k", {}).get(str(k), {})
        print(f"{k:>3}  {row.get('hit_at_k', 0.0):>8.3f}  {row.get('recall_at_k', 0.0):>10.3f}")
    print(f"MRR: {agg.get('mrr', 0.0):.3f}")
    print(f"forbidden leaks: {agg.get('forbidden_leak_count', 0)}")
    print(f"latency p50/p95 ms: {agg.get('latency_p50_ms', 0.0):.3f} / {agg.get('latency_p95_ms', 0.0):.3f}")


def _print_compare(delta: Dict[str, Any]) -> None:
    print(f"{'k':>3}  {'d hit@k':>10}  {'d recall@k':>12}")
    for k, row in delta.get("per_k", {}).items():
        print(f"{k:>3}  {row.get('hit_at_k_delta', 0.0):>+10.3f}  {row.get('recall_at_k_delta', 0.0):>+12.3f}")
    print(f"d MRR: {delta.get('mrr_delta', 0.0):+.3f}")
    print(f"d forbidden leaks: {delta.get('forbidden_leak_delta', 0):+d}")
    print(f"d latency p50/p95 ms: {delta.get('latency_p50_ms_delta', 0.0):+.3f} / "
          f"{delta.get('latency_p95_ms_delta', 0.0):+.3f}")


def _save_under_data_dir(report: Dict[str, Any]) -> Optional[str]:
    """Follows ``src/bench/runner.py``'s own convention
    (``DATA_DIR/benchmarks/<name>.json``, atomic write)."""
    try:
        import os
        from core.atomic_io import atomic_write_json
        from src.constants import DATA_DIR

        out_dir = os.path.join(DATA_DIR, "benchmarks", "memory_recall")
        os.makedirs(out_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = os.path.join(out_dir, f"{stamp}.json")
        atomic_write_json(path, report)
        return path
    except Exception:  # noqa: BLE001 - saving a copy is a convenience, never fatal
        return None


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Deterministic memory-recall benchmark")
    parser.add_argument("--json", help="write the full report as JSON to this path")
    parser.add_argument("--k", default="1,3,5", help="comma-separated k values (default: 1,3,5)")
    parser.add_argument("--embedder", default="auto", choices=("auto", "hash"))
    parser.add_argument("--compare", nargs=2, metavar=("A_JSON", "B_JSON"),
                         help="compare two previously saved reports instead of running the suite")
    args = parser.parse_args(argv)

    if args.compare:
        with open(args.compare[0], "r", encoding="utf-8") as f:
            report_a = json.load(f)
        with open(args.compare[1], "r", encoding="utf-8") as f:
            report_b = json.load(f)
        delta = compare(report_a, report_b)
        _print_compare(delta)
        return 0

    k_values = tuple(int(part) for part in str(args.k).split(",") if part.strip())
    report = run(k_values=k_values, embedder=args.embedder)
    _print_report(report)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, sort_keys=True)
        print(f"wrote {args.json}")
    else:
        saved = _save_under_data_dir(report)
        if saved:
            print(f"saved {saved}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
