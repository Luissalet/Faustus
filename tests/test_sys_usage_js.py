"""The live usage widget (static/js/sysUsage.js) with more than one GPU.

/api/system/usage now carries a `gpu_pool` block and per-card `models`, and
every loaded model says where it sits (`placement`, `gpus`, `per_gpu`). The
widget gets a combined / separate view of the cards, a placement line per
model and a pill that still fits the top bar. Node runs the pure helpers
(pillText, gpuSectionsHtml, placementText, poolOf) against a two-card
fixture and a one-card fixture; the one-card output is pinned to today's
text byte for byte. Source contracts pin the wiring (localStorage key, the
`gpu_pool.count > 1` guard, the delegated click, window.sysUsage).
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
MODULE_PATH = _REPO / "static/js/sysUsage.js"
MODULE_JS = MODULE_PATH.read_text(encoding="utf-8")
CSS = (_REPO / "static/style.css").read_text(encoding="utf-8")
_HAS_NODE = shutil.which("node") is not None

GIB = 1024 ** 3

# A two-card box: RTX 4070 Ti (12 GB) + RTX 5060 Ti (16 GB). qwen3.8:27b is
# split 8.5 + 10.2 GB across both, qwen3.5:9b sits on GPU 1 alone (5.2 GB).
# MiB for the nvidia-smi fields (as the route returns them), bytes for the
# Ollama / per-card model sizes.
_TWO = {
    "ts": 1,
    "ram": {"total": 64 * GIB, "used": 20 * GIB, "percent": 31},
    "cpu": {"percent": 12, "count": 32},
    "gpu": [
        {"index": 0, "name": "NVIDIA GeForce RTX 4070 Ti", "util": 22, "mem_used": 9523, "mem_total": 12282,
         "mem_free": 2759, "temp": 39, "power": 16.5, "power_limit": 285,
         "uuid": "GPU-5ab72dd9-1a45-c3af-5e12-ac7796b1def7", "bus_id": "00000000:01:00.0",
         "models": [{"name": "qwen3.8:27b-q4_K_M", "bytes": int(8.5 * GIB)}], "runner_pids": [15948]},
        {"index": 1, "name": "NVIDIA GeForce RTX 5060 Ti", "util": 0, "mem_used": 16179, "mem_total": 16311,
         "mem_free": 132, "temp": 43, "power": 7.2, "power_limit": 180,
         "uuid": "GPU-15d17fee-8c0c-4be3-be46-35fb3e32f2aa", "bus_id": "00000000:10:00.0",
         "models": [{"name": "qwen3.8:27b-q4_K_M", "bytes": int(10.2 * GIB)}, {"name": "qwen3.5:9b", "bytes": int(5.2 * GIB)}],
         "runner_pids": [15948, 16001]},
    ],
    "gpu_pool": {"count": 2, "mem_used": 25702, "mem_total": 28593, "mem_free": 2891, "util": 22, "util_avg": 11,
                 "power": 23.7, "power_limit": 465, "temp": 43,
                 "names": ["NVIDIA GeForce RTX 4070 Ti", "NVIDIA GeForce RTX 5060 Ti"]},
    "ollama": {
        "reachable": True, "base": "http://127.0.0.1:11434",
        "models": [
            {"name": "qwen3.8:27b-q4_K_M", "size": int(18.7 * GIB), "size_vram": int(18.7 * GIB), "gpu_pct": 100, "cpu_pct": 0,
             "context_length": 16384, "expires_at": "2099-01-01T00:00:00Z", "parameter_size": "27B", "quantization": "Q4_K_M",
             "gpus": [0, 1], "placement": "split", "per_gpu": [{"index": 0, "bytes": int(8.5 * GIB)}, {"index": 1, "bytes": int(10.2 * GIB)}]},
            {"name": "qwen3.5:9b", "size": int(5.2 * GIB), "size_vram": int(5.2 * GIB), "gpu_pct": 100, "cpu_pct": 0,
             "context_length": 32768, "parameter_size": "9B", "quantization": "Q4_K_M",
             "gpus": [1], "placement": "single", "per_gpu": [{"index": 1, "bytes": int(5.2 * GIB)}]},
        ],
    },
    "gpu_mem": {"supported": True, "total_shared": 0, "ollama": {"shared": 0, "dedicated": 0, "spilling": False, "shared_fraction": 0}},
}

# Today's one-card box: the same route shape as before, plus the count-1 pool.
_ONE = {
    "ts": 1,
    "ram": {"total": 64 * GIB, "used": 20 * GIB, "percent": 31},
    "gpu": [
        {"index": 0, "name": "NVIDIA GeForce RTX 4070 Ti", "util": 22, "mem_used": 9523, "mem_total": 12282,
         "temp": 39, "power": 16.5, "power_limit": 285,
         "models": [{"name": "qwen3.5:9b", "bytes": int(7.5 * GIB)}], "runner_pids": [15948]},
    ],
    "gpu_pool": {"count": 1, "mem_used": 9523, "mem_total": 12282, "mem_free": 2759, "util": 22, "util_avg": 22,
                 "power": 16.5, "power_limit": 285, "temp": 39, "names": ["NVIDIA GeForce RTX 4070 Ti"]},
    "ollama": {"reachable": True, "base": "http://127.0.0.1:11434",
               "models": [{"name": "qwen3.5:9b", "size": int(7.5 * GIB), "size_vram": int(7.5 * GIB), "gpu_pct": 100, "cpu_pct": 0,
                           "context_length": 32768, "gpus": [0], "placement": "single", "per_gpu": [{"index": 0, "bytes": int(7.5 * GIB)}]}]},
}

# What the pill said for _ONE before multi-GPU existed (byte for byte).
_ONE_PILL_TODAY = "GPU 22% · 9.3/12.0G · 39° · qwen3.5 100%↑GPU · RAM 31%"

_PRELUDE = f"const m = await import({json.dumps(MODULE_PATH.as_posix())});\n" \
           f"const TWO = {json.dumps(_TWO)};\nconst ONE = {json.dumps(_ONE)};\n"


def _run(script: str) -> dict:
    proc = subprocess.run(["node", "--input-type=module"], input=_PRELUDE + script, capture_output=True,
                          text=True, encoding="utf-8", timeout=60, cwd=str(_REPO))
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ── source contracts ────────────────────────────────────────────────────────

def test_module_parses_and_imports_without_a_dom():
    proc = subprocess.run(["node", "--check", str(MODULE_PATH)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    # The pure helpers must import cleanly under node: no document/window at import time.
    assert "if (typeof window !== 'undefined') window.sysUsage = sysUsage;" in MODULE_JS


def test_view_mode_is_persisted_under_its_own_key_and_gated_on_the_pool_count():
    assert "const GPU_VIEW_KEY = 'odysseus-usage-gpu-view';" in MODULE_JS
    assert "localStorage.getItem(GPU_VIEW_KEY) === 'separate' ? 'separate' : 'combined'" in MODULE_JS
    assert "localStorage.setItem(GPU_VIEW_KEY, _gpuView)" in MODULE_JS
    # combined / separate only exist when the server says there is a pool
    assert "d.gpu_pool.count > 1" in MODULE_JS
    guard = MODULE_JS.split("function isMulti(d) {", 1)[1].split("}", 1)[0]
    assert "d.gpu_pool && d.gpu_pool.count > 1" in guard


def test_exports_and_window_object_carry_the_view_switch():
    for name in ("export function setGpuView(mode)", "export function gpuView()", "export function pillText(d, mode",
                 "export function gpuSectionsHtml(d, mode", "export function placementText(m, d)", "export function poolOf(d)",
                 "export function pillLevel(d)"):
        assert name in MODULE_JS, name
    obj = MODULE_JS.split("const sysUsage = {", 1)[1].split("};", 1)[0]
    for key in ("init", "tick", "toggle", "setVisible", "setExpanded", "setGpuView", "gpuView", "get last()"):
        assert key in obj, key


def test_segmented_control_click_is_delegated_inside_the_panel_without_a_reload():
    handler = MODULE_JS.split("panel.addEventListener('click',", 1)[1].split("});", 1)[0]
    assert "closest('[data-su-gpu-view]')" in handler
    assert "setGpuView(btn.getAttribute('data-su-gpu-view'))" in handler
    assert "e.stopPropagation();" in handler          # must not count as a click outside → close
    assert "location.reload" not in MODULE_JS
    for dialog in ("alert(", "confirm(", "prompt("):
        assert dialog not in MODULE_JS, dialog
    set_view = MODULE_JS.split("export function setGpuView(mode) {", 1)[1].split("\n}\n", 1)[0]
    assert "if (_pill) render();" in set_view


def test_busy_dot_uses_the_pool_max_util():
    render = MODULE_JS.split("function render() {", 1)[1].split("\n}\n", 1)[0]
    assert "dot.classList.toggle('busy', !!model && (gpus.length ? (poolOf(d).util || 0) > 5 : true));" in render
    assert "text.textContent = pillText(d, _gpuView);" in render
    assert "pillLevel(d)" in render


def test_css_has_the_multi_gpu_rules_next_to_the_other_su_rules():
    block = CSS.split("/* System usage pill + panel */", 1)[1].split("@media (max-width: 1100px)", 1)[0]
    for sel in (".su-h.su-h-gpu", ".su-gpu-view", ".su-gpu-view button.on", ".su-gpu-row", ".su-gpu-name",
                ".su-gpu-mini", ".su-gpu-mini.hot span", ".su-gpu-models"):
        assert sel in block, sel
    # theme-neutral like its neighbours: tokens, no hard-coded page colours
    view = block.split("\n.su-gpu-view {", 1)[1].split("\n", 1)[0]
    assert "var(--border)" in view
    on = block.split(".su-gpu-view button.on {", 1)[1].split("\n", 1)[0]
    assert "var(--color-accent" in on


# ── the helpers under node ──────────────────────────────────────────────────

@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_pill_text_combined_separate_and_one_card():
    out = _run("""
      console.log(JSON.stringify({
        combined: m.pillText(TWO, 'combined'),
        separate: m.pillText(TWO, 'separate'),
        dflt: m.pillText(TWO),
        one: m.pillText(ONE, 'combined'),
        oneSep: m.pillText(ONE, 'separate'),
        none: m.pillText(null),
        level: m.pillLevel(TWO),
        levelOne: m.pillLevel(ONE),
        spill: m.pillText({ ...TWO, gpu_mem: { supported: true, ollama: { spilling: true } } }, 'combined'),
        noGpu: m.pillText({ gpu: [], gpu_pool: {}, ollama: { reachable: true, models: [] }, ram: { total: 1, percent: 50 } }),
        offline: m.pillText({ gpu: [], ollama: { reachable: false, models: [] } }),
      }));
    """)
    assert out["combined"] == "GPU 22% · 9.3+15.8/28G · 43° · qwen3.8 100%↑GPU · RAM 31%"
    assert out["separate"] == "GPU0 22% 9.3/12G · GPU1 0% 15.8/16G · 43° · qwen3.8 100%↑GPU · RAM 31%"
    assert out["dflt"] == out["combined"]                      # default view is combined
    # one card: exactly what the pill said before, in either mode
    assert out["one"] == _ONE_PILL_TODAY and out["oneSep"] == _ONE_PILL_TODAY
    assert out["none"] == "usage: n/a"
    # the fullest card sets the colour (GPU 1 at 99 %), not the 90 % pool
    assert out["level"] == "hot" and out["levelOne"] == "warm"
    assert "⚠ PCIe spill" in out["spill"] and out["spill"].index("28G") < out["spill"].index("⚠")
    assert out["noGpu"] == "no model · RAM 50%"
    assert out["offline"] == "ollama offline"


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_gpu_sections_combined_lists_pool_bars_then_every_card_with_its_models():
    out = _run("""
      console.log(JSON.stringify({ html: m.gpuSectionsHtml(TWO, 'combined'), pool: m.poolOf(TWO) }));
    """)
    html = out["html"]
    assert html.count('<div class="su-section">') == 1
    assert '<div class="su-h su-h-gpu">GPUs (2)<span class="su-gpu-view"' in html
    assert 'data-su-gpu-view="combined" class="on"' in html and 'data-su-gpu-view="separate" title=' in html
    # pool bars: util = max (avg alongside), VRAM = sums, power = sums, temp = max
    assert '<span class="su-label">Util</span>' in html and '22% max · 11% avg' in html
    assert '25.1 / 27.9 GB' in html and 'style="width:89.9%"' in html
    assert '24 W / 465 W' in html
    assert '43 °C max' in html
    # per-card rows with a mini meter, short names and the spec'd stats line
    assert '<span class="su-gpu-name">#0 RTX 4070 Ti</span>' in html and '<span class="su-gpu-name">#1 RTX 5060 Ti</span>' in html
    assert '9.3/12 GB · 22 % · 39 ° · 17 W' in html and '15.8/16 GB · 0 % · 43 ° · 7 W' in html
    assert 'su-gpu-mini warm"><span style="width:77.5%"' in html and 'su-gpu-mini hot"><span style="width:99.2%"' in html
    assert html.index("#0 RTX 4070 Ti") < html.index("#1 RTX 5060 Ti")
    # the models resident on each card, the split one marked on both
    assert '<div>qwen3.8:27b-q4_K_M · 8.5 GB · split with #1</div>' in html
    assert '<div>qwen3.8:27b-q4_K_M · 10.2 GB · split with #0</div>' in html
    assert '<div>qwen3.5:9b · 5.2 GB</div>' in html
    assert html.count('class="su-gpu-models"') == 2
    assert out["pool"]["count"] == 2 and out["pool"]["util"] == 22 and out["pool"]["mem_total"] == 28593


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_gpu_sections_separate_is_a_section_per_card_plus_models_and_the_switch_once():
    out = _run("""
      console.log(JSON.stringify({ html: m.gpuSectionsHtml(TWO, 'separate') }));
    """)
    html = out["html"]
    assert html.count('<div class="su-section">') == 2
    # short names in the headers: the vendor prefix wrapped the header and
    # squeezed the view switch to "Separat" (seen live)
    assert '#0 RTX 4070 Ti' in html and '#1 RTX 5060 Ti' in html
    assert 'NVIDIA GeForce RTX 4070 Ti #0' not in html
    assert html.count('class="su-gpu-view"') == 1                 # on the first card only
    assert 'data-su-gpu-view="separate" class="on"' in html
    assert '9.3 / 12.0 GB' in html and '15.8 / 15.9 GB' in html   # today's per-card bars
    assert '17 W / 285 W' in html and '7 W / 180 W' in html         # + power
    assert '39 °C' in html and '43 °C' in html
    assert 'split with #1' in html and 'split with #0' in html and 'qwen3.5:9b · 5.2 GB' in html
    assert 'su-gpu-row' not in html                                  # no compact rows in this view


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_one_card_renders_exactly_like_today():
    out = _run("""
      const one = m.gpuSectionsHtml(ONE, 'combined');
      const oneSep = m.gpuSectionsHtml(ONE, 'separate');
      // the same payload without the pool block (the route before multi-GPU)
      const { gpu_pool, ...legacy } = ONE;
      console.log(JSON.stringify({ one, oneSep, legacy: m.gpuSectionsHtml(legacy, 'combined'),
                                   none: m.gpuSectionsHtml({ gpu: [], errors: ['nvidia-smi: not found'] }) }));
    """)
    one = out["one"]
    assert one == out["oneSep"] == out["legacy"]
    assert one.startswith('<div class="su-section"><div class="su-h">NVIDIA GeForce RTX 4070 Ti</div>')
    for absent in ("su-gpu-view", "su-gpu-row", "su-gpu-models", "su-h-gpu", "#0", "GPUs ("):
        assert absent not in one, absent
    assert '<span class="su-val">22%</span>' in one and '9.3 / 12.0 GB' in one and '17 W / 285 W' in one and '39 °C' in one
    assert 'nvidia-smi unavailable — nvidia-smi: not found' in out["none"]


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_placement_line_per_loaded_model():
    out = _run("""
      const [split, single] = TWO.ollama.models;
      console.log(JSON.stringify({
        split: m.placementText(split, TWO),
        single: m.placementText(single, TWO),
        cpu: m.placementText({ placement: 'cpu', gpus: [] }, TWO),
        unknown: m.placementText({ placement: 'unknown', gpus: [] }, TWO),
        absent: m.placementText({ name: 'x' }, TWO),
        singleNoName: m.placementText({ placement: 'single', gpus: [7] }, TWO),
        splitNoBytes: m.placementText({ placement: 'split', gpus: [0, 1] }, TWO),
        section: m.ollamaSectionHtml(TWO),
        legacySection: m.ollamaSectionHtml({ ollama: { reachable: true, base: 'http://x', models: [{ name: 'a:b', gpu_pct: 100, cpu_pct: 0, size: 1, size_vram: 1 }] } }),
      }));
    """)
    assert out["split"] == "split: #0 8.5 GB + #1 10.2 GB"
    assert out["single"] == "GPU 1 (RTX 5060 Ti)"
    assert out["cpu"] == "CPU" and out["unknown"] == "—" and out["absent"] == ""
    assert out["singleNoName"] == "GPU 7" and out["splitNoBytes"] == "split: #0 + #1"
    section = out["section"]
    assert '<span class="su-label">Placement</span><span class="su-val">split: #0 8.5 GB + #1 10.2 GB</span>' in section
    assert '<span class="su-label">Placement</span><span class="su-val">GPU 1 (RTX 5060 Ti)</span>' in section
    assert "Placement" not in out["legacySection"]          # a server without placement → no row


@pytest.mark.skipif(not _HAS_NODE, reason="node not installed")
def test_hostile_server_strings_are_escaped_everywhere():
    evil = "<img src=x onerror=1>"
    out = _run(f"""
      const bad = JSON.parse(JSON.stringify(TWO));
      const evil = {json.dumps(evil)};
      bad.gpu[0].models[0].name = evil; bad.gpu[1].models[0].name = evil;
      bad.gpu[1].name = evil + ' card';
      bad.ollama.models[0].name = evil;
      bad.ollama.base = 'http://' + evil;
      bad.errors = [evil];
      console.log(JSON.stringify({{
        combined: m.gpuSectionsHtml(bad, 'combined'),
        separate: m.gpuSectionsHtml(bad, 'separate'),
        ollama: m.ollamaSectionHtml(bad),
        none: m.gpuSectionsHtml({{ gpu: [], errors: [evil] }}),
        pill: m.pillText(bad, 'combined'),
      }}));
    """)
    for key in ("combined", "separate", "ollama", "none"):
        assert evil not in out[key], key
        assert "&lt;img src=x onerror=1&gt;" in out[key], key
    # the pill is set through textContent, so the raw name is fine there — but it is the model's name, nothing else
    assert out["pill"].startswith("GPU 22% · 9.3+15.8/28G · 43° · <img src=x onerror=1> 100%↑GPU")
