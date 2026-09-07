"""The Context Engine watches the turn without touching it (FAUSTUS §20).

Phase 1 of the plan is the only phase in which the engine is allowed to be
wrong for free: it compiles the packet it *would* have delivered, puts it beside
the prompt the app really sent, and changes nothing.  That freedom lasts
exactly as long as the four promises in `src/context_engine/wiring.py` hold,
and every one of them is a specific, expensive failure if it does not:

* a shadow that mutated the message list would change the answer it was
  supposed to be measuring, and the measurement would then be of itself;
* a shadow that raised would turn a diagnostic nobody asked for into a dead
  turn the user did ask for;
* a shadow that blocked would add its own retrieval to every turn's latency,
  which is precisely the cost the engine exists to remove;
* a shadow that recompiled on every round would multiply that cost by nine and
  learn nothing the first round had not already said.

The last test in the file is a wiring test in the style of
`tests/test_context_ledger_wiring.py`: the promises above are worth nothing if
the call is spliced into `agent_loop.py` in the wrong place, or outside a
`try`, so the file is read as text and the shape of the splice is asserted.
"""

import asyncio
import copy
import json
from pathlib import Path

import pytest

from src.context_engine import compiler as compiler_module
from src.context_engine import wiring

ROOT = Path(__file__).resolve().parents[1]
LOOP = (ROOT / "src" / "agent_loop.py").read_text(encoding="utf-8")
ROUTES = (ROOT / "routes" / "chat_routes.py").read_text(encoding="utf-8")


#: What `ContextCompiler.shadow` hands back, trimmed to the keys the wiring
#: reads.  A literal rather than a real compilation: these tests are about the
#: seam, and a real packet would make them fail for reasons in `compiler.py`.
SHADOW = {
    "packet_id": "ctxpkt_0123456789abcdef",
    "estimator": "heuristic",
    "packet_tokens": 1200,
    "sent_tokens": 4100,
    "delta_tokens": -2900,
    "messages": 3,
    "by_section": [{"section": "retrieved_memory", "packet_tokens": 300,
                    "packet_items": 2, "sent_tokens": 0, "sent_messages": 0,
                    "delta_tokens": 300}],
    "sent_by_bucket": {"system": {"tokens": 900, "messages": 1}},
    "would_add": [{"source_type": "memory", "source_ref": "mem:prefers-pathlib",
                   "tokens": 40, "transformation": "verbatim"}],
    "would_drop": [{"section": "tool_guidance", "tokens": 2600, "messages": 1}],
    "omitted": [{"source_type": "document", "source_ref": "doc:handbook",
                 "reason": "budget", "score": 0.4, "recoverable": True,
                 "detail": "the window was already spent"}],
    "mention_check": "heuristic: source_ref or its basename found in the text",
    "packet": {"sections": [{"kind": "retrieved_memory",
                             "items": [{"body": "the user prefers pathlib"}]}]},
    "summary": {"degraded": False, "warnings": ["the semantic lane was down"]},
    "delivered": False,
}

MESSAGES = [
    {"role": "system", "content": "You are Faustus."},
    {"role": "user", "content": "owner: admin\nwhy does the build fail?",
     "metadata": {"trusted": False, "source": "memory"}},
    {"role": "user", "content": "why does the build fail?"},
]


class FakeCompiler:
    """A compiler that counts, and does whatever the test asked it to do.

    `calls` is the point of it: several of these tests are about the compiler
    *not* being reached, and an assertion on a return value cannot tell the
    difference between "did not run" and "ran and returned nothing"."""

    def __init__(self, *, report=None, error=None, delay=0.0, mutate=False):
        self.report = SHADOW if report is None else report
        self.error = error
        self.delay = delay
        self.mutate = mutate
        self.calls = []

    async def shadow(self, request, *, messages, **kw):
        self.calls.append({"request": request, "messages": messages, "kw": kw})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if self.mutate:
            # A future version of the engine that forgot rule 1.  The caller's
            # list must survive it anyway.
            for row in messages:
                row["injected"] = "the engine wrote here"
        return copy.deepcopy(self.report)


