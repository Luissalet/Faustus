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
    assert "YYYY-MM-DD" not in fmt["keys"]           # a form, matched by a regex instead
    assert any(m["src"] == "YYYY-MM-DD" and m["on"] == "output" for m in fmt["matchers"])
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


def test_ordering_rules_are_not_pushed_to_the_end():
    text = ("## Rules\n\n"
            "- Each record must have a `name` field.\n"
            "- Each record must have a `size` field.\n"
            "- Each record must have a `kind` field.\n"
            "- Within a batch, points keep their order: later wins.\n"
            "- Checks run in this order: field validation, then existence, then conflicts.\n"
            "- When several things are wrong, the first failing check decides the error code.\n")
    book = cc.CriteriaBook.from_message(text, min_chars=0)
    order = [c["quote"] for c in book.ordered()]
    later = next(c for c in book.criteria if "later wins" in c["quote"])
    assert later["priority"] >= 3
    for q in ("Within a batch", "Checks run in this order", "the first failing check"):
        idx = next(i for i, x in enumerate(order) if q in x)
        assert idx < 3, (q, order)


def test_an_abbreviation_does_not_end_a_sentence():
    crit, _ = cc.extract("Instants use the canonical `...Z` form, e.g. `2026-01-02T03:04:05Z`, in every output.")
    assert len(crit) == 1 and "`2026-01-02T03:04:05Z`" in crit[0]["quote"]


@pytest.mark.parametrize("literal,good,bad", [
    ("YYYY-MM-DD", "on 2026-10-22 at", "on 22/10/2026"),
    ("YYYY-MM-DDTHH:MM:SSZ", '"2026-10-22T08:00:00Z"', '"2026-10-22T08:00:00+02:00"'),
    ("...Z", "2026-10-22T08:00:00Z", "2026-10-22T08:00:00"),
    ("HH:MM", "opens 09:30", "opens 9h30"),
    ("+HH:MM", "2026-10-22T08:00:00+02:00", "2026-10-22T08:00:00Z"),
    ("<series_id>:<YYYY-MM-DD>", '"s1:2026-04-06"', '"s1-2026"'),
    ("-?[0-9]+", "quantity -3", "quantity three"),
    ("[0-9]+(\\.[0-9]{1,4})?", "price 12.5", "price twelve"),
    ("2026-03-30T09:00:00+02:00", "2026-01-01T00:00:00+01:00", "2026-01-01 00:00"),
])
def test_format_literals_become_output_regexes(literal, good, bad):
    m = cc._literal_matcher(literal)
    assert m and m["on"] == "output" and "re" in m, m
    assert cc.matcher_shown(m, output=good, command="")
    assert not cc.matcher_shown(m, output=bad, command="")


@pytest.mark.parametrize("literal,command,other", [
    ("python -m ledger <command> ...", "D:\\venv\\Scripts\\python.exe -m ledger report d.db", "python -m vault get"),
    ("ingest DB FILE...", "python -m ledger ingest d.db a.csv", "python -m ledger rates d.db r.csv"),
    ("project-add --id ID --name NAME --tz ZONE [--holidays D1,D2,...]",
     "python -m tracker --db t.db project-add --name N --tz UTC --id P1", "python -m tracker project-add --id P1"),
    ("GET /api/report?by=...&tz=...", 'curl "http://127.0.0.1:8000/api/report?by=day"', "curl http://h/api/items"),
    ("--tz", "python -m meter report m.db --tz Europe/Madrid", "python -m meter report m.db"),
])
def test_command_templates_are_matched_against_the_command(literal, command, other):
    m = cc._literal_matcher(literal)
    assert m and m["on"] == "command", m
    assert cc.matcher_shown(m, output="", command=command)
    assert not cc.matcher_shown(m, output=command, command=other)


