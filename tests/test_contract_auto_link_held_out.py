"""Precision of the harness's own linking, second round: the false proofs seen on
six tasks the rules were not tuned on. Every case below is a small anonymised
output shaped like one of them (no task output is stored): pytest summaries
that are failures, file-name keys, words without anything to show, label
echoes, limits that are not measurements, relations that names cannot show and
HTTP statuses the output never prints."""
import importlib.util
import pathlib

import pytest

from src import contract_criteria as cc
from src import verification as ver


def _crit(cid, quote, **kw):
    base = {"id": cid, "quote": quote, "state": "pending", "kind": "behaviour", "subject": None,
            "keys": [], "words": [], "matchers": [], "limits": [], "exits": [], "statuses": []}
    base.update(kw)
    return base


def _call(output, command="python -m meter report db --by hour", exit_code=0, call_id="c1"):
    return {"call_id": call_id, "tool": "bash", "seq": 1, "command": command, "output": output,
            "exit_code": exit_code, "mutation": False}


def _auto(crit, call, df=None, terms=None):
    return ver.verify_contract_link(crit, call, strict=True, auto=True, df=df, terms_out=terms).state


# -- 1. failures the summary line of a test run shows ---------------------------------

@pytest.mark.parametrize("line", [
    "3 failed, 20 passed in 1.2s",
    "1 error in 0.3s",
    "2 errors in 0.5s",
    "ERROR tests/test_cli.py - ImportError: cannot import name x",
    "=== 1 failed in 0.1s ===",
])
def test_pytest_summaries_with_failures_or_errors_are_hard_failures(line):
    assert ver._HARD_FAIL_RE.search(line)


@pytest.mark.parametrize("line", ["20 passed in 1.2s", "0 failed, 20 passed", "0 errors", "FAIL: 0", "no failures"])
def test_clean_summaries_are_not_failures(line):
    assert not ver._HARD_FAIL_RE.search(line)


def test_a_criterion_about_tests_is_never_proven_by_a_run_with_errors():
    crit = _crit("C005", "The tests of `test_cli` and `cli_ok` pass with exit status 0.",
                 keys=["test_cli", "cli_ok"], exits=[0])
    shown = '{"test_cli": 1, "cli_ok": 1}\n'
    for summary in ("1 error in 0.3s", "3 failed, 20 passed in 1.2s", "ERROR tests/test_cli.py - ImportError: x"):
        assert _auto(crit, _call(shown + summary, command="python -m pytest -q"), df={}) == "pending", summary
    assert _auto(crit, _call(shown + "23 passed in 1.2s", command="python -m pytest -q"), df={}) == "verified"


def test_only_a_criterion_about_a_crash_may_be_proven_by_a_traceback():
    crash = _crit("C1", "A broken archive prints a traceback with `corrupt` and `crc_error`.",
                  keys=["corrupt", "crc_error"])
    out = ('{"corrupt": 1, "crc_error": 2}\nTraceback (most recent call last):\n'
           '  File "a.py", line 1\nValueError: x')
    assert _auto(crash, _call(out, exit_code=1), df={}) == "verified"
    plain = _crit("C2", "A healthy archive reports `corrupt` and `crc_error`.", keys=["corrupt", "crc_error"])
    assert _auto(plain, _call(out, exit_code=1), df={}) == "pending"


# -- 2. a file name says the file exists, not what it holds ------------------------------

@pytest.mark.parametrize("key", ["requirements.txt", "README.md", "cmd.exe", "settings.json", "data.csv",
                                 "pyproject.toml", "setup.cfg", "tool.ini", "notes.md", "schema.sql.db"])
def test_file_name_keys_are_generic(key):
    assert ver.generic_key(key)


def test_an_identifier_key_is_not_generic():
    assert not ver.generic_key("energy_wh") and not ver.generic_key("time_zone")


