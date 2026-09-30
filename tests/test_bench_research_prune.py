"""`scripts/bench_research_prune.py` replays page extraction with and without
pruning on the saved fixture pages, offline, with a stand-in model."""
import importlib.util
import json
import os

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
SCRIPT = os.path.join(ROOT, "scripts", "bench_research_prune.py")


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("bench_research_prune", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def result(bench):
    import argparse
    args = argparse.Namespace(endpoint="", model_name="", timeout=30, sim_rate=150.0,
                              max_content_chars=15000, prune_max_chars=6000)
    return bench.run_benchmark(bench.DEFAULT_PAGES_DIR, args)


def test_pruned_mode_reads_fewer_characters_and_loses_nothing(result):
    full, pruned = result["full"], result["pruned"]
    assert pruned["chars_read"] < full["chars_read"]
    assert result["chars_saved_pct"] > 0
    assert pruned["sources"] == full["sources"] == 4
    assert pruned["gold_recall_pct"] >= full["gold_recall_pct"] > 0
    assert full["cited_pct"] == pruned["cited_pct"] == 100.0       # nothing the stand-in model says is invented
    assert pruned["seconds_per_page"] < full["seconds_per_page"]
    assert full["simulated_time"] and pruned["simulated_time"]


def test_long_page_is_capped_and_traced(result):
    row = next(r for r in result["pruned"]["pages"] if r["page"] == "caching_guide_en.html")
    full_row = next(r for r in result["full"]["pages"] if r["page"] == "caching_guide_en.html")
    assert row["chars_read"] < full_row["chars_read"]
    trace = {t["url"].rsplit("/", 1)[-1]: t for t in result["pruned"]["trace"]}["caching_guide_en.html"]
    assert trace["pruned_chars"] <= 6000 + 12 and trace["original_chars"] > trace["pruned_chars"]
    assert result["full"]["trace"] == []               # the original path records no pruning


def test_cli_prints_a_table_and_json(bench, capsys):
    assert bench.main([]) == 0
    table = capsys.readouterr().out
    assert "chars read" in table and "gold recall" in table and "pruned" in table
    assert bench.main(["--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert set(data) == {"full", "pruned", "chars_saved_pct"}


def test_empty_pages_dir_is_an_error(bench, tmp_path):
    with pytest.raises(SystemExit):
        bench.main(["--pages-dir", str(tmp_path)])
