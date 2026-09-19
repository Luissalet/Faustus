"""The chart-fence validator (studio/src/lib/chartSpec.ts).

Pure: JSON text in, `{ok:true,spec}|{ok:false,reason}` out — never a throw.
`studio/checks/chartSpec.check.mjs` drives it (valid bar/line/pie/area, every
declared cap, NaN/string/negative handling, malformed JSON) and prints
ok/FAIL lines; this test runs it. Needs node and the repo's node_modules
(esbuild bundles the TS), same pattern as test_studio_markdown_js.py.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_CHECK = _REPO / "studio" / "checks" / "chartSpec.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()

pytestmark = pytest.mark.skipif(
    not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed"
)


def test_chart_spec_checks_pass():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout


def test_the_files_this_feature_owns_exist():
    for rel in (
        "studio/src/lib/chartSpec.ts",
        "studio/src/components/ChartBlock.tsx",
        "docs/ui/charts.md",
    ):
        assert (_REPO / rel).exists(), f"missing {rel}"


def test_no_new_charting_dependency():
    import json

    pkg = json.loads((_REPO / "package.json").read_text(encoding="utf-8"))
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    for banned in ("chart.js", "recharts", "d3", "victory", "nivo", "apexcharts", "echarts"):
        assert banned not in deps, f"no charting dependency was to be added, found {banned}"


def test_rich_tsx_hooks_the_chart_fence():
    rich = (_REPO / "studio" / "src" / "screens" / "rich.tsx").read_text(encoding="utf-8")
    assert "CHART_FENCE_LANGS" in rich
    assert "ChartBlock" in rich
    assert "parseChartSpec" in rich


def test_agent_prompt_mentions_the_chart_fence():
    """A pytest guard (per this feature's own instructions) that the model is
    actually taught the fence exists, not just that Studio can render it."""
    agent_loop = (_REPO / "src" / "agent_loop.py").read_text(encoding="utf-8")
    assert "```chart" in agent_loop
    assert "charts.md" in agent_loop
