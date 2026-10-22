"""`CriteriaBook.auto_link` (src/contract_criteria.py, #1127): the harness proves
criteria by itself from fresh runs, with a stricter tier than the model's links.

A fresh passing run proves; a run before a later edit does not (and a proof
made earlier is withdrawn by the edit); a mutation never proves; one shared
word never proves; unmet / not_applicable are never touched; and a pass is
time-bounded."""
import asyncio
import time

from src import contract_criteria as cc
from src import verification
from tests.test_agent_loop_contract_criteria import SPEC

CORRUPT = '{"error": {"code": "corrupt", "message": "crc"}}'


def _book(spec=SPEC):
    book = cc.CriteriaBook.from_message(spec, min_chars=0)
    assert book is not None
    return book


def _run(book, cid, command, output, exit_code=0, tool="bash", kind="shell"):
    return book.record_call(cid, tool, command, {"output": output, "exit_code": exit_code}, kind=kind)


def _edit(book, cid="edit", path="src/arc/pack.py"):
    return book.record_call(cid, "write_file", '{"path": "%s", "content": "x"}' % path,
                            {"output": "ok", "exit_code": 0}, kind="mutation", paths=[path])


def _state(book, cid):
    return book.get(cid)["state"]


def test_a_fresh_passing_call_proves_what_its_output_shows():
    book = _book()
    _run(book, "c1", "python -m arc unpack damaged.arc", CORRUPT, exit_code=1)
    proven = book.auto_link()
    assert "C04" in proven and _state(book, "C04") == "proven"
    crit = book.get("C04")
    assert crit["detail"] == "auto: c1" and crit["evidence_refs"] == ["c1"] and crit["auto"] is True
    # what the call did not show stays pending
    assert _state(book, "C01") == "pending" and _state(book, "C06") == "pending"


def test_a_call_before_a_later_edit_is_stale_and_does_not_prove():
    book = _book()
    _run(book, "c1", "python -m arc unpack damaged.arc", CORRUPT, exit_code=1)
    _edit(book)
    assert book.auto_link() == []
    assert _state(book, "C04") == "pending"
    # run again after the edit: now it proves
    _run(book, "c2", "python -m arc unpack damaged.arc", CORRUPT, exit_code=1)
    assert "C04" in book.auto_link() and book.get("C04")["evidence_refs"] == ["c2"]


def test_an_edit_withdraws_what_the_harness_proved_from_an_earlier_call_but_not_the_models_links():
    book = _book()
    _run(book, "c1", "python -m arc unpack damaged.arc", CORRUPT, exit_code=1)
    # the bare JSON-document rule C05 is not the harness's to prove here
    assert book.auto_link() == ["C04"]
    # the model links C05 itself with the same call
    book.apply_links([{"id": "C05", "call_id": "c1"}])
    assert book.get("C05")["auto"] is False
    _edit(book)
    assert _state(book, "C04") == "pending" and book.get("C04")["auto"] is False
    assert "withdrawn" in book.get("C04")["detail"]
    assert _state(book, "C05") == "proven"
    # an edit that only touched documentation changes nothing
    _run(book, "c3", "python -m arc unpack damaged.arc", CORRUPT, exit_code=1)
    assert "C04" in book.auto_link()
    _edit(book, "doc", "README.md")
    assert _state(book, "C04") == "proven"


def test_a_mutation_call_never_proves_even_when_its_output_shows_the_terms():
    book = _book()
    _run(book, "m1", "python -m arc unpack damaged.arc && rm -rf out", CORRUPT, exit_code=1, kind="mutation")
    assert book.auto_link() == []
    book.record_call("w1", "write_file", '{"path": "a.txt"}', {"output": CORRUPT, "exit_code": 1},
                     kind="mutation", paths=["a.txt"])
    assert book.auto_link() == [] and not [c for c in book.criteria if c["state"] == "proven"]


def test_only_runs_count_not_reads_or_file_viewers():
    book = _book()
    _run(book, "r1", '{"path": "log.txt"}', CORRUPT, exit_code=1, tool="read_file", kind="read")
    _run(book, "r2", "cat log.txt", CORRUPT, exit_code=1)
    _run(book, "r3", "grep -rn corrupt src", CORRUPT, exit_code=1)
    assert book.auto_link() == []
    _run(book, "r4", "python -m arc scan broken.arc", CORRUPT, exit_code=1)
    assert "C04" in book.auto_link()


