"""Static guards for `sdk/ts` (S2) — CONTRATO_SDK_S2.md § S2.3.

Fast, no `npm install`, no build: reads `package.json`, the committed
generated event catalogue, and greps the `.ts` sources directly — the same
no-toolchain-required posture `tests/test_studio_guards.py` already uses
for the Studio tree.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src import api_version

_REPO = Path(__file__).resolve().parents[1]
SDK = _REPO / "sdk" / "ts"
SDK_SRC = SDK / "src"

# Not the app's own product name, and not a generic word likely to appear
# by accident — distinctive third-party AI product/company names this
# package (code, docs, examples) must never mention (CONTRATO_SDK_S2.md:
# "No nombrar productos de terceros").
_BANNED_THIRD_PARTY_NAMES = (
    "openai", "chatgpt", "anthropic", "claude", "gemini", "copilot",
    "ollama", "mistral", "gpt-4", "gpt-3", "groq",
)


def _sdk_ts_sources() -> list[Path]:
    if not SDK_SRC.exists():
        return []
    return sorted(
        p for p in SDK.rglob("*.ts")
        if "node_modules" not in p.parts and "dist" not in p.parts and "dist-test" not in p.parts
    )


def _sdk_library_sources() -> list[Path]:
    """`_sdk_ts_sources()` minus `test/` — the shipped library only."""
    return [p for p in _sdk_ts_sources() if "test" not in p.relative_to(SDK).parts[:1]]


def test_sdk_tree_exists() -> None:
    """Fail loudly if these guards are silently scanning nothing."""
    assert SDK_SRC.exists(), "sdk/ts/src not found — is lot S2 checked out?"
    assert _sdk_ts_sources(), "no .ts sources found under sdk/ts — guards would pass on nothing"


def test_package_json_shape() -> None:
    pkg = json.loads((SDK / "package.json").read_text(encoding="utf-8"))
    assert pkg.get("name") == "faustus-sdk"
    assert pkg.get("private") is False
    assert pkg.get("version") == "0.1.0"
    assert pkg.get("license") == "AGPL-3.0-or-later"
    assert pkg.get("type") == "module"

    exports = pkg.get("exports", {}).get(".", {})
    assert exports.get("types") == "./dist/types/index.d.ts"
    assert exports.get("import") == "./dist/esm/index.js"
    assert exports.get("require") == "./dist/cjs/index.cjs"

    assert pkg.get("dependencies", {}) == {}, "sdk/ts must ship zero runtime dependencies"
    assert "peerDependencies" in pkg and pkg["peerDependencies"] == {}, (
        "sdk/ts must declare peerDependencies explicitly (and empty), not omit the key"
    )
    assert "node" in pkg.get("engines", {}), "package.json must pin engines.node"
    assert pkg.get("files") and "README.md" in pkg["files"] and "dist" in pkg["files"]


def test_generated_events_match_the_catalogue() -> None:
    """`src/events.generated.ts` is a build artifact that gets committed —
    this is the same drift check `npm run check` runs, reimplemented in
    Python so it costs nothing extra here (no Node needed to catch a stale
    commit)."""
    catalog = json.loads((_REPO / "docs" / "api" / "sse_events.json").read_text(encoding="utf-8"))
    core_types = [
        e["type"] for e in catalog["events"]
        if e.get("stability") == "core" and e.get("type") is not None
    ]

    generated_path = SDK_SRC / "events.generated.ts"
    assert generated_path.exists(), "sdk/ts/src/events.generated.ts is missing — run `npm run gen:events`"
    generated = generated_path.read_text(encoding="utf-8")

    match = re.search(r"CORE_EVENT_TYPES\s*=\s*\[(.*?)\]\s*as const", generated, re.S)
    assert match, "events.generated.ts has no `CORE_EVENT_TYPES` export"
    ts_types = re.findall(r"'([^']+)'", match.group(1))
    assert ts_types == core_types, (
        "sdk/ts/src/events.generated.ts is out of date with docs/api/sse_events.json — "
        "run `npm run gen:events` in sdk/ts and commit the result"
    )

    schema_match = re.search(r"SCHEMA_VERSION\s*=\s*'([^']+)'", generated)
    assert schema_match, "events.generated.ts has no `SCHEMA_VERSION` export"
    assert schema_match.group(1) == catalog["schema_version"]
    assert schema_match.group(1) == api_version.API_VERSION, (
        "sdk/ts's SCHEMA_VERSION must track src.api_version.API_VERSION"
    )


def test_no_console_in_the_shipped_library() -> None:
    """CONTRATO_SDK_S2.md: "Sin console.* en la librería" — test doubles
    (`test/`) are exempt; nothing under `src/` may log on its own."""
    offences = []
    for path in _sdk_library_sources():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bconsole\s*\.\s*(log|warn|error|info|debug|trace)\s*\(", line):
                offences.append(f"{path.relative_to(_REPO)}:{n}: {line.strip()}")
    assert not offences, "no console.* allowed in sdk/ts's shipped library:\n" + "\n".join(offences)


def test_no_third_party_product_names() -> None:
    checked = list(_sdk_ts_sources())
    readme = SDK / "README.md"
    if readme.exists():
        checked.append(readme)
    examples_dir = SDK / "examples"
    if examples_dir.exists():
        checked.extend(
            p for p in examples_dir.rglob("*")
            if p.is_file() and p.suffix in (".mjs", ".cjs", ".json", ".md") and "node_modules" not in p.parts
        )

    offences = []
    for path in checked:
        text = path.read_text(encoding="utf-8")
        lower = text.lower()
        for name in _BANNED_THIRD_PARTY_NAMES:
            if name in lower:
                offences.append(f"{path.relative_to(_REPO)}: mentions {name!r}")
    assert not offences, "sdk/ts must not name third-party products:\n" + "\n".join(offences)


def test_no_substring_guessing_about_why_a_request_failed() -> None:
    """Same idea as `tests/test_studio_guards.py`'s guard of the same name:
    the server says why it refused (`detail`/`error`/`message`, read as
    structured fields in `sdk/ts/src/http.ts::describeFailure`) — the SDK
    must not overrule that by pattern-matching the message text."""
    banned = ("includes('tool')", 'includes("tool")',
              "includes('auto')", 'includes("auto")')
    for path in _sdk_ts_sources():
        text = path.read_text(encoding="utf-8")
        for phrase in banned:
            assert phrase not in text, (
                f"{path.relative_to(_REPO)} guesses why a request failed from the "
                "error text; read the server's structured detail instead"
            )


def test_sdk_examples_exist_for_both_module_systems() -> None:
    assert (SDK / "examples" / "esm" / "index.mjs").exists()
    assert (SDK / "examples" / "esm" / "package.json").exists()
    assert (SDK / "examples" / "cjs" / "index.cjs").exists()
    assert (SDK / "examples" / "cjs" / "package.json").exists()


def test_gitignore_covers_sdk_build_output() -> None:
    gitignore = (_REPO / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("sdk/ts/dist-test/", "sdk/ts/*.tgz"):
        assert pattern in gitignore, f".gitignore is missing {pattern!r}"
