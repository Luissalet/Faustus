"""The model picker has to say which models fit on the card — before loading.

Measured in the running app: `qwen3.8:27b-q8_0` does not fit an RTX 4070 Ti's
12 GB. The GPU pill notices and says so — `⚠ PCIe spill · qwen3.8 30%↑GPU` —
but only after the model is loaded and the wait is over, with generation down
to 4 tok/s. The picker offered all five models as equals. The instrument that
knows already existed (src/gpu_shared_memory.py, src/vram_fit.py); what was
missing was carrying its answer to where the choice is made.

Two rules run through every assertion here:

  * **Never invent.** No card, no nvidia-smi, no size from Ollama → no badge
    and no verdict, rather than a confident guess.
  * **Never block.** A model that does not fit stays selectable; it is warned
    about, not disabled.
"""

import asyncio
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import gpu_shared_memory as gsm
from routes import model_routes as mr

_REPO = Path(__file__).resolve().parent.parent
_PICKER_JS = (_REPO / "static" / "js" / "modelPicker.js").read_text(encoding="utf-8")
_ROUTES = (_REPO / "routes" / "model_routes.py").read_text(encoding="utf-8")
_CSS = (_REPO / "static" / "style.css").read_text(encoding="utf-8")

GIB = 1024 ** 3
MIB = 1024 ** 2


# ── src/gpu_shared_memory.vram_snapshot: the card, before anything loads ──

def _fake_nvsmi(monkeypatch, stdout, returncode=0, exe="/usr/bin/nvidia-smi"):
    monkeypatch.setattr(gsm, "_nvidia_smi_path", lambda: exe)

    class _P:
        pass

    proc = _P()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = ""
    monkeypatch.setattr(gsm.subprocess, "run", lambda *a, **k: proc)
    gsm.reset_vram_cache()


def test_vram_snapshot_reads_the_first_card(monkeypatch):
    _fake_nvsmi(monkeypatch, "NVIDIA GeForce RTX 4070 Ti, 12282, 8461\n")
    out = gsm.vram_snapshot()
    assert out["supported"] is True
    assert out["name"] == "NVIDIA GeForce RTX 4070 Ti"
    assert out["total"] == 12282 * MIB
    assert out["used"] == 8461 * MIB
    assert out["free"] == (12282 - 8461) * MIB


def test_vram_snapshot_takes_only_the_first_gpu(monkeypatch):
    """A verdict about "some card in the box" is worse than no verdict."""
    _fake_nvsmi(monkeypatch, "Card A, 12282, 100\nCard B, 49140, 0\n")
    assert gsm.vram_snapshot()["name"] == "Card A"


def test_vram_snapshot_is_unsupported_without_nvidia_smi(monkeypatch):
    monkeypatch.setattr(gsm, "_nvidia_smi_path", lambda: None)
    gsm.reset_vram_cache()
    out = gsm.vram_snapshot()
    assert out["supported"] is False
    assert "not found" in out["reason"]
    assert "total" not in out          # no invented number to fall back on


@pytest.mark.parametrize("stdout,rc", [
    ("", 0),                                    # no rows
    ("NVIDIA, [N/A], [N/A]\n", 0),              # unparseable
    ("NVIDIA GeForce RTX 4070 Ti, 0, 0\n", 0),  # nonsense total
    ("whatever\n", 9),                          # nvidia-smi failed
])
def test_vram_snapshot_never_guesses(monkeypatch, stdout, rc):
    _fake_nvsmi(monkeypatch, stdout, returncode=rc)
    out = gsm.vram_snapshot()
    assert out["supported"] is False
    assert "total" not in out


def test_vram_snapshot_survives_a_broken_nvidia_smi(monkeypatch):
    monkeypatch.setattr(gsm, "_nvidia_smi_path", lambda: "/usr/bin/nvidia-smi")

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired("nvidia-smi", 4)

    monkeypatch.setattr(gsm.subprocess, "run", _boom)
    gsm.reset_vram_cache()
    assert gsm.vram_snapshot()["supported"] is False


