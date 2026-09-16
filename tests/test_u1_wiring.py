"""U1 wiring gap (see .../scratchpad/paridad_wave2/U1_wiring.md).

`sandbox_missing_policy` (and `agent_sandbox_persistent_session`) work at
runtime via `get_setting()`'s default fallback with no change to
`src/settings.py` — neither file is owned by this lot (CONTRATO.md's
ownership table). What is NOT wired without touching that file is
*persisting* an explicit value: `src.settings`'s save path rejects any key
absent from `DEFAULT_SETTINGS`. This pins that gap so it turns into a
regular green check the moment the diff in `U1_wiring.md` lands.
"""
from __future__ import annotations

import pytest

import src.settings as settings_mod


@pytest.mark.xfail(strict=True, reason=(
    "sandbox_missing_policy is not yet registered in "
    "src.settings.DEFAULT_SETTINGS (src/settings.py is not an owned file for "
    "this lot) — see scratchpad/paridad_wave2/U1_wiring.md for the exact diff. "
    "Runtime reads already default safely via get_setting(); only saving an "
    "explicit value through the settings API is blocked until this lands."))
def test_sandbox_missing_policy_is_registered_in_the_settings_schema():
    assert "sandbox_missing_policy" in settings_mod.DEFAULT_SETTINGS