def test_json_shapes_are_matched_by_their_keys_and_unsafe_regexes_are_refused():
    m = cc._literal_matcher('{"accepted": n, "duplicates": n, "rejected": [...]}')
    assert m["json"] == ["accepted", "duplicates", "rejected"]
    assert cc.matcher_shown(m, output='{"duplicates": 0, "accepted": 2, "rejected": []}', command="")
    assert not cc.matcher_shown(m, output='{"accepted": 2}', command="")
    assert cc._literal_matcher("(a+)+b") is None or "re" not in cc._literal_matcher("(a+)+b")
    assert cc._literal_matcher("<command>") is None


# Retained Native Writer R4 receipts as data, never a live browser replay.
# NDJSON SHA256 0e985af223d8a69ac1d162ddc62e2c5d18d1b39cdda50c2a35d7ab8c5faa1cc6.
import json as _json1797

_R4_OPERATIONAL_1797 = _json1797.loads(r'''[
  {
    "call_id": "call_57e318dd1b104eb7834d55a6",
    "tool": "mcp__browser_session__browser_session",
    "content": "{\"action\": \"snapshot\", \"tab_id\": 1069627216, \"view\": {\"query\": \"visibles\", \"roles\": [\"StaticText\"], \"context\": true}}",
    "output": "{\"ok\": true, \"phase\": \"readback_complete\", \"result\": {\"tab\": {\"id\": 1069627216, \"url\": \"http://127.0.0.1:63395/project/proj_mvl51nyx_tl1a8w/overview\", \"title\": \"Writers Hoard\", \"active\": false, \"window_id\": 1069627210}, \"generation\": 8, \"document\": {\"frame_id\": \"26D1015792F3E88CDCC524B743EB3818\", \"loader_id\": \"3955742E939E231CBACE86162C34DE6B\", \"url\": \"http://127.0.0.1:63395/project/proj_mvl51nyx_tl1a8w/overview\"}, \"scope\": \"main_frame\", \"nodes\": [{\"ref\": \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:87\", \"role\": \"StaticText\", \"name\": \"visibles\", \"value\": \"\", \"properties\": {}, \"ignored\": false}], \"selection\": {\"kind\": \"flat\", \"total\": 1053, \"matches\": 1, \"offset\": 0, \"next_offset\": null}, \"context_nodes\": [{\"ref\": \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:58\", \"role\": \"paragraph\", \"name\": \"\", \"value\": \"\", \"properties\": {\"live\": \"polite\", \"atomic\": false, \"relevant\": \"additions text\"}, \"ignored\": false, \"child_refs\": [\"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:85\", \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:86\", \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:87\", \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:88\", \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:89\", \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:90\", \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:91\", \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:92\", \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:93\"]}, {\"ref\": \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:85\", \"role\": \"StaticText\", \"name\": \"36\", \"value\": \"\", \"properties\": {}, \"ignored\": false, \"child_refs\": []}, {\"ref\": \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:86\", \"role\": \"StaticText\", \"name\": \" \", \"value\": \"\", \"properties\": {}, \"ignored\": false, \"child_refs\": []}, {\"ref\": \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:87\", \"role\": \"StaticText\", \"name\": \"visibles\", \"value\": \"\", \"properties\": {}, \"ignored\": false, \"child_refs\": []}, {\"ref\": \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:88\", \"role\": \"StaticText\", \"name\": \" \", \"value\": \"\", \"properties\": {}, \"ignored\": false, \"child_refs\": []}, {\"ref\": \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:89\", \"role\": \"StaticText\", \"name\": \"de\", \"value\": \"\", \"properties\": {}, \"ignored\": false, \"child_refs\": []}, {\"ref\": \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:90\", \"role\": \"StaticText\", \"name\": \" \", \"value\": \"\", \"properties\": {}, \"ignored\": false, \"child_refs\": []}, {\"ref\": \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:91\", \"role\": \"StaticText\", \"name\": \"36\", \"value\": \"\", \"properties\": {}, \"ignored\": false, \"child_refs\": []}, {\"ref\": \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:92\", \"role\": \"StaticText\", \"name\": \" · \", \"value\": \"\", \"properties\": {}, \"ignored\": false, \"child_refs\": []}, {\"ref\": \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:8:93\", \"role\": \"StaticText\", \"name\": \"Todos los documentos\", \"value\": \"\", \"properties\": {}, \"ignored\": false, \"child_refs\": []}]}}"
  },
  {
    "call_id": "call_9818d7a133954cefa86dc094",
    "tool": "mcp__browser_session__browser_session",
    "content": "{\"action\": \"snapshot\", \"tab_id\": 1069627216, \"view\": {\"query\": \"Cerrar\", \"roles\": [\"button\"]}}",
    "output": "{\"ok\": true, \"phase\": \"readback_complete\", \"result\": {\"tab\": {\"id\": 1069627216, \"url\": \"http://127.0.0.1:63395/project/proj_mvl51nyx_tl1a8w/overview\", \"title\": \"Writers Hoard\", \"active\": false, \"window_id\": 1069627210}, \"generation\": 9, \"document\": {\"frame_id\": \"26D1015792F3E88CDCC524B743EB3818\", \"loader_id\": \"3955742E939E231CBACE86162C34DE6B\", \"url\": \"http://127.0.0.1:63395/project/proj_mvl51nyx_tl1a8w/overview\"}, \"scope\": \"main_frame\", \"nodes\": [{\"ref\": \"r:39ecbc01-c314-4b01-aa6a-a00189a0ab39:1069627216:9:36\", \"role\": \"button\", \"name\": \"Cerrar\", \"value\": \"\", \"properties\": {\"invalid\": \"false\", \"focusable\": true, \"focused\": true}, \"ignored\": false}], \"selection\": {\"kind\": \"flat\", \"total\": 1053, \"matches\": 1, \"offset\": 0, \"next_offset\": null}}}"
  },
  {
    "call_id": "call_9312703e145a40d8b17067de",
    "tool": "mcp__browser_session__browser_session",
    "content": "{\"action\": \"release\", \"tab_id\": 1069627216}",
    "output": "{\"ok\": true, \"phase\": \"action_confirmed\", \"result\": {\"tab\": {\"id\": 1069627216, \"url\": \"http://127.0.0.1:63395/\", \"title\": \"Writers Hoard\", \"active\": false, \"window_id\": 1069627210}, \"released\": true}}"
  }
]''')
_R4_PROGRESS_1797 = _json1797.loads(r'''[
  {
    "call_id": "call_1cfbd3cda99e402683bb0745",
    "tool": "todowrite",
    "content": "{\"todos\": [{\"content\": \"t01 \\u2014 action tabs: pesta\\u00f1a QA http://127.0.0.1:63395/ (id 1069627216) identificada; IDs/activeIDs iniciales registrados\", \"priority\": \"high\", \"status\": \"completed\"}, {\"content\": \"t02 \\u2014 Inicio: bot\\u00f3n 'QA compacto Writer1095' abierto por ref; 'Biblioteca' le\\u00eddo y abierto\", \"priority\": \"high\", \"status\": \"completed\"}, {\"content\": \"t03 \\u2014 Contador literal: '36 visibles de 36 \\u00b7 Todos los documentos' con fragmentos por child_refs\", \"priority\": \"high\", \"status\": \"completed\"}, {\"content\": \"t04 \\u2014 Biblioteca cerrada con bot\\u00f3n 'Cerrar'; vuelta a Inicio y 5 proyectos en headings\", \"priority\": \"high\", \"status\": \"completed\"}, {\"content\": \"t05 \\u2014 tabs final id\\u00e9ntico al inicial y release de la pesta\\u00f1a QA confirmado\", \"priority\": \"high\", \"status\": \"completed\"}]}",
    "output": "Updated todo list:\n[x] t01 — action tabs: pestaña QA http://127.0.0.1:63395/ (id 1069627216) identificada; IDs/activeIDs iniciales registrados (high)\n[x] t02 — Inicio: botón 'QA compacto Writer1095' abierto por ref; 'Biblioteca' leído y abierto (high)\n[x] t03 — Contador literal: '36 visibles de 36 · Todos los documentos' con fragmentos por child_refs (high)\n[x] t04 — Biblioteca cerrada con botón 'Cerrar'; vuelta a Inicio y 5 proyectos en headings (high)\n[x] t05 — tabs final idéntico al inicial y release de la pestaña QA confirmado (high)"
  },
  {
    "call_id": "call_baa4ee1e6d624b70a26535ca",
    "tool": "plan_done",
    "content": "{\"id\": \"t01\", \"evidence\": \"action tabs mostr\\u00f3 13 pesta\\u00f1as; la \\u00fanica con URL http://127.0.0.1:63395/ era id 1069627216 (\\\"Writers Hoard\\\", active:false); las dem\\u00e1s eran GitHub/Keep/Drive/Gmail/8899/docs/AVerMedia/58980/58981 (esta \\u00faltima active:true).\"}",
    "output": "t01 t01 marked done"
  },
  {
    "call_id": "call_9f96490cbc8d4fa4b76c089c",
    "tool": "plan_done",
    "content": "{\"id\": \"t02\", \"evidence\": \"snapshot view query 'QA compacto Writer1095' roles ['button'] devolvi\\u00f3 1 match (ref ...:2:243); click abri\\u00f3 proj_mvl51nyx_tl1a8w y el snapshot posterior con roles ['button'] query 'Biblioteca' devolvi\\u00f3 el bot\\u00f3n Biblioteca (...:6:27).\"}",
    "output": "t02 t02 marked done"
  },
  {
    "call_id": "call_535d85b78e674d5c8ea4890b",
    "tool": "plan_done",
    "content": "{\"evidence\": \"snapshot view {query:'visibles',roles:['StaticText'],context:true} devolvi\\u00f3 context_nodes: paragraph con child_refs 8:85..8:93 = \\\"36\\\",\\\" \\\",\\\"visibles\\\",\\\" \\\",\\\"de\\\",\\\" \\\",\\\"36\\\",\\\" \\u00b7 \\\",\\\"Todos los documentos\\\".\", \"id\": \"t03\"}",
    "output": "t03 t03 marked done"
  },
  {
    "call_id": "call_b289b7b053794ff29568a259",
    "tool": "plan_done",
    "content": "{\"evidence\": \"view {query:'Cerrar',roles:['button']} \\u2192 1 match (...:9:36); click cerr\\u00f3 el di\\u00e1logo (vuelve el contenido de proyecto). view {query:'Inicio',roles:['link']} \\u2192 1 match (...:11:35); click naveg\\u00f3 a http://127.0.0.1:63395/. views headings: Proyectos, Inicio + 5 proyectos level 3.\", \"id\": \"t04\"}",
    "output": "t04 t04 marked done"
  },
  {
    "call_id": "call_45fc23eaf5ac47a7ac478b71",
    "tool": "plan_done",
    "content": "{\"evidence\": \"tabs final: 13 pesta\\u00f1as con los mismos ids (1069627211\\u20261069627338) y el mismo active:true (1069627338); 1069627216 segu\\u00eda en http://127.0.0.1:63395/. release sobre tab_id 1069627216 devolvi\\u00f3 {\\\"released\\\": true, \\\"phase\\\": \\\"action_confirmed\\\"}; ninguna pesta\\u00f1a cerrada.\", \"id\": \"t05\"}",
    "output": "t05 t05 marked done"
  }
]''')