def test_vram_snapshot_is_cached(monkeypatch):
    """The picker asks on every open; forking nvidia-smi each time is silly."""
    calls = {"n": 0}
    monkeypatch.setattr(gsm, "_nvidia_smi_path", lambda: "/usr/bin/nvidia-smi")

    class _P:
        returncode = 0
        stdout = "NVIDIA GeForce RTX 4070 Ti, 12282, 0\n"
        stderr = ""

    def _run(*a, **k):
        calls["n"] += 1
        return _P()

    monkeypatch.setattr(gsm.subprocess, "run", _run)
    gsm.reset_vram_cache()
    gsm.vram_snapshot()
    gsm.vram_snapshot()
    assert calls["n"] == 1
    gsm.reset_vram_cache()


def test_the_shared_memory_diagnosis_is_untouched(monkeypatch):
    """vram_snapshot is an addition, not a rewrite: the spill rule still runs
    off the WDDM counters and still reports unsupported off Windows."""
    monkeypatch.setattr(gsm.sys, "platform", "linux")
    gsm.reset_cache()
    assert gsm.collect()["supported"] is False


# ── The arithmetic: fits / tight / over ──────────────────────────────────

@pytest.mark.parametrize("size_gib,budget_gib,expected", [
    (5, 11, "fits"),      # 6 GiB of headroom for the KV cache
    (7.5, 11, "fits"),
    (10, 11, "tight"),    # weights fit, context barely
    (11, 11, "tight"),
    (28, 11, "over"),     # the measured case: 27B-q8_0 on a 12 GB card
])
def test_fit_state(size_gib, budget_gib, expected):
    assert mr._fit_state(int(size_gib * GIB), int(budget_gib * GIB)) == expected


def test_fit_state_says_nothing_when_it_knows_nothing():
    """An empty verdict is the honest answer, and the UI paints nothing for it."""
    assert mr._fit_state(0, 11 * GIB) == ""       # size unknown
    assert mr._fit_state(7 * GIB, 0) == ""        # card unknown
    assert mr._fit_state(0, 0) == ""


def test_the_tight_band_is_wide_enough_for_a_real_kv_cache():
    """"Fits" must mean "fits with a usable context window", not "the weights
    alone happen to squeeze in" — the file on disk is not the footprint."""
    assert mr._FIT_TIGHT_HEADROOM_BYTES >= 1024 * MIB


def test_fit_note_carries_the_real_numbers_and_admits_it_is_approximate():
    note = mr._fit_note(int(7.5 * GIB), int(11.2 * GIB), 12 * GIB, "fits")
    assert "7.5 GB" in note and "11.2 GB" in note and "12.0 GB" in note
    assert "Approximate" in note
    assert "KV cache" in note


def test_fit_note_for_over_explains_the_consequence_without_alarm():
    note = mr._fit_note(28 * GIB, 11 * GIB, 12 * GIB, "over")
    assert "28.0 GB" in note
    assert "does not fit" in note
    assert "PCIe" in note
    # Not a blocker, not a scolding.
    assert "!" not in note


def test_fit_note_is_empty_without_a_verdict():
    assert mr._fit_note(7 * GIB, 11 * GIB, 12 * GIB, "") == ""


# ── The endpoint ─────────────────────────────────────────────────────────

def test_the_fit_endpoint_exists_and_is_authenticated():
    assert '@router.get("/models/fit")' in _ROUTES
    body = _ROUTES[_ROUTES.index('@router.get("/models/fit")'):]
    body = body[:body.index("# Brief cache for local-probe")]
    assert "require_user(request)" in body


def test_the_endpoint_reuses_the_existing_gpu_module():
    assert "from src import gpu_shared_memory" in _ROUTES
    assert "gpu_shared_memory.vram_snapshot()" in _ROUTES


def test_sizes_come_from_ollamas_own_catalogue():
    """`/api/tags` carries `size` per model — no new size database to keep."""
    assert '"/api/tags"' in _ROUTES or "/api/tags" in _ROUTES
    collect = _ROUTES[_ROUTES.index("def _collect_fit_hints("):]
    collect = collect[:collect.index('@router.get("/models/fit")')]
    assert "/api/tags" in collect
    # What Ollama already holds is about to be freed by switching model, so it
    # must not count against the next one.
    assert "/api/ps" in collect
    assert "used - held_by_runner" in collect


