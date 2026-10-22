"""Contract criteria extraction (src/contract_criteria.py): pure, deterministic,
on synthetic task statements written for these tests (several domains, English
and Spanish), plus one recall check on the series contracts when present."""
from pathlib import Path

import pytest

from src import contract_criteria as cc


KV_STORE = """# Task: a key-value store

Build a package `kv` runnable as `python -m kv <command>`.

## Commands

- `set KEY VALUE` stores one value. A key must match `[a-z0-9_]{1,32}`; any other key fails with `invalid`.
- `dump`, `load` and `merge` stream: they must never hold more than 50 MB in memory, whatever the size
  of the store (a 2 GB store must work).
- Every command prints exactly one JSON document and exits 0 on success.
- A usage error (unknown command, missing or malformed option: code `usage`) exits 2.

## Notes

The store is a single file. Why a single file? It keeps backups simple.

```
python -m kv set a 1   # must print {"ok": true}
```
"""

BOOKINGS_ES = """# Reservas

El servicio guarda reservas de salas.

1. Una sala solo puede tener una reserva activa a la vez.
2. Las fechas deben ir en formato `YYYY-MM-DD`; en otro caso la respuesta es `fecha_invalida`.
3. Cada petición debe responder en como máximo 200 ms con 50 usuarios.
4. Nunca se borra una reserva: se cancela.

¿Hace falta autenticación? No en esta versión.
"""

IMAGES = """Resize images in a folder.

The tool reads every `.png` and `.jpg` file in INPUT and writes the result to OUTPUT. Output images are at most
1024 pixels wide and at least 64 px tall. The program must finish within 30 seconds for 100 images,
and it must exit with code 3 when INPUT does not exist.

It is a nice tool and people like it.
"""

LONG_STEP = """## Import

Importing applies these steps in order:
1. Shape: exactly four keys, `version` is the integer 2, timestamps in the canonical `...Z` form, `size` an
   integer 0-100, no duplicate id inside one list, every tag is lower-case, and the whole document is at most
   1 MB (`invalid` otherwise).
"""


def _quotes(criteria):
    return [c["quote"] for c in criteria]


def _by_subject(criteria):
    return {c["subject"]: c for c in criteria if c["subject"]}


def test_compound_subject_gives_one_criterion_per_subject_with_the_same_quote_and_line():
    crit, dropped = cc.extract(KV_STORE)
    subs = _by_subject(crit)
    assert {"dump", "load", "merge"} <= set(subs)
    rows = [subs[s] for s in ("dump", "load", "merge")]
    assert len({r["quote"] for r in rows}) == 1
    assert {r["line"] for r in rows} == {8}
    assert all(r["kind"] == "limit" for r in rows)
    assert "50" in rows[0]["keys"] and rows[0]["keys"][0] == "dump"
    assert all(r["section"] == "Task: a key-value store › Commands" for r in rows)
    assert dropped == 0


def test_parenthesised_cases_expand_and_missing_or_malformed_gives_two():
    crit, _ = cc.extract(KV_STORE)
    subs = _by_subject(crit)
    assert {"unknown command", "missing option", "malformed option"} <= set(subs)
    assert subs["missing option"]["kind"] == "error_code"


def test_descriptive_prose_questions_headings_and_code_are_not_criteria():
    crit, _ = cc.extract(KV_STORE)
    joined = "\n".join(_quotes(crit))
    assert "Why a single file" not in joined
    assert "The store is a single file" not in joined
    assert "must print" not in joined          # inside a fenced block
    assert not any(q.startswith("#") for q in _quotes(crit))


def test_wrapped_lines_join_into_one_sentence_with_the_start_line():
    crit, _ = cc.extract(KV_STORE)
    row = _by_subject(crit)["merge"]
    assert "(a 2 GB store must work)" in row["quote"]
    assert row["line"] == 8


def test_spanish_markers_formats_codes_and_limits():
    crit, _ = cc.extract(BOOKINGS_ES)
    quotes = _quotes(crit)
    assert any("solo puede tener una reserva activa" in q for q in quotes)
    fmt = next(c for c in crit if "YYYY-MM-DD" in c["quote"])
    assert fmt["kind"] in ("format", "error_code")
    assert "YYYY-MM-DD" in fmt["keys"]
    lim = next(c for c in crit if "200 ms" in c["quote"])
    assert lim["kind"] == "limit" and "200" in lim["keys"]
    assert any(q.startswith("Nunca se borra") for q in quotes)
    assert not any("autenticación" in q for q in quotes)
    assert next(c for c in crit if "Nunca se borra" in c["quote"])["section"] == "Reservas › 4"


