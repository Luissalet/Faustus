"""tests/test_media_runs_outbox.py — B-016: a render nothing points at.

`start()` used to commit a `pending` row, send the workflow, and only then
write down the engine's job id. A process killed between the send and the
second commit left a real render consuming a GPU while the row insisted it had
never reached the engine — and with the client id hard-coded to `faustus`,
nothing on the engine said which run had asked for it. That job could not be
polled, cancelled, or collected. Ever.

So the tests here are about the gap. The row is written as an OUTBOX entry
first, carrying a client id derived from the run; the failure that cannot rule
out acceptance is recorded as `submit_unknown` rather than `failed`; and
`reconcile()` asks the engine whether it is holding a job with that id.

The headline is the crash the audit asked for: a socket that dies at the exact
moment after the server accepted the prompt.

FakeComfy's queue and history entries stop at index 2. A real ComfyUI's carry
`extra_data` at index 3, which is the only place the client id survives, so
the tests that need correlation write that shape in by hand and say so.
"""
from __future__ import annotations

import pytest

from src import media_runs
from src.media_backends import ComfyUIBackend, ComfyUIError
from tests.test_media_runs import world  # noqa: F401 — the fixture, reused whole

ASK = {"prompt": "a ceramic mug on a white background", "aspect_ratio": "4:5"}


def queue_entry(number, prompt_id, client_id, prompt=None):
    """A /queue item shaped the way the real engine shapes one."""
    return [number, prompt_id, prompt or {}, {"client_id": client_id}, []]


def crash_after_the_server_accepts(monkeypatch):
    """Submit for real, then lose the answer on the way home.

    This is the exact window: the prompt is on the engine and its id is not on
    the row. Patched at the backend rather than at the socket because what
    matters is which side of the accept the failure lands on.
    """
    real_submit = ComfyUIBackend.submit

    def submit_then_die(self, plan, **kwargs):
        real_submit(self, plan, **kwargs)
        raise ComfyUIError("call_failed",
                           "ConnectionResetError: the socket died after the "
                           "server accepted the prompt")

    monkeypatch.setattr(ComfyUIBackend, "submit", submit_then_die)


# ── the outbox ────────────────────────────────────────────────────────────

def test_the_intention_is_on_the_row_before_anything_is_sent(world, monkeypatch):
    """An outbox entry, not a hopeful `pending`: by the time the engine is
    spoken to, the database already says what we are about to ask for and
    under what name."""
    seen = {}
    real_submit = ComfyUIBackend.submit

    def look_first(self, plan, **kwargs):
        seen["rows"] = media_runs.recent()
        return real_submit(self, plan, **kwargs)

    monkeypatch.setattr(ComfyUIBackend, "submit", look_first)
    started = media_runs.start("image.product", ASK, owner="luis")

    assert [r["status"] for r in seen["rows"]] == ["submit_pending"]
    assert seen["rows"][0]["client_id"] == started["client_id"]


def test_the_engine_is_told_which_run_asked_for_the_render(world):
    """A constant `faustus` correlates nothing: two renders in one queue are
    indistinguishable, which is why a lost id used to be lost for good."""
    started = media_runs.start("image.product", ASK)
    sent = world.submitted[0]

    assert sent["client_id"] == f"faustus:{started['run_id']}"
    assert started["client_id"] == sent["client_id"]


def test_a_graph_the_engine_refuses_is_a_failure_not_an_open_question(world):
    """The classification matters both ways. The engine read this one and said
    no, so nothing is queued and there is nothing to reconcile."""
    world.node_errors = {"5": {"class_type": "KSampler",
                               "errors": [{"message": "seed out of range"}]}}
    out = media_runs.start("image.product", ASK)

    assert out["ok"] is False and out["status"] == "failed"
    assert media_runs.get(out["run_id"])["status"] == "failed"
    assert media_runs.reconcile(grace_seconds=0)["checked"] == 0


# ── the crash the audit asked for ─────────────────────────────────────────