def test_only_this_machines_ollama_gets_a_verdict():
    """A tailnet/LAN Ollama is still "local" to _classify_endpoint but runs on
    someone else's card; a fit verdict there would be a confident lie."""
    fn = _ROUTES[_ROUTES.index("def _same_machine_ollama("):]
    fn = fn[:fn.index("def _collect_fit_hints(")]
    assert "_LOCAL_HOSTS" in fn
    assert "host not in _LOCAL_HOSTS" in fn


def test_without_a_card_the_endpoint_returns_sizes_but_no_verdict():
    collect = _ROUTES[_ROUTES.index("def _collect_fit_hints("):]
    collect = collect[:collect.index('@router.get("/models/fit")')]
    branch = collect[collect.index('if not vram.get("supported"):'):]
    branch = branch[:branch.index("return out") + len("return out")]
    assert '"size_bytes": size' in branch
    assert '"state"' not in branch          # no verdict without a card


def test_the_endpoint_is_cached_server_side():
    assert "_FIT_CACHE_TTL" in _ROUTES
    assert "_fit_cache" in _ROUTES


def test_the_fit_route_is_registered_on_the_router():
    router = mr.setup_model_routes(None)
    assert "/api/models/fit" in {r.path for r in router.routes}


# ── End to end: the measured case ────────────────────────────────────────
#
# A perfect helper nobody calls ships nothing, so the endpoint gets driven for
# real against the box the finding came from: an RTX 4070 Ti with 12 GB and the
# five models that were offered as equals.

class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _Db:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def query(self, model):
        return _Query(self._rows)

    def close(self):
        self.closed = True


class _Ep(SimpleNamespace):
    pass


class _FakeHttpx:
    """Minimal Ollama: /api/tags with sizes, /api/ps with nothing resident."""

    class HTTPError(Exception):
        pass

    def __init__(self, tags, ps=None):
        self._tags = tags
        self._ps = ps or []
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        payload = {"models": self._tags if url.endswith("/api/tags") else self._ps}
        return SimpleNamespace(
            status_code=200,
            json=lambda: payload,
            raise_for_status=lambda: None,
        )


# Sizes as `ollama list` reports them on the reference box.
_TAGS = [
    {"name": "qwen3.5:9b", "size": 8_200_000_000},
    {"name": "qwen3.8:27b-q8_0", "size": 28_000_000_000},
    {"name": "qwen3.8:27b-q4_K_M", "size": 16_000_000_000},
    {"name": "llama3.2:3b", "size": 2_000_000_000},
    {"name": "gemma3:12b-q6_K", "size": 10_400_000_000},
]


def _fit_payload(monkeypatch, vram, tags=_TAGS, ps=None, base="http://127.0.0.1:11434/v1"):
    router = mr.setup_model_routes(model_discovery=None)
    endpoint = None
    for route in router.routes:
        if getattr(route, "path", "") == "/api/models/fit":
            endpoint = route.endpoint
    assert endpoint is not None
    rows = [_Ep(id="local-ollama", name="Ollama", base_url=base, api_key=None,
                is_enabled=True, endpoint_kind="local")]
    fake = _FakeHttpx(tags, ps)
    monkeypatch.setattr(mr, "SessionLocal", lambda: _Db(rows))
    monkeypatch.setattr(mr, "httpx", fake)
    monkeypatch.setattr(mr.gpu_shared_memory, "vram_snapshot", lambda: vram)
    request = SimpleNamespace(
        state=SimpleNamespace(current_user="luis", api_token=False),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
    )
    return asyncio.run(endpoint(request, refresh=True)), fake


_4070TI = {"supported": True, "name": "NVIDIA GeForce RTX 4070 Ti",
           "total": 12282 * MIB, "used": 400 * MIB, "free": 11882 * MIB}


