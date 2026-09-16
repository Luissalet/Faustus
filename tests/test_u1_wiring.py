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


def test_sandbox_missing_policy_is_registered_in_the_settings_schema():
    assert "sandbox_missing_policy" in settings_mod.DEFAULT_SETTINGS
