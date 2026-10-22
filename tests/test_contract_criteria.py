"""Contract criteria extraction (src/contract_criteria.py): pure, deterministic,
on synthetic task statements written for these tests (several domains, English
and Spanish), plus one recall check on the series contracts when present."""
import time
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
    assert {"num": "50", "unit": "MB"} in rows[0]["limits"] and rows[0]["keys"][0] == "dump"
    assert "load" not in rows[0]["keys"]          # sibling subjects are not keys
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
    assert lim["kind"] == "limit" and lim["limits"][0]["num"] == "200"
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


# -- review fixes ---------------------------------------------------------------

@pytest.mark.parametrize("sentence", [
    "Create `out.csv` with one row per user.",
    "The program prints `OK` on success and exits with status 0.",
    "Implement a CLI named `vault` with the commands put, get and compact.",
    "Write the report to `report/summary.json`.",
    "Use Python 3.11 and no third-party packages.",
    "The output should be sorted by date.",
    "Crea el fichero `salida.txt` con una línea por registro.",
    "La salida debería ir ordenada por fecha.",
    "Each command exits with code 2 on a usage error.",
    "Sale con código 1 si falta el fichero.",
])
def test_imperatives_should_and_exit_statuses_are_requirements(sentence):
    assert cc.is_normative(sentence)


@pytest.mark.parametrize("sentence", [
    "It is a nice tool and people like it.",
    "Read the docs before you start working on it.",
    "The previous version was written in a weekend.",
])
def test_plain_prose_and_vague_imperatives_are_not(sentence):
    assert not cc.is_normative(sentence)


def test_a_vague_imperative_counts_inside_a_deliverables_section():
    assert not cc.is_normative("Document every command for the operators.")
    assert cc.is_normative("Document every command for the operators.", section="Deliverables")
    crit, _ = cc.extract("## Deliverables\n\n- Document every command for the operators.\n")
    assert len(crit) == 1 and crit[0]["kind"] == "behaviour"


def test_exit_status_is_an_error_code_criterion_with_its_status():
    crit, _ = cc.extract("The program prints `OK` on success and exits with status 0.")
    assert crit[0]["kind"] == "error_code" and crit[0]["exits"] == [0]


def test_limits_are_kept_with_their_unit_not_as_bare_numbers():
    crit, _ = cc.extract("Every reply must arrive within 5 ms.")
    assert crit[0]["limits"] == [{"num": "5", "unit": "ms"}]
    assert "5" not in crit[0]["keys"]


def test_a_huge_compound_list_is_one_criterion_and_fast():
    sentence = ("`field_name`, `b`, " * 1200) + "and `c` must work."
    assert len(sentence) > 20_000
    t0 = time.perf_counter()
    crit, _ = cc.extract(sentence)
    assert time.perf_counter() - t0 < 1.0
    assert len(crit) == 1 and crit[0]["subject"] is None
    few, _ = cc.extract("`a`, `b` and `c` must work.")
    assert [c["subject"] for c in few] == ["a", "b", "c"]


def test_a_huge_comma_step_is_one_criterion_and_fast():
    sentence = "Fields: " + ", ".join(f"`field_{i}` must be an integer" for i in range(800)) + "."
    t0 = time.perf_counter()
    crit, _ = cc.extract(sentence)
    assert time.perf_counter() - t0 < 1.0
    assert len(crit) == 1


def test_a_100_kb_message_extracts_well_under_a_second():
    para = ("The `put` command must stream the value in chunks of at most 64 KiB. "
            "It fails with `E_TOO_BIG` when the value exceeds 10 MB; responses must arrive within 200 ms.\n\n")
    rules = "".join(f"- Rule {i}: the field `f{i}` must never be empty.\n" for i in range(600))
    msg = "# Spec\n\n" + para * 460 + "## Rules\n\n" + rules
    assert len(msg) > 100_000
    t0 = time.perf_counter()
    book = cc.CriteriaBook.from_message(msg, min_chars=0)
    assert time.perf_counter() - t0 < 1.0
    assert len(book.criteria) == cc.DEFAULT_MAX and book.dropped > 0


def test_text_read_per_message_is_capped():
    parts = cc.source_text("x" * (cc.MAX_TEXT_CHARS + 5000))
    assert sum(len(t) for _, t in parts) == cc.MAX_TEXT_CHARS