def test_listing_a_file_does_not_prove_what_the_sentence_says_about_it():
    lists = _crit("C1", "`requirements.txt` lists `tzdata` for the time zone tables.",
                  keys=["requirements.txt", "tzdata"])
    assert _auto(lists, _call("requirements.txt\ntzdata", command="ls"), df={}) == "pending"
    assert _auto(lists, _call("4 requirements.txt", command="grep -c tzdata requirements.txt"), df={}) == "pending"
    shell = _crit("C2", "`cmd.exe` is not launched from the shell here.", keys=["cmd.exe"])
    assert _auto(shell, _call("cmd.exe", command="ls"), df={}) == "pending"
    readme = _crit("C3", "The README explains the installation of `README.md` steps.", keys=["README.md"])
    assert _auto(readme, _call("README.md", command="ls"), df={}) == "pending"


# -- 3. words alone, and the sentence that is the task statement ------------------------

def test_words_alone_need_a_number_or_an_identifier_of_the_sentence():
    words = ["rounding", "exact", "group"]
    plain = _crit("C1", "Rounding happens once on the exact group sums.", words=words)
    assert _auto(plain, _call("rounding exact group"), df={}) == "pending"
    anchored = _crit("C2", "Rounding happens once on the exact group sums to 2 decimals.", words=words)
    assert _auto(anchored, _call("rounding exact group"), df={}) == "pending"
    assert _auto(anchored, _call("rounding exact group sums 2 decimals"), df={}) == "verified"


def test_the_first_criterion_is_the_task_statement_and_is_never_auto_proven():
    quote = "Build the archive_tool package with scan, pack and unpack for archive_tool users."
    rows = [_crit("C01", quote, words=["archive_tool", "package", "scan", "unpack"]),
            _crit("C02", quote.replace("Build", "Check"), words=["archive_tool", "package", "scan", "unpack"])]
    book = cc.CriteriaBook(rows)
    book.record_call("c1", "bash", "python -m archive_tool scan x.arc",
                     {"exit_code": 0, "output": "archive_tool package scan unpack"}, kind="shell")
    assert book.auto_link() == ["C02"]
    assert book.get("C01")["state"] == "pending"


# -- 4. a script that prints its own label proves the label ---------------------------

@pytest.mark.parametrize("line", [
    "[OK C012: group objects have exactly the keys energy_wh and intervals]",
    "PASS the group object has energy_wh and intervals",
    "ok C033 energy_wh intervals",
])
def test_a_label_echo_proves_nothing(line):
    crit = _crit("C9", "A group object shows `energy_wh` and `intervals`.", keys=["energy_wh", "intervals"])
    assert _auto(crit, _call(line), df={}) == "pending"
    assert _auto(crit, _call('{"energy_wh": "1.0", "intervals": 1}'), df={}) == "verified"
    assert ver.strip_label_echo(line + "\nreal line") == "real line"


def test_text_typed_into_the_command_is_not_output():
    crit = _crit("C9", "A group object shows `energy_wh` and `intervals`.", keys=["energy_wh", "intervals"])
    typed = '{"energy_wh": "1.0", "intervals": 1} expected'
    call = _call(typed, command='python -c "print(\'' + typed + '\')"')
    assert _auto(crit, call, df={}) == "pending"


# -- 5. limits: a measurement of the operation, within the bound, not zero -------------

def test_a_limit_needs_a_real_measurement_of_the_measured_operation_within_its_bound():
    quote = "The peak working set of `report` stays under 64 MB."
    limit = {"num": "64", "unit": "MB"}
    near = ["report", "peak"]
    assert ver._limit_measured("report peak working set 12 MB", limit, quote, near) is True
    assert ver._limit_measured("report peak working set 0 MB", limit, quote, near) is False   # degenerate
    assert ver._limit_measured("report peak working set 90 MB", limit, quote, near) is False  # over the bound
    assert ver._limit_measured("peak working set 12 MB", limit, quote, ["report", "scan"]) is False
    assert ver._limit_measured("12 MB", limit, quote, near) is False                           # names nothing
    assert ver._limit_measured("report peak working set 20000 KB", limit, quote, near) is True  # unit conversion