def test_a_crash_right_after_the_server_accepts_the_prompt_is_recoverable(world, monkeypatch):
    """The headline. The engine has the job; the answer never got home. The
    run must not claim it never reached the engine, and the render must not
    become an orphan burning a GPU that nothing can stop or collect."""
    crash_after_the_server_accepts(monkeypatch)
    started = media_runs.start("image.product", ASK, owner="luis")
    run_id = started["run_id"]

    assert started["ok"] is False
    assert len(world.submitted) == 1, "the engine really did take the job"
    assert media_runs.get(run_id)["status"] == "submit_unknown", (
        "the engine is holding this job and the row says the send is settled")
    assert media_runs.get(run_id)["engine_job_id"] is None
    assert started["status"] == "submit_unknown"
    assert started["recoverable"] is True

    # The real engine's queue carries our client id in extra_data.
    world.pending = [queue_entry(1, "p1", started["client_id"],
                                 world.submitted[0]["prompt"])]

    settled = media_runs.reconcile(grace_seconds=0)
    assert settled["adopted"] == [run_id]

    adopted = media_runs.get(run_id)
    assert adopted["status"] == "submitted"
    assert adopted["engine_job_id"] == "p1"

    # And from here it is an ordinary render again.
    world.finish("p1")
    done = media_runs.poll(run_id)
    assert done["status"] == "completed"
    assert len(done["artifacts"]) == 1


def test_polling_an_unsettled_run_reconciles_it_rather_than_writing_it_off(world, monkeypatch):
    """Polling is the moment somebody is actually asking about a render, so it
    is the right moment to find out the answer — not to repeat the row's
    stale claim that nothing was ever sent."""
    crash_after_the_server_accepts(monkeypatch)
    started = media_runs.start("image.product", ASK)
    world.pending = [queue_entry(1, "p1", started["client_id"])]

    out = media_runs.poll(started["run_id"])
    assert out["engine_job_id"] == "p1"
    assert out["status"] in ("queued", "running")
    assert "never reached the engine" not in out.get("detail", "")


def test_an_orphan_the_engine_already_finished_is_found_by_its_metadata(world, monkeypatch):
    """The collector for outputs nobody knows about. The render finished while
    Faustus was not looking; its history entry still carries the client id, so
    the run adopts the job and collects what it made."""
    crash_after_the_server_accepts(monkeypatch)
    started = media_runs.start("image.product", ASK, owner="luis")
    run_id = started["run_id"]

    world.finish("p1")
    world.history["p1"]["prompt"] = [1, "p1", {},
                                     {"client_id": started["client_id"]}, []]
    world.pending = []
    # FakeComfy serves /history/{id} but not the bare listing the real engine
    # answers on /history, so that one call is stood in for here.
    monkeypatch.setattr(ComfyUIBackend, "history_listing",
                        lambda self, **kw: dict(world.history))

    settled = media_runs.reconcile(grace_seconds=0)
    assert settled["adopted"] == [run_id]
    assert media_runs.get(run_id)["engine_job_id"] == "p1"

    done = media_runs.poll(run_id)
    assert done["status"] == "completed" and len(done["artifacts"]) == 1


def test_a_prompt_that_never_reached_the_queue_is_recorded_as_failed(world, monkeypatch):
    """The other honest answer. The engine is reachable and holds nothing of
    ours, so the send genuinely did not land — and saying so is what stops the
    run sitting in `submit_unknown` forever."""
    crash_after_the_server_accepts(monkeypatch)
    started = media_runs.start("image.product", ASK)
    world.pending = []
    monkeypatch.setattr(ComfyUIBackend, "history_listing", lambda self, **kw: {})

    settled = media_runs.reconcile(grace_seconds=0)
    assert settled["never_queued"] == [started["run_id"]]
    row = media_runs.get(started["run_id"])
    assert row["status"] == "failed"
    assert "never reached the queue" in row["reason"]


def test_an_unreachable_engine_decides_nothing(world, monkeypatch):
    """A status written on a guess is the bug one level up. With the engine
    down, `submit_unknown` is still the truth."""
    crash_after_the_server_accepts(monkeypatch)
    started = media_runs.start("image.product", ASK)
    world.stop()

    settled = media_runs.reconcile(grace_seconds=0)
    assert settled["undecided"] == [started["run_id"]]
    assert settled["runs"][0]["reason"] == "engine_unreachable"
    assert media_runs.get(started["run_id"])["status"] == "submit_unknown"