def test_only_statement_like_attachments_are_read():
    msg = ("Implement the attached spec.\n=== File: TASK.md ===\nIt must exit 0.\n"
           "=== File: LICENSE ===\nThe above notice shall be included in all copies.\n"
           "=== File: server.log ===\nerror code `E42` must never happen\n")
    assert [p[0] for p in cc.source_text(msg)] == ["message", "TASK.md"]


def test_exposure_order_takes_sections_in_turn_and_strong_rules_first():
    text = ("## A\n\n- A plain line must be shown.\n- It must never crash.\n- Another one must be kept.\n\n"
            "## B\n\n- B must log every call.\n\n## C\n\n- C must exit 0.\n")
    book = cc.CriteriaBook.from_message(text, min_chars=0)
    order = [c["quote"] for c in book.ordered()]
    assert order[0] == "It must never crash."          # strongest of section A first
    assert order[1].startswith("B must") and order[2].startswith("C must")
    assert order[3] == "A plain line must be shown."


SERIES5 = SERIES / "series21-job5-tsdb" / "TASK.md"
SERIES6 = SERIES / "series21-job6-meter" / "TASK.md"


def test_recall_on_the_large_series_contracts_keeps_every_never_rule():
    if not (SERIES5.exists() and SERIES6.exists()):
        pytest.skip("series contracts not available")
    tsdb = cc.CriteriaBook.from_message(SERIES5.read_text(encoding="utf-8"), min_chars=0)
    meter = cc.CriteriaBook.from_message(SERIES6.read_text(encoding="utf-8"), min_chars=0)
    assert tsdb.dropped == 0 and meter.dropped == 0
    tq = [c["quote"] for c in tsdb.criteria]
    mq = [c["quote"] for c in meter.criteria]
    assert any("stack trace must never be the only output" in q for q in tq)
    assert any("Within a batch, points keep their order" in q or "later wins" in q for q in tq)
    assert any("The index ends exactly where the footer starts" in q for q in tq)
    assert any(q.startswith("Never print a Python traceback") for q in mq)
    assert any("never produce `\\r\\n` line ends" in q for q in mq)
    assert any("named exactly as it was written on the command line" in q for q in mq)
    # all of it is listable, page by page, and every criterion counts
    pages = meter.list_page(page=1)["pages"]
    seen = set()
    for p in range(1, pages + 1):
        seen.update(meter.list_page(page=p)["ids"])
    assert seen == {c["id"] for c in meter.criteria}
    assert meter.summary()["open_total"] == len(meter.criteria)


def test_notes_stay_in_budget_and_the_gap_note_covers_what_the_audit_did_not():
    if not SERIES5.exists():
        pytest.skip("series contracts not available")
    book = cc.CriteriaBook.from_message(SERIES5.read_text(encoding="utf-8"), min_chars=0)
    base = "BASE AUDIT TEXT " * 50
    start = book.start_note()
    audit = book.audit_note(base)
    gap = book.gap_note()
    assert len(start) <= cc.START_NOTE_CHARS
    assert audit.startswith(base) and len(audit) <= cc.AUDIT_NOTE_CHARS + 600
    assert len(gap) <= cc.GAP_NOTE_CHARS
    assert "more open" in audit or "Also open" in audit
    quoted = book.shown_audit
    assert quoted
    first_gap_ids = [ln.split(" ", 1)[0] for ln in gap.splitlines()[1:6]]
    assert first_gap_ids and not (set(first_gap_ids) & quoted)


def test_list_page_filters_by_section_and_kind_and_pages():
    text = "## Storage\n\n" + "".join(f"- The file `s{i}` must exist.\n" for i in range(40)) + \
           "\n## Report\n\n" + "".join(f"- The report must show `r{i}` within 5 ms.\n" for i in range(5))
    book = cc.CriteriaBook.from_message(text, min_chars=0)
    rep = book.list_page(section="report")
    assert rep["rows"] == 5 and all(i in rep["ids"] for i in [c["id"] for c in book.criteria[40:]])
    lim = book.list_page(kind="limit")
    assert lim["rows"] == 5
    small = book.list_page(page_chars=400)
    assert small["pages"] > 1
    assert book.list_page(page_chars=400, page=99)["page"] == small["pages"]