def test_words_alone_never_prove_a_rule_without_a_number_or_an_identifier():
    spec = (
        "# Tool\n\n" + "Context about the nightly backup job of the team. " * 30 + "\n\n"
        "## Rules\n\n"
        "- The tool must never delete the source files.\n"
        "- The tool must always keep the original timestamps.\n"
        "- The tool must never leave partial output behind.\n"
    )
    book = _book(spec)
    delete = next(c for c in book.criteria if "delete" in c["quote"])
    assert not (delete["keys"] or delete["limits"] or delete["exits"] or delete["statuses"] or delete["matchers"])
    one = {"call_id": "c1", "tool": "bash", "seq": 1, "command": "python -m arc scan", "mutation": False,
           "output": "listing source", "exit_code": 0}
    # the model's own link keeps the lenient tier: one word is enough there
    assert verification.verify_contract_link(delete, one, strict=True).state == "verified"
    # the harness's tier is not
    assert verification.verify_contract_link(delete, one, strict=True, auto=True).state == "pending"
    book.calls.append(one)
    book.seq = 1
    assert book.auto_link() == [] and _state(book, delete["id"]) == "pending"
    _run(book, "c2", "python -m arc scan", "no source files to delete: nothing was deleted")
    # the words show, but nothing in the sentence (a number, an identifier) is shown
    assert delete["id"] not in book.auto_link()


def test_the_harness_never_sets_unmet_or_not_applicable_and_leaves_them_alone():
    book = _book()
    book.apply_links([{"id": "C04", "status": "unmet", "reason": "still failing"},
                      {"id": "C06", "status": "not_applicable", "reason": "sources are read-only"}])
    _run(book, "c1", "python -m arc unpack damaged.arc", CORRUPT, exit_code=1)
    book.auto_link()
    assert _state(book, "C04") == "unmet" and _state(book, "C06") == "not_applicable"
    assert {c["state"] for c in book.criteria} <= {"pending", "proven", "unmet", "not_applicable"}


def test_a_pass_stops_at_its_time_budget(monkeypatch):
    book = _book()
    _run(book, "c1", "python -m arc unpack damaged.arc", CORRUPT, exit_code=1)
    seen = []

    def slow(criterion, call, **kw):
        seen.append(kw["budget_s"])
        time.sleep(0.05)
        return verification.verify_criterion(criterion, evidence_ok=None, detail="slow")
    monkeypatch.setattr(verification, "verify_contract_link", slow)
    t0 = time.monotonic()
    assert book.auto_link(budget_s=0.2) == []
    assert time.monotonic() - t0 < 0.6
    assert 1 <= len(seen) < len(book.criteria) * 2 and max(seen) <= cc.AUTO_LINK_S


def test_auto_link_async_runs_off_the_loop_and_commits_on_it():
    book = _book()
    _run(book, "c1", "python -m arc unpack damaged.arc", CORRUPT, exit_code=1)

    async def go():
        return await book.auto_link_async()
    assert "C04" in asyncio.run(go())
    assert _state(book, "C04") == "proven"


def test_a_pending_link_attempt_by_the_model_does_not_undo_an_auto_proof():
    book = _book()
    _run(book, "c1", "python -m arc unpack damaged.arc", CORRUPT, exit_code=1)
    book.auto_link()
    _run(book, "c2", "python -m arc --help", "usage: arc")
    rows = book.apply_links([{"id": "C04", "call_id": "c2"}])
    assert rows[0]["accepted"] is False and _state(book, "C04") == "proven" and book.get("C04")["auto"] is True


def test_start_note_takes_its_example_id_from_the_book_and_is_not_deferred():
    book = _book()
    note = book.start_note()
    assert "Before you finish" not in note
    first_open = book.ordered(book.open())[0]["id"]
    assert f'"id": "{first_open}"' in note
    assert note.count("{") == note.count("}")


def test_start_note_ids_follow_the_width_of_the_book_and_its_paging_hint_is_balanced():
    rules = "".join(f"- The `cmd{n}` command must never use more than {n + 5} MB of memory.\n" for n in range(1, 140))
    book = _book("# Tool\n\n" + "Background of the nightly job. " * 60 + "\n\n## Rules\n\n" + rules)
    assert len(book.criteria) >= 100 and book.criteria[0]["id"] == "C001"
    note = book.start_note()
    assert '"id": "C0' in note and '"id": "C01"' not in note
    hint = note.splitlines()[-1]
    assert "more open" in hint
    assert hint.count("{") == hint.count("}") and "}}" not in hint
