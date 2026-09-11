"""MOD-04 — `studio/src/screens/settings/LocalModels.tsx::ScopedOverridePanel`
used to offer only `num_ctx`/`keep_alive` even though the backend
(`src/model_load_options.py::ALLOWED_KEYS`, `PUT .../options/scoped`) always
accepted `num_gpu`/`main_gpu` too — an admin literally had no field to type
them into for a project/session override. It also saved every field the
same way, with no warning that `num_ctx`/`num_gpu`/`main_gpu` only take
effect on the model's next load while `keep_alive` does not.

This is a source-level check rather than a bundled/behavioural one:
`LocalModels.tsx` pulls in `VramAdmissionDialog` and friends, too heavy to
bundle standalone the way the smaller adapters get their own
`.check.mjs`. It still fails on the pre-change file (grep for
`num_gpu`/`main_gpu` inputs inside `ScopedOverridePanel` finds nothing) and
passes once the panel gains them and the reload-confirmation path.
"""
import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "studio" / "src" / "screens" / "settings" / "LocalModels.tsx"


def _panel_source() -> str:
    text = _SRC.read_text(encoding="utf-8")
    start = text.index("function ScopedOverridePanel")
    # Up to the doc comment introducing the next component (LocalModelsSection).
    end = text.index("\n/**\n * Local models:", start)
    return text[start:end]


def test_scoped_override_panel_offers_num_gpu_and_main_gpu():
    panel = _panel_source()
    assert 'placeholder="num_gpu"' in panel, "num_gpu has no input in the scoped override panel"
    assert 'placeholder="main_gpu"' in panel, "main_gpu has no input in the scoped override panel"
    # And they are actually sent on save, not just displayed.
    save_call = re.search(r"putScopedOptions\(.*?\);", panel, re.DOTALL)
    assert save_call, "no putScopedOptions(...) call found"
    assert "num_gpu" in save_call.group(0), "num_gpu is not part of the save payload"
    assert "main_gpu" in save_call.group(0), "main_gpu is not part of the save payload"


def test_scoped_override_panel_warns_before_a_reload_required_change():
    panel = _panel_source()
    assert "RELOAD_FIELDS" in _SRC.read_text(encoding="utf-8"), "no reload-field ledger at module scope"
    assert "num_ctx" in panel and "num_gpu" in panel and "main_gpu" in panel
    # A confirmation gate exists and is keyed off which reload fields actually changed.
    assert "changedReloadFields" in panel
    assert "window.confirm(" in panel, "no confirmation before a reload-requiring save"
    # keep_alive is explicitly NOT gated the same way (applies without a reload).
    assert "RELOAD_FIELDS = new Set(['num_ctx', 'num_gpu', 'main_gpu'])" in _SRC.read_text(encoding="utf-8")
