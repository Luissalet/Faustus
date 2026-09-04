"""
tests/test_media_pool.py — two engines, and a reason for every choice.

This machine has two GPUs and ComfyUI takes exactly one, so using both means
running two engines — and the moment there are two, something has to decide
which gets a job and be able to say why.

The rule is **least busy first, then the smallest card that fits**. That reads
backwards until you see what the other order does: a 512px draft does not need
12 GB, and putting it on the big card leaves the job that DOES need 12 GB
queued behind three that did not.

This is NOT the LLM side's rule and should not be confused with it.
`gpu_placement_prefer` (src/gpu_policy.py) is a number a person sets — "fill
card N first", default -1 for Ollama's own choice — and it exists because a
language model is resident for hours and wants to live wholly on one card,
where pinning one that does not fit is worse than letting Ollama split it.
A render is transient: it takes seconds and gives the card back. Nobody
configures this one, and nothing here reads that setting.

What is pinned here is mostly refusals — an engine that does not answer, an
engine without the model — and the thing that makes them useful, which is that
"no engine available" is never the whole answer. Every engine that was looked
at comes back with what disqualified it.
"""
from __future__ import annotations

import pytest

from src.media_backends import pool
from src.media_backends.pool import Engine


PLAN = {"graph": {"1": {"class_type": "CheckpointLoaderSimple", "inputs": {}}},
        "models": [{"name": "sd15.safetensors", "kind": "checkpoint"}]}


def engine(url, *, ok=True, gpu="", vram=None, queued=0, models=("sd15.safetensors",),
           reason="ready", detail=""):
    return Engine(url, ok, reason, detail or f"{gpu}", gpu, vram,
                  tuple(models), queued)


# ── which engine, and why ─────────────────────────────────────────────────

def test_the_smaller_card_is_filled_first():
    """Throughput, not politeness: leaving the big card free means the job
    that needs it is not queued behind three that did not."""
    big = engine("http://a:8188", gpu="RTX 4070 Ti", vram=12.0)
    small = engine("http://b:8189", gpu="RTX 5060 Ti", vram=8.0)

    picked = pool.choose(PLAN, engines=[big, small])
    assert picked["ok"] and picked["url"] == "http://b:8189"
    assert "smallest card" in picked["chosen_because"]

    # …and the order they were configured in does not decide it.
    assert pool.choose(PLAN, engines=[small, big])["url"] == "http://b:8189"


def test_a_busy_engine_loses_to_an_idle_one_even_if_it_is_smaller():
    """Queue depth beats size: an idle 12 GB card finishes sooner than an 8 GB
    one with three jobs in front."""
    small_busy = engine("http://b:8189", gpu="5060 Ti", vram=8.0, queued=3)
    big_idle = engine("http://a:8188", gpu="4070 Ti", vram=12.0, queued=0)

    picked = pool.choose(PLAN, engines=[small_busy, big_idle])
    assert picked["url"] == "http://a:8188"
    assert "least busy" in picked["chosen_because"]


def test_an_engine_that_does_not_answer_is_not_a_candidate_and_is_named():
    down = engine("http://a:8188", ok=False, reason="backend_unavailable",
                  detail="nothing answered")
    up = engine("http://b:8189", gpu="5060 Ti", vram=8.0)

    picked = pool.choose(PLAN, engines=[down, up])
    assert picked["url"] == "http://b:8189"
    reasons = {w["url"]: w for w in picked["why"]}
    assert reasons["http://a:8188"]["reason"] == "backend_unavailable"
    assert "nothing answered" in reasons["http://a:8188"]["detail"]
    assert reasons["http://b:8189"]["reason"] == "eligible"


def test_an_engine_without_the_model_is_not_a_candidate():
    """Two ComfyUIs usually share a models folder, but they do not have to —
    and "it failed on engine B" twenty minutes in is what this avoids."""
    wrong = engine("http://a:8188", vram=12.0, models=("sdxl.safetensors",))
    right = engine("http://b:8189", vram=8.0, models=("sd15.safetensors",))

    picked = pool.choose(PLAN, engines=[wrong, right])
    assert picked["url"] == "http://b:8189"
    missing = next(w for w in picked["why"] if w["url"] == "http://a:8188")
    assert missing["reason"] == "missing_models"
    assert missing["missing_models"] == ["sd15.safetensors"]


def test_no_engine_available_is_never_the_whole_answer():
    """The least useful sentence in the system, on a machine with two of
    them. Every engine that was looked at says what disqualified it."""
    picked = pool.choose(PLAN, engines=[
        engine("http://a:8188", ok=False, reason="backend_unavailable",
               detail="nothing answered at http://a:8188"),
        engine("http://b:8189", models=("sdxl.safetensors",)),
    ])
    assert picked["ok"] is False and picked["reason"] == "no_engine"
    assert "http://a:8188: nothing answered" in picked["detail"]
    assert "does not have sd15.safetensors" in picked["detail"]
    assert len(picked["why"]) == 2