def _r4_reference_book1797(alias=None):
    book = cc.CriteriaBook.from_message(
        "The output must include `visibles`.", min_chars=0, min_criteria=1)
    assert book is not None and len(book.criteria) == 1
    book.persist = False
    for packet in _R4_OPERATIONAL_1797 + _R4_PROGRESS_1797:
        tool = alias if alias and packet["tool"].endswith("__browser_session") else packet["tool"]
        book.record_call(packet["call_id"], tool, packet["content"],
                         {"output": packet["output"], "exit_code": 0}, kind="read", ok=True)
    # Existing valid entry before testing any improved locator.
    healthy = book.find_call(call_id="call_9818d7a133954cefa86dc094")
    assert healthy is not None and '\"name\": \"Cerrar\"' in healthy["output"]
    return book


@pytest.mark.parametrize("locator", [
    '"query":"Cerrar"', '"query": "Cerrar"', "Cerrar",
])
def test_r4_command_reference_finds_the_button_not_a_progress_note(locator):
    book = _r4_reference_book1797()
    call = book.find_call(command=locator)
    assert call is not None
    assert call["call_id"] == "call_9818d7a133954cefa86dc094"
    assert '"name": "Cerrar"' in call["output"]
    assert call["tool"] == "mcp__browser_session__browser_session"
    assert book.find_call(test="Cerrar")["call_id"] == call["call_id"]


