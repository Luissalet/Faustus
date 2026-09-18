"""OBS-04 - reproduccion y soporte local (src/support_bundle.py).

No content leaves the machine and no personal content is included by
default: effective config is masked at the source (src.effective_config),
recent events are field-ALLOWLISTED (a made-up event carrying a "text" field
must never surface it), and the log excerpt is secret-redacted. Isolated
DATA_DIR per test (tmp_path) so this never reads this repo checkout's real
data/runs.
"""
from __future__ import annotations

import json
import os
import zipfile

import pytest

from src import support_bundle as sb


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    import src.constants as consts
    monkeypatch.setattr(consts, "DATA_DIR", str(tmp_path), raising=False)
    return tmp_path


def _write_run_log(data_dir, name, lines):
    runs_dir = data_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{name}.jsonl"
    with open(path, "w", encoding="utf-8") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
    return path


# ── recent_events: allowlist, not a denylist ────────────────────────────────


def test_recent_events_keeps_structural_fields_only(isolated_data_dir):
    _write_run_log(isolated_data_dir, "s1", [
        {"event": "phase", "phase": "generating", "trace_id": "tr-1", "ts": 1.0,
         # Personal content that must NEVER survive the allowlist, even
         # though a real replay-log line carries exactly these fields:
         "text": "my social security number is 123-45-6789",
         "delta": "the secret prompt content", "tool_args": {"path": "/home/luis/secret.txt"}},
    ])
    events = sb.recent_events()
    assert len(events) == 1
    assert events[0] == {"event": "phase", "phase": "generating", "trace_id": "tr-1", "ts": 1.0}
    dumped = json.dumps(events)
    assert "social security" not in dumped
    assert "secret prompt" not in dumped
    assert "/home/luis" not in dumped


def test_recent_events_handles_missing_runs_dir(isolated_data_dir):
    assert sb.recent_events() == []


def test_recent_events_skips_unparseable_lines_without_crashing(isolated_data_dir):
    runs_dir = isolated_data_dir / "runs"
    runs_dir.mkdir()
    (runs_dir / "broken.jsonl").write_text("not json\n{\"event\": \"ok\", \"ts\": 2.0}\n[1,2,3]\n")
    events = sb.recent_events()
    assert events == [{"event": "ok", "ts": 2.0}]


def test_recent_events_respects_limit_and_orders_by_ts(isolated_data_dir):
    _write_run_log(isolated_data_dir, "s1", [{"event": "a", "ts": t} for t in range(10)])
    events = sb.recent_events(limit=3)
    assert [e["ts"] for e in events] == [7, 8, 9]


# ── sanitized_log_excerpt: redacts secret-shaped substrings ─────────────────


def test_log_excerpt_redacts_api_keys_and_bearer_tokens(isolated_data_dir, monkeypatch, tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text(
        "line one\n"
        "calling openai with key sk-abcdefghijklmnopqrstuvwxyz\n"
        "Authorization: Bearer abcdefghij1234567890.zzzz\n"
        "line four\n"
    )
    monkeypatch.setenv("ODYSSEUS_LOG_FILE", str(log_file))
    excerpt = sb.sanitized_log_excerpt()
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in excerpt
    assert "[redacted]" in excerpt
    assert "line one" in excerpt and "line four" in excerpt


def test_log_excerpt_empty_when_unconfigured(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_LOG_FILE", raising=False)
    monkeypatch.delenv("FAUSTUS_LOG_FILE", raising=False)
    assert sb.sanitized_log_excerpt() == ""


# ── build_bundle: a real zip, nothing personal by default ──────────────────


def test_build_bundle_produces_a_zip_with_the_documented_contents(isolated_data_dir):
    _write_run_log(isolated_data_dir, "s1", [{"event": "phase", "phase": "done", "ts": 1.0}])
    path = sb.build_bundle(output_dir=str(isolated_data_dir / "out"))
    assert os.path.isfile(path)
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert {"manifest.json", "version.json", "effective_config.json",
                "recent_events.json", "log_excerpt.txt"} <= names
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["personal_content_included"] is False
        events = json.loads(zf.read("recent_events.json"))
        assert events == [{"event": "phase", "phase": "done", "ts": 1.0}]


def test_build_bundle_can_skip_events_and_log(isolated_data_dir):
    path = sb.build_bundle(output_dir=str(isolated_data_dir / "out"), include_events=False, include_log=False)
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        assert "recent_events.json" not in names
        assert "log_excerpt.txt" not in names
        assert "manifest.json" in names


# ── CLI ──────────────────────────────────────────────────────────────────


def test_cli_prints_the_bundle_path(isolated_data_dir, capsys):
    rc = sb.main(["--output-dir", str(isolated_data_dir / "cli-out")])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert os.path.isfile(out)
    assert out.startswith(str(isolated_data_dir / "cli-out"))