def test_the_default_model_that_did_not_fit_is_flagged(monkeypatch):
    """The whole reason this exists: qwen3.8:27b-q8_0 on a 12 GB card."""
    data, _ = _fit_payload(monkeypatch, _4070TI)
    over = data["models"]["qwen3.8:27b-q8_0"]
    assert over["state"] == "over"
    assert over["size_bytes"] == 28_000_000_000
    assert "26.1 GB" in over["note"]        # the file, in real numbers
    # …against the budget: 12282 MiB of card, minus the 800 MiB reserve, minus
    # the 400 MiB something else on the desktop is holding.
    assert "10.8 GB usable of 12.0 GB" in over["note"]


def test_the_models_that_do_fit_are_not_cried_over(monkeypatch):
    data, _ = _fit_payload(monkeypatch, _4070TI)
    assert data["models"]["qwen3.5:9b"]["state"] == "fits"
    assert data["models"]["llama3.2:3b"]["state"] == "fits"
    # 9.7 GiB of weights against 10.8 GiB of budget: the weights load, but
    # there is barely a gigabyte left for the KV cache.
    assert data["models"]["gemma3:12b-q6_K"]["state"] == "tight"


def test_quantisations_of_the_same_model_get_their_own_verdict(monkeypatch):
    """Matching on the base name would hand q4_K_M the q8_0's 28 GB — the exact
    11 GB error src/vram_fit.py's docstring warns about."""
    data, _ = _fit_payload(monkeypatch, _4070TI)
    assert data["models"]["qwen3.8:27b-q8_0"]["state"] == "over"
    assert data["models"]["qwen3.8:27b-q4_K_M"]["state"] == "over"
    assert (data["models"]["qwen3.8:27b-q4_K_M"]["size_bytes"]
            != data["models"]["qwen3.8:27b-q8_0"]["size_bytes"])


def test_what_ollama_already_holds_does_not_count_against_the_next_model(monkeypatch):
    """Picking another model unloads the current one, so its VRAM is about to
    come back. Counting it would make every row read "over" mid-session."""
    loaded = dict(_4070TI, used=8861 * MIB, free=3421 * MIB)
    ps = [{"name": "qwen3.5:9b", "size_vram": 8461 * MIB}]
    data, _ = _fit_payload(monkeypatch, loaded, ps=ps)
    assert data["vram"]["held_by_runner_bytes"] == 8461 * MIB
    assert data["models"]["qwen3.5:9b"]["state"] == "fits"
    # Budget is the card minus the reserve minus what someone *else* holds:
    # 12282 - 800 - (8861 - 8461) MiB. The 8.3 GiB the runner is sitting on
    # does not appear anywhere in that sum.
    assert data["vram"]["budget_bytes"] == 11082 * MIB


def test_without_a_card_sizes_ship_but_no_verdict_does(monkeypatch):
    data, _ = _fit_payload(monkeypatch, {"supported": False, "reason": "nvidia-smi: not found"})
    assert data["vram"]["supported"] is False
    entry = data["models"]["qwen3.8:27b-q8_0"]
    assert entry["size_bytes"] == 28_000_000_000
    assert "state" not in entry and "note" not in entry


def test_an_unreachable_ollama_produces_no_rows_rather_than_zeroes(monkeypatch):
    class _Dead(_FakeHttpx):
        def get(self, url, **kwargs):
            raise OSError("connection refused")

    router = mr.setup_model_routes(model_discovery=None)
    endpoint = [r.endpoint for r in router.routes if getattr(r, "path", "") == "/api/models/fit"][0]
    rows = [_Ep(id="local-ollama", name="Ollama", base_url="http://127.0.0.1:11434/v1",
                api_key=None, is_enabled=True, endpoint_kind="local")]
    monkeypatch.setattr(mr, "SessionLocal", lambda: _Db(rows))
    monkeypatch.setattr(mr, "httpx", _Dead([]))
    monkeypatch.setattr(mr.gpu_shared_memory, "vram_snapshot", lambda: _4070TI)
    request = SimpleNamespace(
        state=SimpleNamespace(current_user="luis", api_token=False),
        app=SimpleNamespace(state=SimpleNamespace(auth_manager=None)),
    )
    data = asyncio.run(endpoint(request, refresh=True))
    assert data["models"] == {}