@pytest.mark.parametrize("alias", [
    "mcp__browser_session__browser_session", "mcp__823416aa__browser_session",
])
def test_r4_tool_alias_and_full_name_locate_the_same_operational_receipt(alias):
    book = _r4_reference_book1797(alias)
    for locator in (alias, "browser_session"):
        call = book.find_call(command=locator)
        assert call is not None and call["call_id"] == "call_9312703e145a40d8b17067de"
        assert _json1797.loads(call["output"])["result"]["released"] is True


def test_r4_context_json_spacing_and_operational_hints_preserve_old_receipts():
    book = _r4_reference_book1797()
    call = book.find_call(command='context":true')
    assert call is not None and call["call_id"] == "call_57e318dd1b104eb7834d55a6"
    assert '"StaticText"' in call["output"] and '"child_refs"' in call["output"]
    hints = book.recent_calls()
    assert any("call_57e318dd1b104eb7834d55a6" in hint for hint in hints)
    assert any("call_9818d7a133954cefa86dc094" in hint for hint in hints)
    assert not any(" plan_done:" in hint or " todowrite:" in hint for hint in hints)
    # Filtering suggestions must not delete the book or invalidate explicit IDs.
    assert len(book.calls) == 9
    note = _R4_PROGRESS_1797[-1]
    assert book.find_call(call_id=note["call_id"])["tool"] == "plan_done"