@pytest.fixture()
def flags(monkeypatch):
    """`src.settings.get_setting`, answering from a dict the test can edit."""
    import src.settings as settings_module

    values = {
        "agent_context_engine": False,
        "agent_context_engine_shadow": True,
        "agent_context_timeout_ms": 2000,
    }
    monkeypatch.setattr(settings_module, "get_setting",
                        lambda key, default=None: values.get(key, default))
    return values


@pytest.fixture()
def fake(monkeypatch):
    """Install a `FakeCompiler` where `compiler()` is looked up."""
    def install(**kw):
        double = FakeCompiler(**kw)
        monkeypatch.setattr(compiler_module, "compiler", lambda: double)
        return double

    return install


def _request(**over):
    fields = dict(owner="luis", session_id="s1", model="test-model",
                  workspace="/repo", project_id="p1", messages=MESSAGES,
                  agent_mode=True)
    fields.update(over)
    return wiring.build_request(**fields)


# ── rule 1: an observation may not change what it observes ─────────────────

async def test_shadow_round_never_touches_the_messages_it_was_shown(flags, fake):
    """The whole exit criterion of Phase 1 is "no afecta respuestas"."""
    double = fake(mutate=True)
    messages = copy.deepcopy(MESSAGES)
    before = copy.deepcopy(messages)
    first = messages[0]

    report = await wiring.shadow_round(request=_request(), messages=messages)

    assert report is not None and double.calls, "the compiler must have run"
    assert messages == before, "the content changed"
    assert messages[0] is first, "the list was rebuilt underneath the caller"
    assert "injected" not in messages[0], "the engine reached the real dict"


async def test_the_report_carries_no_bodies(flags, fake):
    """The packet was not delivered.  Shipping the text of the memories it
    would have contained to a browser, once per turn, to prove that they were
    not sent, is a trade nobody makes on purpose."""
    fake()
    report = await wiring.shadow_round(request=_request(), messages=MESSAGES)
    assert "packet" not in report
    assert "the user prefers pathlib" not in json.dumps(report)


# ── rule 2: a measurement may not end a turn ───────────────────────────────

async def test_shadow_round_survives_a_compiler_that_raises(flags, fake):
    double = fake(error=RuntimeError("the retrieval layer fell over"))
    assert await wiring.shadow_round(request=_request(),
                                     messages=MESSAGES) is None
    assert double.calls, "the failure has to come from the compiler"


async def test_shadow_round_returns_nothing_when_there_is_no_packet(flags, fake):
    """`shadow()` catches its own failures and reports them in-band; a report
    with no packet is not a measurement and must not reach the screen."""
    fake(report={"error": "shadow compilation failed", "delivered": False,
                 "packet": None, "summary": None})
    assert await wiring.shadow_round(request=_request(),
                                     messages=MESSAGES) is None


# ── rule 3: a measurement may not cost a second ────────────────────────────

async def test_shadow_round_gives_up_instead_of_delaying_the_turn(flags, fake,
                                                                  monkeypatch):
    monkeypatch.setattr(wiring, "ASSEMBLY_ALLOWANCE_MS", 0)
    flags["agent_context_timeout_ms"] = 20
    double = fake(delay=5.0)

    started = asyncio.get_running_loop().time()
    report = await wiring.shadow_round(request=_request(), messages=MESSAGES)
    elapsed = asyncio.get_running_loop().time() - started

    assert report is None
    assert double.calls, "the compiler was started and then cancelled"
    assert elapsed < 2.0, f"the turn waited {elapsed:.2f}s for a diagnostic"


def test_an_unreadable_timeout_falls_back_instead_of_becoming_zero(flags):
    """A zero-second deadline would fail every observation and look like a
    broken engine rather than a broken setting."""
    flags["agent_context_timeout_ms"] = 0
    assert wiring.timeout_s() > 0
    flags["agent_context_timeout_ms"] = "not a number"
    assert wiring.timeout_s() > 0


# ── rule 4: once per turn, not once per round ──────────────────────────────

async def test_shadow_round_measures_the_first_round_and_no_other(flags, fake):
    double = fake()
    assert await wiring.shadow_round(request=_request(), messages=MESSAGES,
                                     round_index=0) is not None
    assert len(double.calls) == 1
    for later in (1, 2, 8):
        assert await wiring.shadow_round(request=_request(), messages=MESSAGES,
                                         round_index=later) is None
    assert len(double.calls) == 1, "a later round compiled a second packet"


