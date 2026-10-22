"""Precision of the harness's own linking (`CriteriaBook.auto_link`,
`verify_contract_link(auto=True)`): an exit status or a bare JSON document
never proves alone, a silent run proves nothing, crashed or failed runs prove
nothing, a run that only reads files is not a run, a generative script
withdraws earlier proofs, the pass rotates and never judges a pair twice."""
import asyncio
import time

import pytest

from src import contract_criteria as cc
from src import verification
from src.agent_harness import shell_command_looks_mutating
from tests.test_agent_loop_contract_criteria import SPEC

CORRUPT = '{"error": {"code": "corrupt", "message": "crc"}}'


def _book(spec=SPEC):
    book = cc.CriteriaBook.from_message(spec, min_chars=0)
    assert book is not None
    return book


def _run(book, cid, command, output, exit_code=0, tool="bash", kind="shell"):
    result = {"exit_code": exit_code}
    if output is not None:
        result["output"] = output
    return book.record_call(cid, tool, command, result, kind=kind)


def _crit(quote, **kw):
    base = {"id": "C1", "quote": quote, "state": "pending", "subject": "", "keys": [], "words": [],
            "matchers": [], "limits": [], "exits": [], "statuses": []}
    base.update(kw)
    return base


def _call(output, exit_code=0, command="python -m arc scan good.arc"):
    return {"call_id": "c1", "tool": "bash", "seq": 1, "command": command, "output": output,
            "exit_code": exit_code, "mutation": False}


def _verdict(crit, call, auto=True):
    return verification.verify_contract_link(crit, call, strict=True, auto=auto).state


# -- exit status ---------------------------------------------------------------

def test_a_bare_exit_status_rule_is_not_proven_by_just_any_clean_run():
    crit = _crit("The tool exits with status 0 when the archive is healthy.", exits=[0],
                 words=["tool", "exits", "archive", "healthy"])
    call = _call("all done")
    # the model's own link keeps the exit-code branch
    assert _verdict(crit, call, auto=False) == "verified"
    assert _verdict(crit, call, auto=True) == "pending"


def test_an_exit_status_counts_together_with_subject_key_or_two_words():
    words = ["tool", "exits", "archive", "healthy"]
    quote = "The tool exits with status 0 when the archive is healthy."
    with_subject = _crit(quote, exits=[0], words=words, subject="scan")
    assert _verdict(with_subject, _call("scan: 3 members ok")) == "verified"
    # the subject must show (in the output, or as the command's own argument)
    assert _verdict(with_subject, _call("all done", command="python -m arc pack good.arc")) == "pending"
    with_key = _crit(quote, exits=[0], words=words, keys=["members"])
    assert _verdict(with_key, _call("3 members ok")) == "verified"
    two_words = _crit(quote, exits=[0], words=words)
    assert _verdict(two_words, _call("tool archive healthy")) == "verified"
    assert _verdict(two_words, _call("archive healthy")) == "pending"


# -- silent runs and the JSON document ------------------------------------------

def test_a_silent_failing_run_does_not_prove_the_json_document_rule():
    book = _book()
    call = _run(book, "s1", "python -m arc scan x.arc", None, exit_code=1)
    assert call["silent"] is True
    # the record is a JSON dump (kept for the model's own links) ...
    assert call["output"].startswith("{")
    # ... but the harness never reads it as something the program printed
    assert book.auto_candidates() == []
    assert book.auto_link() == []
    assert book.get("C05")["state"] == "pending"


def test_a_bare_json_document_needs_a_key_a_subject_or_two_words():
    book = _book()
    _run(book, "j1", "python -m arc scan x.arc", '{"a": 1, "b": [2]}')
    assert book.auto_link() == []
    assert book.get("C05")["state"] == "pending"
    # the model's own link to the same call is still accepted
    book.apply_links([{"id": "C05", "call_id": "j1"}])
    assert book.get("C05")["state"] == "proven"
    book2 = _book()
    _run(book2, "j2", "python -m arc scan x.arc", '{"command": "scan", "document": 1, "stdout": "one"}')
    assert book2.auto_link() == ["C05"]


# -- failing and crashed runs ---------------------------------------------------

@pytest.mark.parametrize("output,code", [
    ("FAILED tests/test_corrupt.py::test_corrupt - assert 1 == 2\n1 failed, 3 passed in 0.2s", 1),
    ("Traceback (most recent call last):\n  File \"arc.py\", line 3, in <module>\n"
     "NameError: name 'corrupt' is not defined", 1),
    ("ERROR tests/test_corrupt.py - ImportError: cannot import name corrupt", 2),
])
def test_a_crashed_or_failed_test_run_does_not_prove_a_failure_rule(output, code):
    book = _book()
    _run(book, "f1", "python -m pytest tests/test_corrupt.py", output, exit_code=code)
    assert book.auto_link() == []
    assert book.get("C04")["state"] == "pending"
    # the same words from the program reporting the error on purpose do prove it
    _run(book, "f2", "python -m arc unpack damaged.arc", CORRUPT, exit_code=1)
    assert "C04" in book.auto_link()