def test_a_test_run_time_is_not_the_time_of_an_operation():
    quote = "A report over 2000 readings completes in under 2 s."
    limit = {"num": "2", "unit": "s"}
    assert ver._limit_measured("4 passed in 0.99s", limit, quote, ["report", "readings"]) is False
    assert ver._limit_measured("report of 2000 readings took 0.99s", limit, quote, ["report", "readings"]) is True
    assert ver._limit_measured("report of 2000 readings took 3.5s", limit, quote, ["report", "readings"]) is False
    crit = _crit("C128", quote, limits=[limit], keys=["report_time"])
    assert _auto(crit, _call("4 passed in 0.99s"), df={}) == "pending"


def test_a_lower_bound_is_shown_by_a_value_at_or_over_it():
    quote = "At least 4 lines of context are shown per match."
    limit = {"num": "4", "unit": "lines"}
    assert ver._limit_measured("match context 2 lines", limit, quote, ["context", "match"]) is False
    assert ver._limit_measured("match context 5 lines", limit, quote, ["context", "match"]) is True


# -- 6. relations and statuses that names cannot show ---------------------------------

def test_a_condition_is_not_shown_by_the_names_in_it():
    crit = _crit("C042", "The database holds at most one stored reading per `reading_id` and `meter_id`.",
                 keys=["reading_id", "meter_id"])
    columns = "1 ['reading_id', 'meter_id', 'read_at', 'register_wh', 'rev'] None"
    assert _auto(crit, _call(columns), df={}) == "pending"
    plain = _crit("C043", "The table has the columns `reading_id` and `meter_id`.", keys=["reading_id", "meter_id"])
    assert _auto(plain, _call(columns), df={}) == "verified"


def test_a_sentence_about_http_statuses_needs_a_status_in_the_output():
    crit = _crit("C105", "Errors have the error document as body and the status `422` for `invalid` and "
                         "`404` for `not_found`.", keys=["invalid", "not_found"], statuses=[422, 404])
    cli = '{"error": {"code": "invalid", "message": "x"}}\n{"error": {"code": "not_found", "message": "y"}}'
    assert _auto(crit, _call(cli, exit_code=1), df={}) == "pending"
    http = cli + "\nHTTP/1.1 422 Unprocessable Entity"
    assert _auto(crit, _call(http, command="curl -i http://localhost:8000/x"), df={}) == "verified"


def test_the_keys_of_a_sentence_have_to_show_together():
    crit = _crit("C036", "Create a project (`time_zone`, `holidays`) and fail on `bad_input`.",
                 keys=["time_zone", "holidays", "bad_input"])
    project = '{"id": "p1", "time_zone": "Europe/Madrid", "holidays": []}'
    error = '{"error": {"code": "bad_input", "message": "state"}}'
    spread = "\n".join([project, "{}", "{}", "{}", "{}", "{}", error])
    assert _auto(crit, _call(spread), df={}) == "pending"
    assert _auto(crit, _call(project + "\n" + error), df={}) == "verified"


# -- 7. the replay script reads `**exit_code:** N` --------------------------------------

def _replay_module():
    path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "replay_contract_auto_link.py"
    spec = importlib.util.spec_from_file_location("replay_contract_auto_link_t", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_replay_reads_the_exit_code_line_of_a_tool_result():
    replay = _replay_module()
    content = "### bash: python -m meter\n⏱ 0.2s\n```\n{\"error\": 1}\n```\n**exit_code:** 3"
    got = replay._tool_result(content)
    assert got == {"output": '{"error": 1}', "exit_code": 3}
    assert replay._tool_result("### bash: ls\n```\nx\n```\n**exit_code:** 0")["exit_code"] == 0
    assert replay._tool_result('{"output": "x", "exit_code": 2}')["exit_code"] == 2
