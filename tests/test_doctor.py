"""
tests/test_doctor.py — the report that says what this machine can do.

Its three rules are the interesting part, and all three are about NOT lying:

* nothing reports `ok` that was not checked. A probe that could not run is
  `unknown` with the reason. This caught a real wrong answer the first time
  the doctor ran: the skills check swallowed a `TypeError` and reported "no
  skills stored", which is a different claim from "I could not look";
* every finding that is not OK carries a fix. "docker: unavailable" sends
  somebody to a search engine; "the daemon did not answer — start Docker
  Desktop" sends them to the taskbar;
* a missing capability is `absent`, not `fail`. No ComfyUI is a fact about
  the machine. Painting every unused capability red teaches people to ignore
  the report.

Machine-independent on purpose: what is asserted is the SHAPE of the answer
and the rules, never "docker is up here", because whether it is up on the
machine running the suite is not a property of this code.
"""
from __future__ import annotations

import pytest

from src import doctor


def test_the_report_has_a_finding_for_every_area_and_a_worst():
    report = doctor.run()
    assert report["worst"] in doctor.STATES
    areas = {f["area"] for f in report["findings"]}
    assert {"runtime", "backends", "execution", "coding", "media",
            "approvals", "workflows", "skills"} <= areas
    for finding in report["findings"]:
        assert finding["state"] in doctor.STATES
        assert finding["name"] and finding["area"]


def test_anything_that_is_not_ok_says_what_to_do_about_it():
    """The line that makes the difference between a diagnosis and a shrug.
    `absent` for a roadmap item is allowed to say so and nothing more."""
    for finding in doctor.run()["findings"]:
        if finding["state"] in ("ok", "absent"):
            continue
        assert finding["fix"], (
            f"{finding['area']}/{finding['name']} is {finding['state']} and "
            f"offers no fix: {finding['detail']}")


def test_a_probe_that_blows_up_is_unknown_and_never_ok(monkeypatch):
    """The rule this exists for. An `unknown` rounded up to `ok` is how
    somebody spends an evening on a feature that was never going to work."""
    def explode():
        raise RuntimeError("the disk caught fire")

    monkeypatch.setattr(doctor, "_git", explode)
    report = doctor.run(areas=["runtime"])
    git = next(f for f in report["findings"] if f["name"] == "git")
    assert git["state"] == "unknown"
    assert "the check itself failed" in git["detail"]
    assert "the disk caught fire" in git["detail"]
    assert report["worst"] != "ok"


def test_a_check_that_could_not_look_does_not_report_an_empty_result(monkeypatch):
    """The exact wrong answer the doctor gave the first time it ran: the
    skills probe swallowed a TypeError and said "no skills stored", which is
    a claim about the machine rather than about the check."""
    import builtins

    real_import = builtins.__import__

    def broken(name, *a, **kw):
        if name == "services.memory.skills":
            raise TypeError("SkillsManager.__init__() missing 1 argument")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", broken)
    finding = doctor._skills()
    assert finding.state == "unknown"
    assert "could not be read" in finding.detail
    assert "says nothing about whether skills are installed" in finding.fix


def test_a_capability_nobody_installed_is_absent_and_not_a_failure():
    """`remote_worker` is Phase 6 and has no code. That is a fact about this
    build, and a report that painted it red would train everyone to skim."""
    report = doctor.run(areas=["backends"])
    remote = next(f for f in report["findings"] if f["name"] == "remote_worker")
    assert remote["state"] == "absent"
    assert report["ok"] is True or report["worst"] == "fail"


def test_asking_for_one_area_answers_about_that_area_only():
    report = doctor.run(areas=["runtime"])
    assert {f["area"] for f in report["findings"]} == {"runtime"}
    assert len(report["findings"]) >= 3


# ── the rendering ─────────────────────────────────────────────────────────

def test_the_rendering_leads_with_problems_and_hides_the_working_ones():
    report = {
        "checked_at": "2026-09-04T15:00:00Z", "worst": "warn", "ok": True,
        "counts": {"ok": 2, "warn": 1},
        "findings": [
            {"area": "runtime", "name": "python", "state": "ok",
             "detail": "3.13", "fix": "", "facts": {}},
            {"area": "backends", "name": "docker_workspace", "state": "ok",
             "detail": "up", "fix": "", "facts": {}},
            {"area": "backends", "name": "media_worker", "state": "warn",
             "detail": "nothing answered", "fix": "start ComfyUI", "facts": {}},
        ],
    }
    text = doctor.render(report)
    assert "WORTH A LOOK" in text
    assert "backends/media_worker: nothing answered" in text
    assert "→ start ComfyUI" in text
    # the working ones are one summary line, not three
    assert "runtime/python: 3.13" not in text
    assert "working: backends/docker_workspace, runtime/python" in text

    loud = doctor.render(report, verbose=True)
    assert "runtime/python: 3.13" in loud


def test_the_rendering_groups_by_state_so_a_heading_appears_once():
    """Grouping by area would print the same header once per state something
    in that area happens to be in, which is how a short report starts looking
    like a long one."""
    report = {
        "checked_at": "t", "worst": "warn", "ok": True, "counts": {},
        "findings": [
            {"area": "backends", "name": "a", "state": "warn", "detail": "x",
             "fix": "f", "facts": {}},
            {"area": "backends", "name": "b", "state": "absent", "detail": "y",
             "fix": "", "facts": {}},
            {"area": "backends", "name": "c", "state": "warn", "detail": "z",
             "fix": "f", "facts": {}},
        ],
    }
    text = doctor.render(report)
    assert text.count("WORTH A LOOK") == 1
    assert text.count("NOT ON THIS MACHINE") == 1


# ── the CLI ───────────────────────────────────────────────────────────────

def test_the_exit_code_is_zero_unless_something_is_actually_broken(monkeypatch, capsys):
    """A machine with no ComfyUI is not a broken machine. A doctor that
    exited non-zero for every absent capability would be useless in a script."""
    monkeypatch.setattr(doctor, "run", lambda **kw: {
        "checked_at": "t", "worst": "absent", "ok": True, "counts": {},
        "findings": []})
    assert doctor.main([]) == 0

    monkeypatch.setattr(doctor, "run", lambda **kw: {
        "checked_at": "t", "worst": "fail", "ok": False, "counts": {},
        "findings": [{"area": "runtime", "name": "data directory",
                      "state": "fail", "detail": "gone", "fix": "run setup.py",
                      "facts": {}}]})
    assert doctor.main([]) == 1
    assert "run setup.py" in capsys.readouterr().out


def test_the_cli_can_answer_as_json(monkeypatch, capsys):
    import json

    monkeypatch.setattr(doctor, "run", lambda **kw: {
        "checked_at": "t", "worst": "ok", "ok": True, "counts": {"ok": 1},
        "findings": [{"area": "runtime", "name": "python", "state": "ok",
                      "detail": "3.13", "fix": "", "facts": {}}]})
    assert doctor.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["worst"] == "ok"
