"""Spec lint for dispatched jobs (src/dispatch_spec_lint.py, src/dispatch.py,
POST /api/dispatch/lint): is a task specified well enough for a worker that
cannot ask a question?"""
from __future__ import annotations

import asyncio

import pytest

from src import dispatch, dispatch_spec_lint as lint
from tests.test_dispatch import box, _client, _close_clients  # noqa: F401


@pytest.fixture(autouse=True)
def no_real_verification(monkeypatch):
    """A job's verify step would run the real command in the workspace (a whole
    pytest run for a spec that names one): these tests are about what happens
    BEFORE the workers start, so the verification is a stub."""
    monkeypatch.setattr(dispatch, "run_verification",
                        lambda *a, **k: {"ran": False, "ok": True, "summary": "skipped in this test"})

VAGUE = "improve the settings page"
GOOD = {
    "instruction": ("Add apply_tax(total, rate) to cart.py so it returns round(total * (1 + rate), 2). "
                    "Do not change the signature of checkout() or any other public function in cart.py."),
    "files": ["cart.py", "tests/test_cart.py"],
}
GOOD_JOB = {"verify": "pytest tests/test_cart.py -q"}


def codes(problems):
    return [p["code"] for p in problems]


# -- the checks ----------------------------------------------------------------------------------

def test_a_vague_job_is_told_what_is_missing():
    problems = lint.validate_task_spec(VAGUE)
    assert {"no_files", "no_verify", "vague_verbs"} <= set(codes(problems))
    for p in problems:
        assert p["message"] and p["fix"]


def test_a_good_job_has_no_problems():
    assert lint.validate_task_spec(GOOD, job=GOOD_JOB) == []


def test_the_module_function_and_the_dispatch_entry_point_agree():
    assert dispatch.validate_task_spec(VAGUE) == lint.validate_task_spec(VAGUE)
    assert dispatch.validate_task_spec(GOOD, GOOD_JOB) == []


def test_no_file_named_or_scoped():
    assert "no_files" in codes(lint.validate_task_spec("Fix the bug where totals are wrong. Run pytest -q."))
    assert "no_files" not in codes(lint.validate_task_spec("Fix totals in src/cart.py. Run pytest -q."))
    assert "no_files" not in codes(lint.validate_task_spec({"instruction": "Fix totals", "files": ["cart.py"]}))
    assert "no_files" not in codes(lint.validate_task_spec("Fix the function `apply_tax`. Run pytest -q."))


def test_an_exact_verify_command_can_come_from_the_text_the_task_or_the_job():
    assert "no_verify" in codes(lint.validate_task_spec("Fix totals in cart.py"))
    assert "no_verify" not in codes(lint.validate_task_spec("Fix totals in cart.py, then run `pytest tests/test_cart.py -q`"))
    assert "no_verify" not in codes(lint.validate_task_spec({"instruction": "Fix totals in cart.py", "verify_command": "make test"}))
    assert "no_verify" not in codes(lint.validate_task_spec("Fix totals in cart.py", job={"verify": "npm test"}))
    # "auto" is exactly what the lint is about: the job would rely on auto-detection
    assert "no_verify" in codes(lint.validate_task_spec("Fix totals in cart.py", job={"verify": "auto"}))


def test_more_than_one_outcome_is_a_problem():
    two = "Add a /health route to app.py. Then rename the helpers in utils.py. Run pytest -q."
    assert "multiple_outcomes" in codes(lint.validate_task_spec(two))
    listed = "Changes in cart.py:\n1. add apply_tax\n2. remove the old discount code\n3. update the docs\nRun pytest -q."
    assert "multiple_outcomes" in codes(lint.validate_task_spec(listed))
    one = "Add apply_tax to cart.py and cover it with a test in tests/test_cart.py. Run pytest -q."
    assert "multiple_outcomes" not in codes(lint.validate_task_spec(one))


def test_a_vague_verb_needs_a_criterion():
    assert "vague_verbs" in codes(lint.validate_task_spec("Clean up cart.py. Run pytest -q."))
    assert "vague_verbs" not in codes(lint.validate_task_spec(
        "Clean up cart.py so that pytest -q still passes and no function is longer than 30 lines."))
    assert "vague_verbs" not in codes(lint.validate_task_spec(
        {"instruction": "Clean up cart.py. Run pytest -q.", "criteria": ["no function longer than 30 lines"]}))
    assert "vague_verbs" not in codes(lint.validate_task_spec("Add apply_tax to cart.py. Run pytest -q."))


def test_a_public_api_needs_a_do_not_change_line():
    touching = "Add an endpoint /api/tax to routes/tax.py. Run pytest -q."
    assert "public_api" in codes(lint.validate_task_spec(touching))
    guarded = touching + " Do not change the existing route paths or response fields."
    assert "public_api" not in codes(lint.validate_task_spec(guarded))
    assert "public_api" not in codes(lint.validate_task_spec("Rename a local variable in cart.py. Run pytest -q."))