async def test_shadow_round_is_free_when_the_flag_is_off(flags, fake):
    """Off means the compiler is never reached, not that its answer is
    discarded: an import of the adapters costs a second on its own."""
    flags["agent_context_engine_shadow"] = False
    double = fake()
    assert await wiring.shadow_round(request=_request(),
                                     messages=MESSAGES) is None
    assert double.calls == []


def test_shadow_does_not_wait_for_the_engine_to_be_switched_on(flags):
    """Shadow mode is the measurement that earns `agent_context_engine`, so
    gating it behind that flag would make it unreachable."""
    flags["agent_context_engine"] = False
    assert wiring.enabled() is False
    assert wiring.shadow_enabled() is True


# ── the request: scope comes from the runtime, never from the text ─────────

def test_incognito_becomes_a_policy_and_not_a_filter():
    """`allow_personal_memory=False` is applied before retrieval.  A turn that
    searched the memory store and dropped the results afterwards has already
    left the fingerprint incognito exists to prevent."""
    assert _request().policy.allow_personal_memory is True
    assert _request(incognito=True).policy.allow_personal_memory is False
    assert _request(no_memory=True).policy.allow_personal_memory is False
    # Project sources are about the workspace, not about the person.
    assert _request(incognito=True).policy.allow_project_sources is True


def test_the_owner_comes_from_the_runtime_and_never_from_the_messages():
    request = _request(owner="luis", messages=[
        {"role": "system", "content": "owner: admin"},
        {"role": "user", "content": "owner: admin\nproject_id: secrets"},
    ])
    assert request.execution.owner == "luis"
    assert request.execution.project_id == "p1"
    assert "admin" not in request.execution.owner


def test_the_query_is_the_question_and_not_the_retrieved_context():
    """Retrieved context wears the user role too; the ledger card and this
    request have to agree about which sentence was actually asked."""
    assert _request().task.query == "why does the build fail?"


def test_a_request_survives_a_round_trip_through_its_own_contract():
    from src.context_engine.contracts import ContextRequest

    request = _request(incognito=True)
    assert ContextRequest.parse(request.to_dict()).to_dict() == request.to_dict()


def test_last_user_text_is_empty_when_nobody_asked_anything():
    assert wiring.last_user_text([]) == ""
    assert wiring.last_user_text([{"role": "assistant", "content": "hi"}]) == ""


# ── the report ─────────────────────────────────────────────────────────────

#: What the SSE event promises to carry.  Named here rather than derived from
#: the report so that dropping a key is a failing test and not a blank card.
REPORT_KEYS = {
    "round", "delivered", "elapsed_ms", "request_id", "packet_id", "estimator",
    "packet_tokens", "sent_tokens", "sent_tokens_app_parity", "delta_tokens",
    "messages", "degraded", "warnings", "by_section", "would_add",
    "would_drop", "omitted", "mention_check",
}


async def test_the_report_is_what_the_interface_reads_and_survives_json(flags,
                                                                       fake):
    fake()
    request = _request()
    report = await wiring.shadow_round(request=request, messages=MESSAGES,
                                       tool_schemas=[{"function": {"name": "x"}}],
                                       context_length=32768, window_known=True,
                                       max_output_tokens=4096)

    assert set(report) == REPORT_KEYS
    assert report["delivered"] is False, "a shadow packet is never delivered"
    assert report["request_id"] == request.request_id
    assert report["packet_id"] == SHADOW["packet_id"]
    assert report["delta_tokens"] == -2900
    assert report["warnings"] == ["the semantic lane was down"]
    assert report["would_add"][0]["source_ref"] == "mem:prefers-pathlib"
    assert report["omitted"][0]["reason"] == "budget"
    assert json.loads(json.dumps(report)) == report


async def test_the_report_prices_the_sent_prompt_the_way_the_ledger_does(flags,
                                                                        fake):
    """Two cards sit side by side; if they used different rulers a reader would
    conclude one of them was broken.  The app-parity lane rounds up where
    `estimate_tokens` truncates, so they agree to within a token per string."""
    from src.context_engine.budgets import app_parity_estimator
    from src.model_context import estimate_tokens

    fake()
    report = await wiring.shadow_round(request=_request(), messages=MESSAGES)
    assert report["sent_tokens_app_parity"] == (
        app_parity_estimator().count_messages(MESSAGES))
    assert abs(report["sent_tokens_app_parity"]
               - estimate_tokens(MESSAGES)) <= 2 * len(MESSAGES)


