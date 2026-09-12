"""The admission gate: ask before loading a model that does not fit (OBJ-1).

The night of 08-09-2026 two 27B models were resident at once and the machine
went down. Ollama never refuses a load; Faustus has to. These pin what the
gate decides, who it asks, and â€” above all â€” that it never loads on silence.
"""
import asyncio

import pytest

from src import vram_admission as va
from src import vram_fit

GIB = 2**30
ROOT = "http://127.0.0.1:11434"
EP = ROOT + "/v1/chat/completions"


@pytest.fixture(autouse=True)
def clean_tables():
    # HW-01 reservations are a new, separate global (src/vram_admission.py):
    # a "fits, go ahead" admit() now reserves the room until assess() sees the
    # model resident or the reservation's TTL passes (see
    # release_reservations_for_model). No test here loads anything for real,
    # so nothing ever reports resident, and a reservation left behind by one
    # test would eat into the next test's budget — hence clearing it here too.
    vram_fit.KV_RATES.clear()
    va._PENDING.clear()
    va._RESERVATIONS.clear()
    yield
    vram_fit.KV_RATES.clear()
    va._PENDING.clear()
    va._RESERVATIONS.clear()


def _card(total_gb: float, used_gb: float):
    return {"supported": True, "name": "GeForce", "total": int(total_gb * GIB),
            "used": int(used_gb * GIB), "free": int((total_gb - used_gb) * GIB),
            "gpus": [{"index": 0, "name": "GeForce", "uuid": "u0", "total": int(total_gb * GIB),
                      "used": int(used_gb * GIB), "free": int((total_gb - used_gb) * GIB)}]}


def _ollama(monkeypatch, *, tags, ps, vram):
    """One fake Ollama + one fake card."""
    def _get(root, path, timeout):
        return {"models": tags} if path == "/api/tags" else {"models": ps}
    monkeypatch.setattr(va, "_get", _get)
    monkeypatch.setattr("src.gpu_shared_memory.vram_snapshot", lambda: vram)
    monkeypatch.setattr("src.gpu_placement.placement", lambda root, loaded, gpus: {})


Q8 = {"name": "qwen3.8:27b-q8_0", "size": 29 * GIB, "digest": "d-q8"}
Q4 = {"name": "qwen3.8:27b-q4_K_M", "size": 17 * GIB, "digest": "d-q4"}
SMALL = {"name": "qwen3.5:9b", "size": 6 * GIB, "digest": "d-9b"}


def test_only_this_machine_s_ollama_is_gated():
    assert va.ollama_root(EP) == ROOT
    assert va.ollama_root("http://localhost:11434/v1") == "http://localhost:11434"
    assert va.ollama_root("http://192.168.1.20:11434/v1/chat/completions") is None  # LAN: not our card
    assert va.ollama_root("http://127.0.0.1:8080/v1/chat/completions") is None      # llama.cpp: no /api/ps
    assert va.ollama_root("faustus-cli://codex/x") is None
    assert va.ollama_root("https://api.openai.com/v1/chat/completions") is None


def test_the_night_of_08_09_is_refused(monkeypatch):
    """q8 resident (spilling), q4 wants in: alongside there is no room."""
    _ollama(monkeypatch, tags=[Q8, Q4],
            ps=[{"name": Q8["name"], "digest": "d-q8", "size": 33 * GIB,
                 "size_vram": 26 * GIB, "context_length": 65536}],
            vram=_card(28, 27))
    a = va.assess(ROOT, Q4["name"])

    assert a["fits"] is False
    assert a["measured"] is False          # q4 never loaded: weights-only floor
    assert a["residents"][0]["name"] == Q8["name"]
    assert a["residents"][0]["spill_bytes"] == 7 * GIB
    assert a["shortfall_bytes"] > 0
    assert a["suggestion"] == [Q8["name"]]  # one big one beats three small ones
    assert a["suggestion_enough"] is True


def test_the_same_model_already_inside_is_not_a_load(monkeypatch):
    _ollama(monkeypatch, tags=[Q8, Q4],
            ps=[{"name": Q4["name"], "digest": "d-q4", "size": 20 * GIB, "size_vram": 20 * GIB,
                 "context_length": 32768}],
            vram=_card(28, 21))
    a = va.assess(ROOT, Q4["name"])
    assert a["fits"] is True and a.get("already_resident") is True


def test_an_empty_card_fits_and_learns_nothing_false(monkeypatch):
    _ollama(monkeypatch, tags=[Q8, Q4, SMALL], ps=[], vram=_card(28, 0.5))
    a = va.assess(ROOT, SMALL["name"])
    assert a["fits"] is True
    assert a["residents"] == []


