"""What the NVIDIA driver actually exposes about the sysmem fallback policy.

Probed on the reference box (RTX 4070 Ti, driver 560.94): NVAPI answers, the
driver settings store opens without elevation, and its list of 102 settings
does **not** include the CUDA sysmem fallback policy — the Control Panel writes
that one privately. `DRS_SaveSettings` also returns NVAPI_ACCESS_DENIED
unelevated. So the module must report the truth rather than pretend it can flip
the switch; these tests pin that contract on any machine.
"""
from src import nvidia_drs


def test_status_shape_and_never_raises():
    s = nvidia_drs.status()
    for key in ("available", "exposed", "value", "label", "manual_only", "setting_id",
                "steps", "reason"):
        assert key in s
    assert isinstance(s["available"], bool)
    assert isinstance(s["exposed"], bool)
    assert s["setting_id"] == "0x10ececc9"


def test_steps_are_actionable_when_it_cannot_be_automated():
    s = nvidia_drs.status()
    assert s["steps"] and all(isinstance(x, str) and x for x in s["steps"])
    joined = " ".join(s["steps"]).lower()
    assert "control panel" in joined
    assert "sysmem fallback" in joined
    if not s["exposed"]:
        # The honest state: we know the value cannot be read or written here.
        assert s["manual_only"] is True
        assert s["reason"]


def test_values_map():
    assert nvidia_drs.VALUES[nvidia_drs.PREFER_NO_FALLBACK] == "Prefer No Sysmem Fallback"
    assert set(nvidia_drs.VALUES) == {0, 1, 2}


def test_open_control_panel_returns_steps_even_on_failure():
    out = nvidia_drs.open_control_panel()
    assert "ok" in out and out["steps"]