async def test_long_lists_are_clipped_before_they_reach_the_wire(flags, fake):
    payload = dict(SHADOW)
    payload["omitted"] = [dict(SHADOW["omitted"][0]) for _ in range(500)]
    fake(report=payload)
    report = await wiring.shadow_round(request=_request(), messages=MESSAGES)
    assert len(report["omitted"]) == wiring.MAX_REPORT_ROWS


# ── the receipt: observed use only ─────────────────────────────────────────

TOOL_TURN = [
    {"role": "assistant", "content": None, "tool_calls": [
        {"function": {"name": "read_file",
                      "arguments": '{"path": "src/app.py"}'}},
        {"function": {"name": "bash",
                      "arguments": '{"command": "pytest -q"}'}},
    ]},
    {"role": "tool", "content": "1 failed"},
]


def test_observe_receipt_records_what_was_seen_and_nothing_declared(monkeypatch):
    """Asking a model which memories it used produces a number that then
    becomes training signal for the curator — which is how a system learns to
    trust its own guesses.  Phase 1 records the runtime's view and no other."""
    captured = []
    monkeypatch.setattr(compiler_module, "record_receipt", captured.append)

    wiring.observe_receipt(packet_id="ctxpkt_1", request_id="ctxreq_1",
                           messages=TOOL_TURN, tool_results=2,
                           outcome_ref="msg:42", verdict="pass")

    assert len(captured) == 1
    receipt = captured[0]
    assert receipt.packet_id == "ctxpkt_1"
    assert receipt.opened_source_refs == ("src/app.py",), "a shell command is not a source"
    assert receipt.tool_results_added == 2
    assert receipt.declared_item_ids == ()
    assert receipt.used_item_ids == ()


def test_observe_receipt_ignores_a_packet_it_cannot_name(monkeypatch):
    captured = []
    monkeypatch.setattr(compiler_module, "record_receipt", captured.append)
    wiring.observe_receipt(packet_id="", messages=TOOL_TURN)
    assert captured == []


def test_observe_receipt_never_raises(monkeypatch):
    def explode(_receipt):
        raise RuntimeError("the ledger is on a full disk")

    monkeypatch.setattr(compiler_module, "record_receipt", explode)
    wiring.observe_receipt(packet_id="ctxpkt_1", messages=TOOL_TURN)


def test_note_event_swallows_everything_including_an_unknown_name():
    """It is called from emitters that have already done their real work; an
    exception here would roll back somebody else's commit for a cache."""
    wiring.note_event("project_context_updated", {"owner": "luis",
                                                  "project_id": "p1"})
    wiring.note_event("no_such_event", {"owner": "luis"})
    wiring.note_event("", None)


# -- live delivery ---------------------------------------------------------

