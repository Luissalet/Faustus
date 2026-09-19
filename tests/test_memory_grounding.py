"""Grounding lint for the learned memory store (src/memory_grounding.py)
and its HTTP surface (routes/memory_grounding_routes.py).

Mirrors tests/test_memory_conflicts.py's fixtures (real FastAPI app +
TestClient, COMUN.md rule 7) and tests/test_memory_engine.py's
disposable-store fixture.
"""

import pytest

from src import memory_grounding as grounding
from src import memory_engine as engine


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "DATA_DIR", str(tmp_path))
    engine.set_vector_store(None)
    engine.clear_injected()
    yield tmp_path
    engine.reset_vector_store()
    engine.clear_injected()


@pytest.fixture()
def client(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.middleware import require_admin
    from routes import memory_grounding_routes

    monkeypatch.setattr(memory_grounding_routes, "effective_user", lambda request: "luis")
    app = FastAPI()
    app.include_router(memory_grounding_routes.setup_memory_grounding_routes())
    app.dependency_overrides[require_admin] = lambda: None
    return TestClient(app)


# ── extract_specifics — table-driven, English + Spanish ────────────────────

def test_extracts_plain_and_thousands_numbers():
    specs = grounding.extract_specifics("Revenue was 1,000 units and 42 more.")
    numbers = [s for s in specs if s["type"] == "number"]
    assert {s["norm"] for s in numbers} == {1000.0, 42.0}


def test_extracts_percent_and_currency():
    specs = grounding.extract_specifics("Grew 19% to $1.000.000 this quarter.")
    kinds = {s["type"]: s["norm"] for s in specs if s["type"] in ("percent", "currency")}
    assert kinds.get("percent") == 19.0
    assert kinds.get("currency") == 1000000.0


def test_extracts_iso_date():
    specs = grounding.extract_specifics("Shipped on 2026-09-19.")
    dates = [s for s in specs if s["type"] == "date"]
    assert dates and dates[0]["norm"] == (2026, 9, 19)


def test_extracts_spanish_long_date():
    specs = grounding.extract_specifics("Firmado el 19 de septiembre de 2026.")
    dates = [s for s in specs if s["type"] == "date"]
    assert dates and dates[0]["norm"] == (2026, 9, 19)


def test_extracts_english_long_date_with_and_without_year():
    specs = grounding.extract_specifics("Due September 19, 2026, follow up Sept 19.")
    dates = [s for s in specs if s["type"] == "date"]
    norms = {d["norm"] for d in dates}
    assert (2026, 9, 19) in norms
    assert (None, 9, 19) in norms


def test_extracts_quoted_string():
    specs = grounding.extract_specifics('The report is called "Faustus Weekly".')
    quotes = [s for s in specs if s["type"] == "quote"]
    assert quotes and quotes[0]["value"] == "Faustus Weekly"


def test_extracts_proper_noun_multiword_name():
    specs = grounding.extract_specifics("Ada Lovelace approved the change.")
    names = [s for s in specs if s["type"] == "proper_noun"]
    assert any(n["value"] == "Ada Lovelace" for n in names)


def test_single_capitalized_word_is_not_a_proper_noun():
    specs = grounding.extract_specifics("Faustus is a workspace.")
    assert not [s for s in specs if s["type"] == "proper_noun"]


def test_extracts_url_and_email():
    specs = grounding.extract_specifics("See https://example.com/report or ada@example.com.")
    kinds = {s["type"] for s in specs}
    assert "url" in kinds and "email" in kinds


def test_prose_with_nothing_checkable_extracts_nothing():
    assert grounding.extract_specifics("This is fine.") == []


def test_empty_text_extracts_nothing():
    assert grounding.extract_specifics("") == []
    assert grounding.extract_specifics(None) == []


# ── number normalisation equivalences ───────────────────────────────────────

def test_number_normalization_thousands_equivalence():
    assert grounding.normalize_number("1.000") == 1000.0
    assert grounding.normalize_number("1,000") == 1000.0
    assert grounding.normalize_number("1000") == 1000.0


def test_number_normalization_decimal_comma_equivalence():
    assert grounding.normalize_number("19,5") == 19.5
    assert grounding.normalize_number("19.5") == 19.5


def test_number_normalization_mixed_separators():
    assert grounding.normalize_number("1.234.567,89") == pytest.approx(1234567.89)
    assert grounding.normalize_number("1,234,567.89") == pytest.approx(1234567.89)


def test_number_normalization_invalid_returns_none():
    assert grounding.normalize_number("not a number") is None
    assert grounding.normalize_number("") is None


# ── check_item — grounded vs fabricated ─────────────────────────────────────

def test_check_item_grounded_when_every_specific_is_backed():
    claim = "Revenue grew 19% to $1,000,000 on September 19, 2026."
    evidence = ["Q3 update: revenue grew 19 percent to 1.000.000 dollars on "
               "19 de septiembre de 2026."]
    result = grounding.check_item(claim, evidence)
    assert result["grounded"] is True
    assert result["missing"] == []
    assert result["checked"] > 0


def test_check_item_flags_fabricated_specifics():
    claim = "Revenue grew 19% to $1,000,000 on September 19, 2026."
    evidence = ["Q3 update: revenue grew nicely this quarter."]
    result = grounding.check_item(claim, evidence)
    assert result["grounded"] is False
    assert len(result["missing"]) > 0
    assert result["checked"] > 0


def test_check_item_with_no_checkable_specifics_is_trivially_grounded():
    result = grounding.check_item("Everything is fine.", [])
    assert result == {"grounded": True, "missing": [], "checked": 0}


def test_check_item_quote_and_name_matched_case_and_accent_insensitively():
    claim = 'Confirmed by "the CFO" per José García.'
    evidence = ['confirmed by "THE CFO" per Jose Garcia yesterday']
    result = grounding.check_item(claim, evidence)
    assert result["grounded"] is True


# ── lint — over the memory store ────────────────────────────────────────────

def test_lint_flags_item_whose_claim_outruns_its_evidence(store):
    item = engine.add_item(
        "Revenue grew 19% to $1,000,000 on September 19, 2026.",
        owner="luis", trust_class="human_explicit",
        evidence=[{"kind": "chat", "excerpt": "revenue grew nicely this quarter"}],
    )
    report = grounding.lint(owner="luis")
    ids = {f["id"] for f in report["findings"]}
    assert item["id"] in ids
    finding = next(f for f in report["findings"] if f["id"] == item["id"])
    assert len(finding["missing"]) > 0


def test_lint_does_not_flag_a_well_grounded_item(store):
    engine.add_item(
        "Revenue grew 19% to $1,000,000.",
        owner="luis", trust_class="human_explicit",
        evidence=[{"kind": "chat", "excerpt": "revenue grew 19% to $1,000,000 this quarter"}],
    )
    report = grounding.lint(owner="luis")
    assert report["findings"] == []


def test_lint_counts_items_without_evidence_as_unverifiable_not_flagged(store):
    engine.add_item("Revenue grew 19% to $1,000,000 on September 19, 2026.",
                    owner="luis", trust_class="human_explicit")
    report = grounding.lint(owner="luis")
    assert report["findings"] == []
    assert report["unverifiable"] == 1


def test_lint_sorts_findings_by_missing_count_descending(store):
    small = engine.add_item(
        "Grew 19%.", owner="luis", trust_class="human_explicit",
        evidence=[{"kind": "chat", "excerpt": "nothing about growth here"}],
    )
    big = engine.add_item(
        "Grew 19% to $1,000,000 on September 19, 2026, per Ada Lovelace.",
        owner="luis", trust_class="human_explicit",
        evidence=[{"kind": "chat", "excerpt": "nothing about growth here"}],
    )
    report = grounding.lint(owner="luis")
    ids_in_order = [f["id"] for f in report["findings"]]
    assert ids_in_order.index(big["id"]) < ids_in_order.index(small["id"])


def test_lint_owner_isolation(store):
    engine.add_item(
        "Grew 19% to $1,000,000.", owner="alice", trust_class="human_explicit",
        evidence=[{"kind": "chat", "excerpt": "no numbers here"}],
    )
    engine.add_item(
        "Grew 19% to $1,000,000.", owner="bob", trust_class="human_explicit",
        evidence=[{"kind": "chat", "excerpt": "no numbers here"}],
    )
    alice_report = grounding.lint(owner="alice")
    bob_report = grounding.lint(owner="bob")
    assert len(alice_report["findings"]) == 1
    assert len(bob_report["findings"]) == 1
    assert alice_report["findings"][0]["id"] != bob_report["findings"][0]["id"]


def test_lint_never_raises_on_a_broken_store(monkeypatch):
    from src import memory_engine as engine_mod

    def _boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(engine_mod, "list_items", _boom)
    report = grounding.lint(owner="luis")
    assert report["findings"] == []


# ── HTTP surface — owner scoping ────────────────────────────────────────────

def test_route_returns_owner_scoped_report(client):
    engine.add_item(
        "Grew 19% to $1,000,000.", owner="luis", trust_class="human_explicit",
        evidence=[{"kind": "chat", "excerpt": "no numbers here"}],
    )
    engine.add_item(
        "Grew 12% to $500,000.", owner="someone-else", trust_class="human_explicit",
        evidence=[{"kind": "chat", "excerpt": "no numbers here"}],
    )
    resp = client.get("/api/memory/grounding")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    assert len(body["findings"]) == 1
    assert "19%" in str(body["findings"][0]["missing"]) or any(
        m["value"] == "19%" for m in body["findings"][0]["missing"])
