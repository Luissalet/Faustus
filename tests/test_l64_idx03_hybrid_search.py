"""Lote 64 — IDX-03: measurable hybrid retrieval over the code index.

`src/context_engine/code_index.py::search` is the module actually reachable
by a real caller: `routes/context_engine_routes.py::GET /code-index/search`,
`src/context_engine/adapters/derived.py::CodeIndexSource` (context-packet
retrieval) and `mcp_servers/context_engine_server.py` all call it directly.
Before this closure it was purely lexical/AST — a literal-substring SQL
prefilter plus a hand-weighted name/path/text/freshness score (huecos.md:
"no la fusión BM25+embedding+símbolo con reranking que pide la aceptación
completa"). `search()` now fuses that SAME candidate pool, by Reciprocal
Rank Fusion, with `src.two_tier_search`'s BM25-lite + model-free
hash-embedding ranking, and accepts an optional real `embedder`/`reranker`
— the module's own docstring explains why candidates still come from the
literal-token prefilter rather than a wider semantic net: that is what keeps
"found even when semantic similarity fails" true by construction, not by
hope.

(`src/code_index.py` — the other, agent-tool-facing lexical index this lote
also owns — is a different module by design; see its own docstring. IDX-03's
acceptance text is about retrieval QUALITY over an already-found candidate
set, which is what the module above is actually wired to serve, so that is
where this closure lives.)
"""
import os

import pytest

from src.context_engine import code_index as ci
from src.context_engine import store


@pytest.fixture()
def ce_store(tmp_path):
    store.use_path(str(tmp_path / "ce.db"))
    try:
        yield
    finally:
        store.use_path(None)


def write(root, rel, text):
    path = os.path.join(root, *rel.split("/"))
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


# Three deliberately dissimilar modules: an invented identifier no embedder
# has training signal for, a Spanish-language domain function, and a plain
# English one sharing a token ("calcular"-free but money-flavoured) that a
# weak fusion could confuse with the Spanish module.
ZORBLAXX_PY = '''"""An invented subsystem."""


def zorblaxx_recompute_flux(seed: int) -> int:
    """Recompute the flux capacitor's invented internal state."""
    return seed * 37
'''

DESCUENTOS_PY = '''"""Cálculo de descuentos y envío — módulo en español."""


def calcular_descuento_envio(precio: float, cupon: str) -> float:
    """Aplica el descuento del cupón al coste de envío."""
    return precio * 0.9 if cupon else precio


def calcular_impuestos(precio: float) -> float:
    """Aplica el IVA al precio."""
    return precio * 1.21
'''

INVOICE_PY = '''"""Plain English billing module."""


def compute_invoice_total(items):
    """Sum every line item's price into a grand total."""
    return sum(item.price for item in items)
'''


@pytest.fixture()
def workspace(tmp_path):
    root = str(tmp_path / "repo")
    os.makedirs(root, exist_ok=True)
    write(root, "src/zorblaxx.py", ZORBLAXX_PY)
    write(root, "src/descuentos.py", DESCUENTOS_PY)
    write(root, "src/invoice.py", INVOICE_PY)
    ci.refresh(root)
    return root


# ── unusual (invented) identifier: no embedder has ever seen this word ──────

def test_unusual_invented_identifier_is_the_top_hit(ce_store, workspace):
    hits = ci.search("zorblaxx_recompute_flux", workspace=workspace)
    assert hits and hits[0]["path"] == "src/zorblaxx.py"
    assert hits[0]["qualname"] == "zorblaxx_recompute_flux"


def test_unusual_identifier_partial_query_still_finds_the_file(ce_store, workspace):
    """A PARTIAL query on the invented compound word must still surface the
    file — `_tokens`'s identifier-aware split feeds both the existing
    name/path/text scorer AND the new BM25/hash corpus."""
    hits = ci.search("zorblaxx flux", workspace=workspace)
    assert any(hit["path"] == "src/zorblaxx.py" for hit in hits)


# ── Spanish: shares no synonym with anything an embedder has training on ────

def test_spanish_query_finds_the_spanish_module_not_the_english_lookalike(ce_store, workspace):
    hits = ci.search("descuento envio", workspace=workspace)
    assert hits, "a Spanish query must find something"
    assert hits[0]["path"] == "src/descuentos.py"


def test_spanish_exact_qualname_query_is_the_top_hit(ce_store, workspace):
    hits = ci.search("calcular_descuento_envio", workspace=workspace)
    assert hits[0]["qualname"] == "calcular_descuento_envio"


def test_two_similar_spanish_symbols_are_disambiguated_by_the_exact_query(ce_store, workspace):
    """`calcular_descuento_envio` and `calcular_impuestos` share the token
    "calcular" (both would appear in the lexical prefilter's candidate
    pool); an EXACT query for one must still put it first."""
    hits = ci.search("calcular_impuestos", workspace=workspace)
    assert hits[0]["qualname"] == "calcular_impuestos"


# ── code: an English billing query must find the English module ───────────

def test_english_billing_query_finds_the_english_module(ce_store, workspace):
    hits = ci.search("compute invoice total", workspace=workspace)
    assert hits and hits[0]["path"] == "src/invoice.py"


# ── the fusion lanes actually ran, and are reported ─────────────────────────

def test_hits_report_which_retrieval_lanes_actually_contributed(ce_store, workspace):
    hits = ci.search("zorblaxx_recompute_flux", workspace=workspace)
    assert hits[0]["tier"] in ("lexical", "hybrid", "refined", "reranked")
    assert isinstance(hits[0]["lanes"], list) and hits[0]["lanes"]


def test_the_fusion_never_drops_a_lexically_found_candidate(ce_store, workspace):
    """RRF is a REORDERING of the same candidate pool `two_tier_search`
    contributes nothing new to recall — every symbol the old LIKE-based
    prefilter would have returned is still returned; fusion only changes
    the order."""
    hits = ci.search("calcular", workspace=workspace, k=10)
    qualnames = {hit["qualname"] for hit in hits}
    assert {"calcular_descuento_envio", "calcular_impuestos"} <= qualnames


# ── existing return contract is unchanged (as_candidates depends on it) ────

def test_score_field_is_still_the_original_lexical_weighting(ce_store, workspace):
    """`as_candidates` asserts `scores["total"] == hit["score"]`
    (tests/test_context_engine_code_index.py) — the RRF fusion must reorder
    hits without swapping in its own score as `"score"`."""
    hits = ci.search("zorblaxx_recompute_flux", workspace=workspace)
    top = hits[0]
    expected = round(
        0.5 * 1.0 + 0.2 * top["scores"]["path"] + 0.25 * top["scores"]["text"]
        + 0.05 * top["scores"]["freshness"], 6)
    assert top["score"] == pytest.approx(expected)


def test_search_still_never_raises_on_an_empty_query(ce_store, workspace):
    hits = ci.search("", workspace=workspace, k=50)
    assert {h["path"] for h in hits} == {"src/zorblaxx.py", "src/descuentos.py",
                                         "src/invoice.py"}


def test_search_still_never_raises_when_the_store_is_unreachable(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    store.use_path(str(blocker / "ce.db"))
    try:
        assert ci.search("anything", workspace=str(tmp_path)) == []
    finally:
        store.use_path(None)