def test_r4_locator_improvement_does_not_make_observed_text_or_stale_calls_proof():
    book = _r4_reference_book1797()
    criterion = book.criteria[0]["id"]
    for link in ({"id": criterion, "observed": "visibles"},
                 {"id": criterion, "call_id": "not-a-call", "observed": "visibles"}):
        row = book.apply_links([link])[0]
        assert row["state"] == "pending" and row["accepted"] is False
    stale = _r4_reference_book1797()
    stale.record_call("actual-edit", "edit_file", '{"path":"result.py"}',
                      {"output": "Edited result.py", "exit_code": 0}, kind="mutation", ok=True)
    row = stale.apply_links([{
        "id": stale.criteria[0]["id"], "call_id": "call_57e318dd1b104eb7834d55a6",
    }])[0]
    assert row["state"] == "pending" and "stale" in row["detail"]

    failed = _r4_reference_book1797()
    failed.record_call("failed-read", "mcp__browser_session__browser_session",
                       '{"action":"snapshot"}', {"output": "document_changed",
                       "error": "document_changed", "exit_code": 1}, kind="read", ok=False)
    row = failed.apply_links([{
        "id": failed.criteria[0]["id"], "call_id": "failed-read", "observed": "visibles",
    }])[0]
    assert row["state"] == "pending" and row["accepted"] is False

    row = book.apply_links([{
        "id": criterion, "command": 'context":true', "observed": "untrusted note",
    }])[0]
    assert row["state"] == "proven"
    assert book.criteria[0]["evidence_refs"] == ["call_57e318dd1b104eb7834d55a6"]