async def test_live_delivery_is_scoped_rendered_and_auditable(flags, monkeypatch):
    from src.context_engine.adapters.sessions import history_provider
    from src.context_engine.contracts import (
        ContextBudget, ContextItem, ContextPacket, ContextSection,
    )

    flags["agent_context_engine"] = True
    monkeypatch.setattr(wiring, "_live_budget", lambda *a, **k: 900)
    seen = {}

    class LiveCompiler:
        async def compile(self, request, **kw):
            provider = history_provider()
            seen["history"] = list(provider("s1", "luis")) if provider else []
            seen["wrong_owner"] = list(provider("s1", "admin")) if provider else []
            seen["budget"] = request.policy.token_budget
            return ContextPacket(
                packet_id="ctxpkt_live",
                request_id=request.request_id,
                owner="luis",
                session_id="s1",
                model="test-model",
                window=ContextBudget(max_tokens=4096, input_budget=900),
                sections=(
                    ContextSection(kind="recent_messages", items=(
                        ContextItem(item_id="recent", source_type="message",
                                    source_ref="session:s1#0", body="do not duplicate",
                                    tokens=4),
                    )),
                    ContextSection(kind="retrieved_memory", items=(
                        ContextItem(item_id="memory", source_type="memory",
                                    source_ref="mem:one", title="Known preference",
                                    body="Use concise prose", tokens=6),
                    )),
                ),
            )

    monkeypatch.setattr(compiler_module, "compiler", lambda: LiveCompiler())
    result = await wiring.deliver_round(
        request=_request(), messages=MESSAGES, context_length=4096,
        window_known=True, round_index=2,
    )

    assert result and result["report"]["delivered"] is True
    assert result["report"]["packet_id"] == "ctxpkt_live"
    assert result["report"]["round"] == 2
    assert result["report"]["items"] == 1
    assert result["report"]["history_carried_by_prompt"] is True
    assert "Use concise prose" in result["message"]["content"]
    assert "do not duplicate" not in result["message"]["content"]
    assert result["message"]["metadata"]["trusted"] is False
    assert result["message"]["_agent_injected"] == "context_engine"
    assert seen["history"] == MESSAGES
    assert seen["wrong_owner"] == []
    assert seen["budget"] == 900
    assert history_provider() is None, "the transcript escaped the compile scope"


async def test_live_delivery_is_free_when_disabled(flags, fake):
    flags["agent_context_engine"] = False
    double = fake()
    assert await wiring.deliver_round(request=_request(), messages=MESSAGES) is None
    assert double.calls == []


async def test_live_delivery_never_breaks_the_existing_prompt(flags, monkeypatch):
    flags["agent_context_engine"] = True
    monkeypatch.setattr(wiring, "_live_budget", lambda *a, **k: 900)

    class BrokenCompiler:
        async def compile(self, request, **kw):
            raise RuntimeError("store unavailable")

    monkeypatch.setattr(compiler_module, "compiler", lambda: BrokenCompiler())
    before = copy.deepcopy(MESSAGES)
    assert await wiring.deliver_round(request=_request(), messages=MESSAGES) is None
    assert MESSAGES == before


# ── the splice, read as text (cf. tests/test_context_ledger_wiring.py) ─────

def test_the_loop_asks_the_engine_to_watch():
    assert "from src.context_engine import wiring as _ce_wiring" in LOOP
    assert '"type": "context_shadow"' in LOOP


def test_the_loop_delivers_and_receipts_live_packets():
    assert ".deliver_round(" in LOOP
    assert '"type": "context_packet"' in LOOP
    assert "_ce_wiring.observe_receipt(" in LOOP


def test_the_engine_is_imported_lazily():
    """`agent_loop.py` is the most expensive import in the repo and it is on
    the path of every process that answers a message.  `compiler` pulls the
    adapters, which pull memory, RAG and the provenance graph; none of that
    may be paid at import time for a flag that is off by default."""
    assert "\nfrom src.context_engine" not in LOOP, "imported at module scope"
    assert "\nimport src.context_engine" not in LOOP


def test_the_shadow_measures_the_schemas_that_are_actually_sent():
    """Order matters for the same reason it matters for the ledger: measuring
    the tool schemas before they are slimmed would report a fiction."""
    slim = LOOP.index("from src.tool_slimming import slim_tool_schemas")
    shadow = LOOP.index("_ce_wiring.shadow_round(")
    assert slim < shadow


def test_the_shadow_call_can_never_break_a_round():
    block = LOOP[LOOP.index("from src.context_engine import wiring as _ce_wiring"):]
    assert "except Exception as _ce_err" in block[:2200]


def test_the_loop_compiles_once_per_turn_and_not_once_per_round():
    """Rule 4 has to survive the splice: the loop counts rounds from one."""
    assert "round_index=round_num - 1" in LOOP


def test_the_scope_is_handed_over_by_the_runtime():
    """`owner` and `project_id` are the loop's own variables; if they were ever
    parsed out of the conversation an actor could ask to be someone else."""
    block = LOOP[LOOP.index("_ce_wiring.build_request("):]
    assert "owner=owner or \"\"" in block[:900]
    assert "project_id=str(_hopts.get(\"project_id\") or \"\")" in block[:900]


def test_the_chat_route_forwards_the_event_type():
    assert '"context_shadow",' in ROUTES