def test_a_submit_that_may_still_be_in_flight_is_left_alone(world, monkeypatch):
    """The grace window. A sweep must not adopt or condemn a render whose
    sender is still mid-call — that worker is going to write the id itself."""
    crash_after_the_server_accepts(monkeypatch)
    started = media_runs.start("image.product", ASK)
    world.pending = [queue_entry(1, "p1", started["client_id"])]

    settled = media_runs.reconcile()          # the default grace
    assert settled["adopted"] == []
    assert settled["runs"][0]["reason"] == "too_soon"
    assert media_runs.get(started["run_id"])["status"] == "submit_unknown"


# ── doing it twice ────────────────────────────────────────────────────────

def test_cancelling_a_cancelled_render_is_not_an_error(world):
    """A retried click, a retried request and a cleanup pass all ask for the
    same state, and the run is in it."""
    started = media_runs.start("image.product", ASK)
    first = media_runs.cancel(started["run_id"])
    assert first["ok"] is True and first["status"] == "cancelled"

    second = media_runs.cancel(started["run_id"])
    assert second["ok"] is True
    assert second["idempotent"] is True and second["reason"] == "already_cancelled"


def test_cancelling_an_unsettled_run_stops_the_job_not_just_the_row(world, monkeypatch):
    """The orphan-making move in one line: mark the row cancelled while the
    engine keeps rendering. Reconciling first is what makes the cancel real."""
    crash_after_the_server_accepts(monkeypatch)
    started = media_runs.start("image.product", ASK)
    world.pending = [queue_entry(1, "p1", started["client_id"])]

    out = media_runs.cancel(started["run_id"])
    assert out["ok"] is True and out["status"] == "cancelled"
    assert world.pending == [], "the row was cancelled and the job left on the engine"
    assert media_runs.get(started["run_id"])["engine_job_id"] == "p1"


def test_collecting_the_same_outputs_twice_downloads_them_once(world):
    """The crash between the download and the status write. The artifacts are
    already in the store, so the second pass finishes the sentence rather than
    fetching everything again."""
    started = media_runs.start("image.product", ASK)
    world.finish(started["engine_job_id"])
    first = media_runs.poll(started["run_id"])
    assert len(first["artifacts"]) == 1

    # As if the process had died after `_collect` and before the status write.
    media_runs._update(started["run_id"], status="queued", ended_at=None)
    downloads = len([c for c in world.calls if c[:2] == ("GET", "/view")])

    again = media_runs.poll(started["run_id"])
    assert again["status"] == "completed"
    assert len([c for c in world.calls if c[:2] == ("GET", "/view")]) == downloads, (
        "it downloaded outputs it had already stored")
    assert media_runs.get(started["run_id"])["artifact_ids"] == \
        [a["id"] for a in first["artifacts"]], "the second pass lost the artifacts"


# ── the correlation itself ────────────────────────────────────────────────

def test_the_backend_finds_a_queued_job_by_the_client_id_it_was_given(world, monkeypatch):
    engine = ComfyUIBackend(client_id="faustus:mrun_probe")
    world.pending = [queue_entry(1, "p9", "faustus:mrun_probe"),
                     queue_entry(2, "p10", "faustus:somebody_else")]
    # A miss falls through to the history listing, which FakeComfy does not
    # serve on the bare /history the real engine answers on.
    monkeypatch.setattr(ComfyUIBackend, "history_listing", lambda self, **kw: {})

    assert engine.find_by_client_id("faustus:mrun_probe") == {
        "found": True, "prompt_id": "p9", "where": "queued"}
    assert engine.find_by_client_id("faustus:nobody")["found"] is False
    assert engine.find_by_client_id("")["found"] is False


def test_the_backend_finds_a_running_job_too(world):
    engine = ComfyUIBackend(client_id="faustus:mrun_probe")
    world.running = [queue_entry(1, "p11", "faustus:mrun_probe")]

    found = engine.find_by_client_id("faustus:mrun_probe")
    assert found == {"found": True, "prompt_id": "p11", "where": "running"}


def test_a_queue_entry_without_extra_data_correlates_with_nothing(world, monkeypatch):
    """Older engines and older jobs carry no client id. Reading one as a match
    would adopt somebody else's render, which is worse than not finding ours."""
    engine = ComfyUIBackend(client_id="faustus:mrun_probe")
    world.pending = [[1, "p12", {}]]
    monkeypatch.setattr(ComfyUIBackend, "history_listing", lambda self, **kw: {})

    assert engine.find_by_client_id("faustus:mrun_probe")["found"] is False