def test_a_lan_ollama_is_left_alone(monkeypatch):
    """Its models run on someone else's card; our nvidia-smi says nothing
    about them, so we neither size them nor judge them."""
    data, fake = _fit_payload(monkeypatch, _4070TI, base="http://192.168.1.40:11434/v1")
    assert data["models"] == {}
    assert data["endpoint_ids"] == []
    assert fake.calls == []          # not even probed


def test_the_covered_endpoints_are_named_so_the_picker_can_match_rows(monkeypatch):
    data, _ = _fit_payload(monkeypatch, _4070TI)
    assert data["endpoint_ids"] == ["local-ollama"]


# ── The picker ───────────────────────────────────────────────────────────

def _js_body(src: str, header: str) -> str:
    i = src.index(header)
    p = src.index("(", i)
    depth = 0
    for k in range(p, len(src)):
        if src[k] == "(":
            depth += 1
        elif src[k] == ")":
            depth -= 1
            if depth == 0:
                p = k
                break
    j = src.index("{", p)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


def test_every_picker_row_goes_through_the_badge():
    row = _js_body(_PICKER_JS, "function _addRow(")
    assert "_fitBadge(m)" in row
    assert "row.appendChild(fitEl)" in row


def test_the_badge_paints_nothing_without_data():
    """No entry, or an entry with no size, must produce no element at all."""
    badge = _js_body(_PICKER_JS, "function _fitBadge(")
    guard = badge.index("return null;")
    assert "if (!fit || !(fit.size_bytes > 0)) return null;" in badge
    # Nothing is created before the guard has run.
    assert "createElement" not in badge[:guard]


def test_the_badge_only_colours_a_state_it_was_given():
    badge = _js_body(_PICKER_JS, "function _fitBadge(")
    assert "const state = fit.state || '';" in badge
    assert "if (state) span.classList.add(`mp-fit-${state}`);" in badge


def test_the_badge_says_the_state_in_words_not_only_in_colour():
    badge = _js_body(_PICKER_JS, "function _fitBadge(")
    assert "_FIT_WORD[state]" in badge
    words = _PICKER_JS[_PICKER_JS.index("const _FIT_WORD ="):]
    words = words[:words.index("\n")]
    assert "tight" in words and "over VRAM" in words


def test_the_title_carries_the_backends_sentence():
    badge = _js_body(_PICKER_JS, "function _fitBadge(")
    assert "span.title = fit.note" in badge
    # And the fallback, used when there is a size but no card, still says the
    # number is only the file on disk.
    assert "Approximate" in badge


def test_rows_are_only_annotated_for_endpoints_the_backend_measured():
    lookup = _js_body(_PICKER_JS, "function _fitFor(")
    assert "ids.includes(m.endpointId)" in lookup
    assert "return null" in lookup
    # Exact tag match only: fuzzy matching is what hands one model another
    # model's size (28 GB of q8_0 shown for the q4_K_M).
    assert "table[m.mid] || table[`${m.mid}:latest`]" in lookup


def test_a_model_that_does_not_fit_is_still_selectable():
    """Advisory, never a gate: the row keeps its click handler and gains no
    disabled state."""
    row = _js_body(_PICKER_JS, "function _addRow(")
    assert "row.addEventListener('click', () => _pick(m));" in row
    fit_block = row[row.index("_fitBadge(m)"):]
    assert "disabled" not in fit_block
    assert "pointer-events" not in fit_block


def test_the_hints_are_not_refetched_on_every_keystroke():
    """`_populate` runs on every character typed into the picker search."""
    populate = _js_body(_PICKER_JS, "function _populate(")
    assert "fetch(" not in populate
    assert "_refreshFitHints" not in populate
    # The fetch lives on the open/refresh path instead — and only the explicit
    # refresh button forces a recompute; an ordinary open rides the TTL cache.
    refresh = _js_body(_PICKER_JS, "async function _refreshPickerModels(")
    assert "_refreshFitHints({ force: refreshFit })" in refresh
    assert "refreshFit: true" in _PICKER_JS[_PICKER_JS.index("refreshBtn.addEventListener("):]