def test_an_empty_task_is_one_clear_problem():
    assert codes(lint.validate_task_spec("   ")) == ["empty"]
    assert codes(lint.validate_task_spec({"instruction": ""})) == ["empty"]


def test_lint_tasks_names_each_task_and_flattens_what_to_fix():
    report = lint.lint_tasks([VAGUE, {"name": "tax", **GOOD}], job=GOOD_JOB)
    assert report["ok"] is False and report["codes"] == sorted(set(report["codes"]))
    assert [t["index"] for t in report["tasks"]] == [1, 2]
    assert report["tasks"][1]["name"] == "tax" and report["tasks"][1]["problems"] == []
    assert report["tasks"][0]["problems"]
    assert all(line.startswith("task 1") for line in report["needs_detail"])
    assert lint.lint_tasks([{"name": "tax", **GOOD}], job=GOOD_JOB)["ok"] is True


def test_the_mode_setting_is_forgiving():
    assert [lint.mode(v) for v in (None, "warn", "ENFORCE", "off", "on", "false", "nonsense")] == \
        ["warn", "warn", "enforce", "off", "warn", "off", "warn"]


# -- the job: warn attaches, enforce refuses --------------------------------------------------------

@pytest.fixture
def mode(monkeypatch):
    def setter(value):
        monkeypatch.setattr("src.settings.get_setting",
                            lambda key, default=None: value if key == "dispatch_spec_lint" else default)
    return setter


def test_in_warn_mode_a_vague_job_runs_and_carries_the_problems(box, mode):
    mode("warn")

    async def run():
        job = await dispatch.start("luis", {"tasks": [VAGUE], "workspace": box["ws"]})
        await dispatch.wait(job, 5)
        return job
    job = asyncio.run(run())
    assert box["executed"], "warn mode still runs the job"
    assert job.spec_lint["mode"] == "warn" and {"no_files", "no_verify", "vague_verbs"} <= set(job.spec_lint["codes"])
    assert job.spec_lint["needs_detail"]
    assert dispatch.compact(job)["spec_lint"]["codes"]
    assert job.to_dict()["spec_lint"]["mode"] == "warn"


def test_in_warn_mode_a_good_job_carries_no_lint(box, mode):
    mode("warn")

    async def run():
        job = await dispatch.start("luis", {"tasks": [GOOD], "workspace": box["ws"], **GOOD_JOB})
        await dispatch.wait(job, 5)
        return job
    job = asyncio.run(run())
    assert job.spec_lint is None and "spec_lint" not in job.to_dict()


def test_in_enforce_mode_a_vague_job_is_refused_before_anything_starts(box, mode):
    mode("enforce")
    with pytest.raises(dispatch.SpecNeedsDetail) as err:
        asyncio.run(dispatch.start("luis", {"tasks": [VAGUE], "workspace": box["ws"]}))
    report = err.value.report
    assert report["ok"] is False and report["needs_detail"] and "vague_verbs" in report["codes"]
    assert not box["executed"] and isinstance(err.value, ValueError)


def test_in_enforce_mode_a_good_job_runs(box, mode):
    mode("enforce")

    async def run():
        job = await dispatch.start("luis", {"tasks": [GOOD], "workspace": box["ws"], **GOOD_JOB})
        await dispatch.wait(job, 5)
        return job
    assert asyncio.run(run()).status in ("done", "partial")


def test_with_the_lint_off_nothing_is_checked(box, mode):
    mode("off")

    async def run():
        job = await dispatch.start("luis", {"tasks": [VAGUE], "workspace": box["ws"]})
        await dispatch.wait(job, 5)
        return job
    assert asyncio.run(run()).spec_lint is None


# -- the routes -----------------------------------------------------------------------------------