def test_asking_for_one_by_name_gets_it_when_it_can_take_the_job():
    big = engine("http://a:8188", gpu="4070 Ti", vram=12.0)
    small = engine("http://b:8189", gpu="5060 Ti", vram=8.0)
    picked = pool.choose(PLAN, engines=[big, small], prefer="http://a:8188")
    assert picked["url"] == "http://a:8188"
    assert picked["chosen_because"] == "asked for by name"


def test_asking_for_one_that_cannot_take_the_job_falls_back_and_says_so():
    """A preference is a preference, not an override: an engine without the
    model still cannot run it, and silently ignoring the request would leave
    somebody wondering why their choice did nothing."""
    wrong = engine("http://a:8188", vram=12.0, models=("sdxl.safetensors",))
    right = engine("http://b:8189", vram=8.0)
    picked = pool.choose(PLAN, engines=[wrong, right], prefer="http://a:8188")
    assert picked["ok"] and picked["url"] == "http://b:8189"
    assert any(w["reason"] == "prefer_not_eligible" for w in picked["why"])


def test_one_engine_says_it_was_the_only_one():
    picked = pool.choose(PLAN, engines=[engine("http://a:8188", vram=12.0)])
    assert picked["chosen_because"] == "the only engine that could take it"


# ── configuring the pool ──────────────────────────────────────────────────

def test_the_pool_is_one_variable_and_a_single_engine_needs_none(monkeypatch):
    monkeypatch.delenv(pool.POOL_ENV, raising=False)
    monkeypatch.delenv(pool.SINGLE_ENV, raising=False)
    assert pool.urls() == ["http://127.0.0.1:8188"]

    monkeypatch.setenv(pool.SINGLE_ENV, "http://elsewhere:9000/")
    assert pool.urls() == ["http://elsewhere:9000"]

    monkeypatch.setenv(pool.POOL_ENV,
                       "http://127.0.0.1:8188, http://127.0.0.1:8189 ,")
    assert pool.urls() == ["http://127.0.0.1:8188", "http://127.0.0.1:8189"]


def test_a_probe_that_blows_up_is_reported_not_skipped(monkeypatch):
    """A list that quietly shrank hides the interesting fact, which on a
    two-GPU machine is usually "one of them is down"."""
    class Exploding:
        def __init__(self, url):
            self.base_url = url

        def probe(self):
            raise RuntimeError("the network stack is on fire")

    monkeypatch.setattr(pool, "ComfyUIBackend", Exploding)
    found = pool.survey(base_urls=["http://a:8188"])
    assert len(found) == 1
    assert found[0].ok is False and found[0].reason == "probe_failed"
    assert "on fire" in found[0].detail


# -- what the MCP client is told ------------------------------------------

def test_two_engines_are_reported_as_two_not_as_the_first_one():
    """The catalogue tool used to say "engine: ready" in the singular. On a
    two-GPU box that hides the fact that matters most when a render fails for
    a missing model: the OTHER engine may well have it."""
    from mcp_servers.workers_server import render_media_recipes

    state = {"ok": True, "ready": 1, "configured": 2,
             "engines": [
                 {"url": "http://a:8188", "ok": True, "gpu": "RTX 4070 Ti",
                  "vram_gb": 12.0, "queued": 0,
                  "checkpoints": ["sd15.safetensors"]},
                 {"url": "http://b:8189", "ok": False, "reason": "unreachable",
                  "detail": "connection refused"},
             ]}
    text = render_media_recipes({"workflows": []}, state)

    assert "1 of 2 ready" in text
    assert "RTX 4070 Ti (12.0 GB)" in text
    assert "sd15.safetensors" in text
    # The one that is down is named with its reason, not silently dropped.
    assert "http://b:8189" in text and "unreachable" in text


def test_one_engine_still_reads_as_one_engine():
    """Most machines have one card. The pool must not make that sound like a
    cluster with a single node."""
    from mcp_servers.workers_server import render_media_recipes

    state = {"ok": True, "ready": 1, "configured": 1,
             "detail": "ComfyUI on RTX 4070 Ti",
             "checkpoints": ["sd15.safetensors"],
             "engines": [{"url": "http://a:8188", "ok": True}]}
    text = render_media_recipes({"workflows": []}, state)

    assert text.startswith("engine: ready")
    assert "of 1 ready" not in text


def test_a_render_says_which_card_it_landed_on_and_why():
    """"It is slow" and "it queued behind the other one" are different
    problems, and the second is invisible without this line."""
    from mcp_servers.workers_server import render_media_run

    text = render_media_run({
        "ok": True, "run_id": "mr_1", "status": "queued",
        "workflow": "image.quick-draft", "version": "v1",
        "engine_gpu": "RTX 4070 Ti", "engine_url": "http://a:8188",
        "chosen_because": "smallest card that fits the job (12.0 GB), "
                          "leaving the bigger one free"})

    assert "on RTX 4070 Ti: smallest card that fits" in text


def test_a_render_with_nothing_to_explain_still_says_where_it_ran():
    """A stored row keeps the engine url but not the reasoning. Half the
    answer is still the useful half."""
    from mcp_servers.workers_server import render_media_run

    text = render_media_run({"ok": True, "run_id": "mr_2", "status": "completed",
                             "engine_url": "http://a:8188"})
    assert "on http://a:8188" in text