def test_a_measured_footprint_is_what_is_judged(monkeypatch):
    """The KV cache learned while q4 was resident counts once it is evicted."""
    vram_fit.remember_kv_rate("d-q4", per_token=100_000.0, ctx=65536)   # â‰ˆ 6.1 GB of cache
    _ollama(monkeypatch, tags=[Q4, SMALL],
            ps=[{"name": SMALL["name"], "digest": "d-9b", "size": 7 * GIB, "size_vram": 7 * GIB,
                 "context_length": 8192}],
            vram=_card(28, 7.5))
    a = va.assess(ROOT, Q4["name"])
    assert a["measured"] is True
    assert a["kv_bytes"] == 100_000 * 65536
    assert a["footprint_bytes"] == 17 * GIB + a["kv_bytes"]
    # 28 âˆ’ 0.8 reserve âˆ’ 0.5 others âˆ’ 7 held = 19.7 GB alongside; 17 + 6.1 + 0.5 = 23.6 needed.
    assert a["fits"] is False
    assert a["suggestion"] == [SMALL["name"]]


def test_no_gpu_reading_means_no_verdict(monkeypatch):
    _ollama(monkeypatch, tags=[Q4], ps=[], vram={"supported": False, "reason": "no nvidia-smi"})
    a = va.assess(ROOT, Q4["name"])
    assert a["fits"] is None and "nvidia" in a["reason"]


# â”€â”€ The gate itself â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@pytest.fixture
def blocked(monkeypatch):
    """q8 inside, q4 asking, and an Ollama that really evicts on request."""
    state = {"ps": [{"name": Q8["name"], "digest": "d-q8", "size": 33 * GIB,
                     "size_vram": 26 * GIB, "context_length": 65536}],
             "evicted": []}

    def _get(root, path, timeout):
        return {"models": [Q8, Q4]} if path == "/api/tags" else {"models": state["ps"]}

    def _evict(root, name):
        state["evicted"].append(name)
        state["ps"] = [m for m in state["ps"] if m["name"] != name]
        return True

    monkeypatch.setattr(va, "_get", _get)
    monkeypatch.setattr(va, "_evict", _evict)
    monkeypatch.setattr("src.gpu_shared_memory.vram_snapshot", lambda: _card(28, 27))
    monkeypatch.setattr("src.gpu_placement.placement", lambda root, loaded, gpus: {})
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _real_sleep(0))
    return state


async def _answer_later(action, names=None, by="luis"):
    """The click, arriving while admit() is waiting.

    Polled on a real wall-clock deadline, not a fixed count of event-loop
    turns: the previous ``for _ in range(50): await asyncio.sleep(0)`` assumed
    admit() reaches the ask branch within 50 scheduling opportunities, which
    is not guaranteed once it awaits a real ``asyncio.to_thread()`` round trip
    (assess() runs in a worker thread) — exactly why it flaked under load.
    Same shape as ``unload_and_wait``'s own real-time deadline loop just below
    in src/vram_admission.py, not a new pattern for this codebase.
    """
    deadline = asyncio.get_event_loop().time() + 5.0
    while asyncio.get_event_loop().time() < deadline:
        if va._PENDING:
            tid = next(iter(va._PENDING))
            return va.resolve(tid, action=action, names=names, by=by)
        await asyncio.sleep(0.01)
    raise AssertionError("admit() never asked")


def test_unload_then_load_waits_for_the_eviction(blocked):
    events = []

    async def run():
        gate = asyncio.create_task(va.admit(EP, Q4["name"], owner="luis", on_progress=events.append,
                                            mode="ask", timeout=5))
        await _answer_later("unload", [Q8["name"]])
        return await gate

    assert asyncio.run(run()) == "proceed"
    assert blocked["evicted"] == [Q8["name"]]
    phases = [e["phase"] for e in events]
    assert phases[0] == "vram_blocked"
    assert "unloading_model" in phases
    asked = events[0]
    assert asked["residents"][0]["name"] == Q8["name"]
    assert asked["suggestion"] == [Q8["name"]]
    assert asked["ticket"].startswith("va-")
    assert not va._PENDING  # the question is gone once answered


def test_cancel_refuses_the_load_with_the_reason(blocked):
    async def run():
        gate = asyncio.create_task(va.admit(EP, Q4["name"], mode="ask", timeout=5))
        await _answer_later("cancel")
        return await gate

    with pytest.raises(va.AdmissionCancelled) as err:
        asyncio.run(run())
    assert "does not fit in VRAM" in str(err.value)
    assert blocked["evicted"] == []


def test_proceed_anyway_loads_without_unloading(blocked):
    events = []

    async def run():
        gate = asyncio.create_task(va.admit(EP, Q4["name"], mode="ask", timeout=5,
                                            on_progress=events.append))
        await _answer_later("proceed")
        return await gate

    assert asyncio.run(run()) == "proceed"
    assert blocked["evicted"] == []
    assert any(e["phase"] == "warning" and "spill" in e["message"] for e in events)


def test_silence_never_loads(blocked):
    """Nobody answers: the load is cancelled, not waved through."""
    with pytest.raises(va.AdmissionCancelled) as err:
        asyncio.run(va.admit(EP, Q4["name"], mode="ask", timeout=0.05))
    assert "nobody chose" in str(err.value)
    assert blocked["evicted"] == []
    assert not va._PENDING