def test_limits_with_units_and_exit_codes_in_prose():
    crit, _ = cc.extract(IMAGES)
    quotes = _quotes(crit)
    assert any("at most 1024 pixels" in q for q in quotes)
    assert any("within 30 seconds" in q for q in quotes)
    assert not any("nice tool" in q for q in quotes)
    assert all(c["section"] == "" for c in crit)


def test_long_comma_step_is_split_into_marked_parts():
    crit, _ = cc.extract(LONG_STEP)
    quotes = _quotes(crit)
    assert any(q == "Shape: timestamps in the canonical `...Z` form" for q in quotes), quotes
    assert any("`version` is the integer 2" in q for q in quotes)
    z = next(c for c in crit if "...Z" in c["quote"])
    assert z["kind"] == "format" and z["section"] == "Import › 1" and z["line"] == 4


def test_duplicates_are_dropped_and_ids_are_sequential():
    text = "The output must be sorted.\n\nThe output must be sorted.\n\nIt must never crash. It must log."
    crit, _ = cc.extract(text)
    assert [c["id"] for c in crit] == ["C01", "C02", "C03"]
    assert _quotes(crit).count("The output must be sorted.") == 1


def test_cap_keeps_limits_codes_and_formats_first_then_document_order():
    lines = [f"Rule {i}: the report must mention item {i}." for i in range(10)]
    lines.insert(7, "Every answer must arrive in under 5 seconds.")
    lines.insert(3, "A bad request fails with `bad_request`.")
    crit, dropped = cc.extract("\n\n".join(lines), max_items=4)
    assert dropped == 8
    quotes = _quotes(crit)
    assert "A bad request fails with `bad_request`." in quotes
    assert "Every answer must arrive in under 5 seconds." in quotes
    assert [c["id"] for c in crit] == ["C01", "C02", "C03", "C04"]
    lines_kept = [c["line"] for c in crit]
    assert lines_kept == sorted(lines_kept)


def test_extraction_is_deterministic():
    assert cc.extract(KV_STORE) == cc.extract(KV_STORE)


def test_source_text_reads_typed_request_and_text_attachments_only():
    msg = ("Implement the attached spec.\n=== File: SPEC.md ===\nIt must exit 0.\n"
           "=== File: main.py ===\nassert x  # must never be read\n")
    parts = cc.source_text(msg)
    assert [p[0] for p in parts] == ["message", "SPEC.md"]
    crit, _ = cc.extract_parts(parts)
    assert [c["source"] for c in crit] == ["SPEC.md"]


def test_book_needs_a_long_message_and_enough_criteria(tmp_path, monkeypatch):
    monkeypatch.setattr("src.plan_tracker.PLAN_TRACKER_DIR", str(tmp_path))
    assert cc.CriteriaBook.from_message(KV_STORE, min_chars=5000) is None
    assert cc.CriteriaBook.from_message("It must work. It must be fast. It must never crash.", min_chars=0, min_criteria=3) is not None
    assert cc.CriteriaBook.from_message("It must work.", min_chars=0) is None
    book = cc.CriteriaBook.from_message(KV_STORE, min_chars=100, max_items=60)
    assert book is not None and book.counts()["pending"] == len(book.criteria)


def test_mode_applies():
    assert not cc.mode_applies("off", unattended=True)
    assert cc.mode_applies("unattended", unattended=True)
    assert not cc.mode_applies("unattended", unattended=False)
    assert cc.mode_applies("always", unattended=False)


SERIES = Path(r"D:\LocalAI\research-inbox\sparks835\runtime-audit\resume-20261020")


def _series(job):
    path = SERIES / job / "TASK.md"
    if not path.exists():
        pytest.skip(f"{path} not available")
    return cc.extract(path.read_text(encoding="utf-8"))[0]


def test_recall_on_the_series_contracts_covers_the_four_failed_clauses():
    vault = _series("series21-job2-vault")
    subs = {c["subject"] for c in vault if "memory" in c["quote"] and "200 MB" in c["quote"]}
    assert {"get", "compact"} <= subs
    ledger = _series("series21-job3-ledger")
    assert any("EUR row" in c["quote"] and "as a duplicate" in c["quote"] for c in ledger)
    lending = _series("series21-job4-lending")
    # Import, step 1: instants in the canonical `...Z` form (its own criterion).
    assert any("canonical `...Z`" in c["quote"] and c["section"].endswith("› 1") for c in lending)
    # The command line section states the missing-argument case explicitly.
    assert any(c["subject"] == "missing argument" and "`invalid`" in c["quote"] for c in lending)