def test_a_failed_run_only_proves_a_rule_that_is_about_a_failure():
    delete = _crit("The tool must never delete the source files.", words=["delete", "source", "files"])
    out = "no source files to delete: nothing was deleted"
    assert _verdict(delete, _call(out)) == "verified"
    assert _verdict(delete, _call(out, exit_code=1)) == "pending"
    fails = _crit("A damaged archive fails with `corrupt`.", keys=["corrupt"], words=["damaged", "archive", "fails"])
    assert _verdict(fails, _call(CORRUPT, exit_code=1)) == "verified"
    # about a failure and failing: plain words are not enough
    only_words = _crit("A damaged archive is rejected loudly.", words=["damaged", "archive", "rejected"])
    assert _verdict(only_words, _call("damaged archive rejected", exit_code=1)) == "pending"


# -- what counts as a run -------------------------------------------------------

@pytest.mark.parametrize("tool,command,expected", [
    ("python", "print(open('arc.py').read())", False),
    ("python", "import json\nprint(json.load(open('out.json'))['n'])", False),
    ("python", "from pathlib import Path\nprint(Path('a.txt').read_text())", False),
    ("python", "import arc\nprint(arc.scan('x.arc'))", True),
    ("python", "import subprocess\nsubprocess.run(['arc', 'scan'])", True),
    ("python", "open('out.txt', 'w').write('x')", True),
    ("bash", "python -c \"print(open('arc.py').read())\"", False),
    ("bash", "python -c \"import arc; print(arc.scan('x'))\"", True),
    ("bash", "cat out.json | jq .", False),
    ("bash", "cat log.txt | head -20", False),
    ("bash", "cat log.txt | sort | uniq -c | wc -l", False),
    ("bash", "jq '.items | length' out.json", False),
    ("bash", "cd src && python -m arc scan x.arc", True),
    ("bash", "./run.sh | jq .", True),
    ("powershell", "Get-Content out.json | ConvertFrom-Json", True),
    ("powershell", "Test-Path out.json", False),
])
def test_is_run_call_ignores_what_only_reads_or_filters(tool, command, expected):
    assert cc.is_run_call(tool, command) is expected


def test_a_read_only_python_call_never_proves():
    book = _book()
    _run(book, "p1", "print(open('arc.py').read())", "def unpack(): raise Corrupt('corrupt')\n"
         "# A damaged archive fails with corrupt", tool="python", kind="read")
    _run(book, "p2", "python -c \"print(open('arc.py').read())\"", "damaged archive fails with corrupt")
    assert book.auto_candidates() == [] and book.auto_link() == []


# -- generative scripts ---------------------------------------------------------

@pytest.mark.parametrize("command,expected", [
    ("python build.py", True),
    ("node gen.js", True),
    ("python tools/generate_report.py --out x", True),
    ("./setup.sh", True),
    ("python -m build", True),
    ("bash scripts/migrate-db.sh", True),
    ("python -m arc scan x.arc", False),
    ("python test_setup.py", False),
    ("python check_build.py", False),
    ("python -m pytest tests", False),
    ("node run.js", False),
])
def test_generative_script_names(command, expected):
    assert cc.is_generative_script(command) is expected


def test_a_generative_script_withdraws_earlier_auto_proofs_and_proves_nothing():
    book = _book()
    _run(book, "c1", "python -m arc unpack damaged.arc", CORRUPT, exit_code=1)
    assert book.auto_link() == ["C04"]
    built = _run(book, "b1", "python build.py", CORRUPT, exit_code=1)   # filed as a plain shell call
    assert built["mutation"] is True
    assert book.last_mutation_seq == built["seq"]
    assert book.get("C04")["state"] == "pending" and "withdrawn" in book.get("C04")["detail"]
    assert book.auto_link() == []
    _run(book, "c2", "python -m arc unpack damaged.arc", CORRUPT, exit_code=1)
    assert book.auto_link() == ["C04"]


@pytest.mark.parametrize("command,expected", [
    ("make test", False), ("make check", False), ("make lint", False), ("make verify", False),
    ("make test-unit", False), ("npm run test", False), ("npm run lint", False), ("yarn test", False),
    ("pnpm run check", False), ("pnpm lint", False), ("cd app && make test", False),
    ("make", True), ("make install", True), ("make build", True), ("npm run build", True),
    ("npm install", True), ("yarn", True), ("make test > out.txt", True),
    ("npm run test && npm run build", True), ("make test && make install", True),
    ("git status --short", False),
])
def test_test_like_targets_are_runs_not_mutations(command, expected):
    assert shell_command_looks_mutating(command) is expected


def test_running_the_projects_tests_keeps_the_earlier_proofs():
    book = _book()
    _run(book, "c1", "python -m arc unpack damaged.arc", CORRUPT, exit_code=1)
    assert book.auto_link() == ["C04"]
    for n, cmd in enumerate(("make test", "npm run lint")):
        kind = "mutation" if shell_command_looks_mutating(cmd) else "shell"
        _run(book, f"t{n}", cmd, "ok: 12 checks", kind=kind)
    assert book.get("C04")["state"] == "proven" and book.last_mutation_seq == -1