def test_auto_mode_evicts_the_suggestion_itself(blocked):
    assert asyncio.run(va.admit(EP, Q4["name"], mode="auto")) == "proceed"
    assert blocked["evicted"] == [Q8["name"]]
    assert not va._PENDING


def test_off_mode_does_not_even_look(blocked, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("assess() must not run when the gate is off")
    monkeypatch.setattr(va, "assess", _boom)
    assert asyncio.run(va.admit(EP, Q4["name"], mode="off")) == "proceed"


def test_a_model_that_fits_never_asks(monkeypatch):
    _ollama(monkeypatch, tags=[Q4, SMALL], ps=[], vram=_card(28, 0.5))
    assert asyncio.run(va.admit(EP, SMALL["name"], mode="ask")) == "proceed"
    assert not va._PENDING


def test_resolve_only_accepts_resident_names_and_known_actions(blocked):
    t = va.open_ticket(ROOT, Q4["name"], va.assess(ROOT, Q4["name"]), owner="luis")
    with pytest.raises(ValueError):
        va.resolve(t.id, action="unload", names=["not-loaded:latest"])   # nothing resident picked
    with pytest.raises(ValueError):
        va.resolve(t.id, action="explode")
    assert va.resolve(t.id, action="unload", names=[Q8["name"], "not-loaded:latest"]) is True
    assert t.decision["names"] == [Q8["name"]]                           # the stray name is dropped
    assert va.resolve(t.id, action="cancel") is False                     # answered once only



def test_load_anyway_is_judged_against_free_ram_too(monkeypatch):
    """"Load anyway" puts the shortfall in system RAM. With little RAM free
    that is the 08-09 crash; the dialog is told so and the button turns red."""
    import types
    _ollama(monkeypatch, tags=[Q8, Q4],
            ps=[{"name": Q8["name"], "digest": "d-q8", "size": 33 * GIB,
                 "size_vram": 26 * GIB, "context_length": 65536}],
            vram=_card(28, 27))
    fake_psutil = types.SimpleNamespace(virtual_memory=lambda: types.SimpleNamespace(available=12 * GIB, total=128 * GIB))
    monkeypatch.setitem(__import__("sys").modules, "psutil", fake_psutil)
    a = va.assess(ROOT, Q4["name"])
    assert a["ram_available_bytes"] == 12 * GIB
    assert a["spill_if_forced_bytes"] == a["shortfall_bytes"]
    assert a["forced_load_dangerous"] is True          # 16.5+ GB into 12 GB free

    fake_psutil.virtual_memory = lambda: types.SimpleNamespace(available=100 * GIB, total=128 * GIB)
    a = va.assess(ROOT, Q4["name"])
    assert a["forced_load_dangerous"] is False


# â”€â”€ waited_out: INF-03's queue-wait reading â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def test_waited_out_untouched_when_there_is_no_door_at_all():
    # A remote/non-Ollama endpoint never engages the gate: no measured wait,
    # ever â€” the caller must read this as absent, not a fabricated zero.
    out = {}
    assert asyncio.run(va.admit("http://192.168.1.20:11434/v1", Q4["name"], waited_out=out)) == "proceed"
    assert out == {}


def test_waited_out_untouched_when_admission_is_off(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("assess() must not run when the gate is off")
    monkeypatch.setattr(va, "assess", _boom)
    out = {}
    assert asyncio.run(va.admit(EP, Q4["name"], mode="off", waited_out=out)) == "proceed"
    assert out == {}


def test_waited_out_measured_when_a_model_fits_immediately(monkeypatch):
    _ollama(monkeypatch, tags=[Q4, SMALL], ps=[], vram=_card(28, 0.5))
    out = {}
    assert asyncio.run(va.admit(EP, SMALL["name"], mode="ask", waited_out=out)) == "proceed"
    assert "waited_s" in out
    assert isinstance(out["waited_s"], float)
    assert out["waited_s"] >= 0.0


def test_waited_out_measured_in_auto_mode(blocked):
    out = {}
    assert asyncio.run(va.admit(EP, Q4["name"], mode="auto", waited_out=out)) == "proceed"
    assert out.get("waited_s") is not None
    assert out["waited_s"] >= 0.0


def test_waited_out_measured_through_an_ask_and_unload_round_trip(blocked):
    out = {}

    async def run():
        gate = asyncio.create_task(va.admit(EP, Q4["name"], owner="luis", mode="ask", timeout=5,
                                            waited_out=out))
        await _answer_later("unload", [Q8["name"]])
        return await gate

    assert asyncio.run(run()) == "proceed"
    assert out.get("waited_s") is not None
    assert out["waited_s"] >= 0.0


def test_waited_out_measured_even_when_cancelled(blocked):
    out = {}

    async def run():
        gate = asyncio.create_task(va.admit(EP, Q4["name"], mode="ask", timeout=5, waited_out=out))
        await _answer_later("cancel")
        return await gate

    with pytest.raises(va.AdmissionCancelled):
        asyncio.run(run())
    assert out.get("waited_s") is not None
    assert out["waited_s"] >= 0.0