def test_the_lint_route_checks_without_starting_anything(box, mode, monkeypatch):
    mode("warn")
    c = _client(monkeypatch, token_scopes=["agents:dispatch"])
    r = c.post("/api/dispatch/lint", json={"tasks": [VAGUE]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and body["mode"] == "warn" and body["needs_detail"]
    assert {"no_files", "no_verify", "vague_verbs"} <= set(body["codes"])
    assert body["tasks"][0]["problems"][0]["fix"]
    good = c.post("/api/dispatch/lint", json={"tasks": [GOOD], **GOOD_JOB}).json()
    assert good["ok"] is True and good["needs_detail"] == []
    assert not box["executed"] and c.get("/api/dispatch").json()["jobs"] == []


def test_the_lint_route_rejects_a_body_with_no_tasks_and_needs_the_dispatch_scope(box, monkeypatch):
    c = _client(monkeypatch, token_scopes=["agents:dispatch"])
    assert c.post("/api/dispatch/lint", json={"tasks": []}).status_code == 400
    other = _client(monkeypatch, token_scopes=["chat"])
    assert other.post("/api/dispatch/lint", json={"tasks": [VAGUE]}).status_code == 403


def test_enforce_answers_422_needs_detail_and_warn_answers_200_with_the_lint(box, mode, monkeypatch):
    c = _client(monkeypatch, token_scopes=["agents:dispatch"])
    mode("enforce")
    r = c.post("/api/dispatch", json={"tasks": [VAGUE], "workspace": box["ws"]})
    assert r.status_code == 422
    body = r.json()
    assert body["status"] == "needs_detail" and body["needs_detail"] and "vague_verbs" in body["codes"]
    assert body["mode"] == "enforce" and body["tasks"][0]["problems"]
    assert not box["executed"]
    mode("warn")
    r = c.post("/api/dispatch", json={"tasks": [VAGUE], "workspace": box["ws"]})
    assert r.status_code == 200 and r.json()["spec_lint"]["codes"]


def test_the_lint_is_on_the_authz_surface_for_dispatch_tokens():
    from core import authz
    assert authz.api_token_allowed("POST", "/api/dispatch/lint", ["agents:dispatch"])[0]
    assert not authz.api_token_allowed("POST", "/api/dispatch/lint", ["chat"])[0]


# -- the MCP tools (workers_lint, and a refused dispatch_workers) --------------------------------------

def _ws(monkeypatch):
    from tests.test_dispatch import _load_workers_server
    return _load_workers_server(monkeypatch)


def test_the_mcp_server_lists_workers_lint(monkeypatch):
    ws = _ws(monkeypatch)
    names = [t.name for t in ws.TOOLS]
    assert "workers_lint" in names and len(names) == len(set(names))
    tool = next(t for t in ws.TOOLS if t.name == "workers_lint")
    assert tool.inputSchema["required"] == ["tasks"]


def test_workers_lint_answers_what_is_missing_and_how_to_fix_it(monkeypatch):
    ws = _ws(monkeypatch)
    seen = {}

    def fake(method, path, body=None, *a, **k):
        seen.update(method=method, path=path, body=body)
        return {**lint.lint_tasks([VAGUE]), "mode": "enforce"}
    monkeypatch.setattr(ws, "_request", fake)
    out = asyncio.run(ws.call_tool("workers_lint", {"tasks": [VAGUE], "verify": None}))[0].text
    assert seen == {"method": "POST", "path": "/api/dispatch/lint", "body": {"tasks": [VAGUE]}}
    assert out.startswith("spec lint (enforce): NOT ready") and "no_files" in out and "vague_verbs" in out
    assert "task 1 (task 1):" in out and "-> " in out and "refused" in out


def test_workers_lint_says_so_when_the_spec_is_ready(monkeypatch):
    ws = _ws(monkeypatch)
    monkeypatch.setattr(ws, "_request", lambda *a, **k: {**lint.lint_tasks([GOOD], job=GOOD_JOB), "mode": "warn"})
    out = asyncio.run(ws.call_tool("workers_lint", {"tasks": [GOOD], "verify": GOOD_JOB["verify"]}))[0].text
    assert "ready to dispatch" in out


def test_a_dispatch_refused_with_needs_detail_says_nothing_started_and_what_to_add(monkeypatch):
    ws = _ws(monkeypatch)
    report = {**lint.lint_tasks([VAGUE]), "status": "needs_detail", "mode": "enforce"}

    def refuse(*a, **k):
        raise ws.FaustusHTTPError("Faustus answered HTTP 422", 422, report)
    monkeypatch.setattr(ws, "_request", refuse)
    out = asyncio.run(ws.call_tool("dispatch_workers", {"tasks": [VAGUE], "workspace": "D:/x"}))[0].text
    assert out.startswith("job NOT started - spec lint (enforce)") and "no_verify" in out


def test_a_job_that_ran_with_a_warning_shows_the_lint_in_its_render(monkeypatch):
    ws = _ws(monkeypatch)
    job = {"id": "abc", "status": "done", "title": "t",
           "spec_lint": {"mode": "warn", "codes": ["no_files", "no_verify"],
                         "needs_detail": ["task 1: no file or folder is named - list the files"]}}
    text = ws.render(job)
    assert "SPEC LINT (warn)" in text and "no_files, no_verify" in text and "task 1: no file" in text
    assert "SPEC LINT" not in ws.render({"id": "abc", "status": "done", "title": "t"})


def test_other_http_errors_still_surface_as_errors(monkeypatch):
    ws = _ws(monkeypatch)

    def boom(*a, **k):
        raise ws.FaustusHTTPError("Faustus answered HTTP 403 for POST /api/dispatch", 403, {})
    monkeypatch.setattr(ws, "_request", boom)
    out = asyncio.run(ws.call_tool("dispatch_workers", {"tasks": ["x"], "workspace": "D:/x"}))[0].text
    assert out.startswith("Error: Faustus answered HTTP 403")