# -- the pass: rotation, judged once, nothing new -------------------------------

def _big_book(n=40):
    book = _book()
    base = next(c for c in book.criteria if c["id"] == "C06")
    for i in range(n):
        c = dict(base)
        c["id"] = f"C{100 + i}"
        c["state"] = "pending"
        c["words"] = [f"alpha{i}", f"beta{i}", f"gamma{i}"]
        book.criteria.append(c)
    book._pos = {c["id"]: k for k, c in enumerate(book.criteria)}
    return book


def test_a_pass_that_runs_out_of_time_resumes_where_it_stopped(monkeypatch):
    book = _big_book()
    seen = []
    real = verification.verify_contract_link

    def slow(crit, call, **kw):
        seen.append(crit["id"])
        time.sleep(0.03)
        return real(crit, call, **kw)
    monkeypatch.setattr(verification, "verify_contract_link", slow)
    _run(book, "c1", "python -m arc scan x.arc", "nothing to see")
    book.auto_link(budget_s=0.2)
    first = list(seen)
    assert 0 < len(first) < len(book.criteria)
    seen.clear()
    book.auto_link(budget_s=0.2)
    second = list(seen)
    assert second and second[0] not in first, (first, second)
    # keep going: every criterion is eventually tried against the call, once
    for _ in range(40):
        book.auto_link(budget_s=0.2)
    judged = [(i, c) for (i, c) in book._auto_tried if c == "c1"]
    assert len(judged) == len([c for c in book.criteria if c["state"] == "pending"])


def test_a_pair_is_judged_once_and_a_new_call_only_gets_the_new_pair(monkeypatch):
    book = _book()
    calls = []
    real = verification.verify_contract_link

    def spy(crit, call, **kw):
        calls.append((crit["id"], call["call_id"]))
        return real(crit, call, **kw)
    monkeypatch.setattr(verification, "verify_contract_link", spy)
    _run(book, "c1", "python -m arc scan x.arc", "nothing to see")
    book.auto_link()
    n = len(calls)
    assert n == len([c for c in book.criteria if c["state"] == "pending"]) and {c for _, c in calls} == {"c1"}
    book.auto_link()
    assert len(calls) == n, "the same pairs were judged again"
    _run(book, "c2", "python -m arc scan y.arc", "still nothing")
    book.auto_link()
    assert {c for _, c in calls[n:]} == {"c2"}
    # an edit starts over: the old pairs are gone with their calls
    book.record_call("e1", "write_file", '{"path": "a.py"}', {"output": "ok"}, kind="mutation", paths=["a.py"])
    assert book._auto_tried == set()


def test_the_async_pass_does_not_start_a_thread_when_nothing_new_could_match(monkeypatch):
    book = _book()
    started = []
    monkeypatch.setattr(book, "auto_plan", lambda *a, **k: started.append(1) or [])

    async def go():
        silent = _run(book, "s1", "python -m arc scan x.arc", None, exit_code=0)
        assert await book.auto_link_async([silent]) == []
        viewer = _run(book, "v1", "cat log.txt", "damaged archive fails with corrupt")
        assert await book.auto_link_async([viewer]) == []
        edit = book.record_call("e1", "write_file", '{"path": "a.py"}', {"output": "ok"},
                                kind="mutation", paths=["a.py"])
        assert await book.auto_link_async([edit]) == []
        assert not started
        run = _run(book, "r1", "python -m arc scan x.arc", "something")
        await book.auto_link_async([run])
        assert started == [1]
        # the pair was judged inside the (patched-out) plan only if it ran; mark it so
        for c in book.criteria:
            book._auto_tried.add((c["id"], "r1"))
        await book.auto_link_async([run])
        assert started == [1]
        for c in book.criteria:
            c["state"] = "unmet"
        run2 = _run(book, "r2", "python -m arc scan z.arc", "something")
        assert await book.auto_link_async([run2]) == [] and started == [1]
    asyncio.run(go())


# -- wording --------------------------------------------------------------------

def test_the_setting_text_says_what_the_code_does():
    import pathlib
    import src.agent_settings_schema as schema
    text = pathlib.Path(schema.__file__).read_text(encoding="utf-8")
    start = text.index('"agent_contract_criteria_auto_link"')
    block = text[start:start + 1500]
    assert "one shared word never counts" not in block
    assert "three distinct words" in block and "at most three requirements" in block
    assert verification.AUTO_MIN_WORDS == 3


def test_auto_link_is_experimental_and_off_by_default():
    import pathlib
    import src.agent_settings_schema as schema
    from src.settings import DEFAULT_SETTINGS
    assert DEFAULT_SETTINGS["agent_contract_criteria_auto_link"] is False
    text = pathlib.Path(schema.__file__).read_text(encoding="utf-8")
    start = text.index('"agent_contract_criteria_auto_link"')
    block = text[start:start + 600]
    assert "Experimental" in block and "off by default" in block
    loop_src = (pathlib.Path(schema.__file__).parent / "agent_loop.py").read_text(encoding="utf-8")
    assert 'get_setting("agent_contract_criteria_auto_link", False)' in loop_src