def test_the_hints_are_cached_client_side_too():
    fn = _js_body(_PICKER_JS, "async function _refreshFitHints(")
    assert "_FIT_TTL_MS" in fn
    assert "if (!force && now - _fitFetchedAt < _FIT_TTL_MS) return;" in fn


def test_a_failed_fetch_keeps_the_previous_answer_rather_than_lying():
    fn = _js_body(_PICKER_JS, "async function _refreshFitHints(")
    assert "if (!r.ok) return;" in fn
    assert "catch" in fn


def test_the_two_halves_agree_on_the_payload_keys():
    """A perfect endpoint the picker misreads ships nothing. Pin the contract
    from both sides rather than trusting two files to stay in step."""
    for key in ("endpoint_ids", "size_bytes", "state", "note"):
        assert f'"{key}"' in _ROUTES, key
    assert "data.endpoint_ids" in _PICKER_JS
    assert "data.models" in _PICKER_JS
    assert "fit.size_bytes" in _PICKER_JS
    assert "fit.state" in _PICKER_JS
    assert "fit.note" in _PICKER_JS
    # And the picker asks the route that actually exists.
    assert "/api/models/fit" in _PICKER_JS


def test_the_badge_is_styled_for_both_themes():
    """theme.js writes --bg/--fg onto :root for every palette, light or dark,
    so --warn/--red have to be blended toward --fg to stay legible in both."""
    block = _CSS[_CSS.index(".model-picker-list .model-switch-item .mp-fit {"):]
    block = block[:block.index("/* Keyboard navigation highlight")]
    assert "color-mix(in srgb, var(--warn)" in block
    assert "color-mix(in srgb, var(--red)" in block
    assert "var(--fg))" in block
    # Comments stripped: the rationale above the rules names the hex it is
    # arguing against, and that is not a colour anything paints with.
    declarations = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", declarations), declarations


def test_there_are_at_most_three_states():
    states = set(re.findall(r"mp-fit-([a-z]+)", _CSS))
    assert states == {"fits", "tight", "over"}


# ---------------------------------------------------------------------------
# The badge must not cost the model its name.
#
# Found by opening the real picker after shipping the badge: the row is ~290px
# and the name, the endpoint and the badge all share it, so three rows came
# back as "qwen3.8:...", "qwen3.8:..." and "qwen3-c..." — you could not tell
# the q4_K_M from the q8_0, which is the only reason to open the menu at all.
# ---------------------------------------------------------------------------

def _picker_source() -> str:
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    return (root / "static" / "js" / "modelPicker.js").read_text(encoding="utf-8")


def test_the_endpoint_yields_its_place_to_the_fit_badge():
    """A fit badge only exists for a loopback Ollama, where the endpoint text is
    `127.0.0.1:11434` — the least informative thing in the row. The badge takes
    its place so the model name keeps its width."""
    src = _picker_source()
    assert "epSpan.textContent = fitEl ? '' : _epDisplay;" in src
    assert "if (!fitEl) row.appendChild(epSpan);" in src
    # ...and the old unconditional append is gone, or the row grows again.
    assert "\n      row.appendChild(epSpan);\n" not in src


def test_a_truncated_name_still_reveals_the_full_model_id_on_hover():
    src = _picker_source()
    assert "nameSpan.title = m.mid || m.display || '';" in src


def test_the_viewport_media_query_is_not_relied_on_for_this():
    """The CSS `@media (max-width: 480px)` measures the viewport, not the popup,
    so on a wide screen it never fires on a 290px menu. If someone ever deletes
    the JS rule and leans on the media query again, this fails."""
    src = _picker_source()
    assert "fitEl ? '' : _epDisplay" in src, (
        "the endpoint/badge trade-off must be decided in JS, not by a viewport "
        "media query that cannot see the menu's width"
    )
