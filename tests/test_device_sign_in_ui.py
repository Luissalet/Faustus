"""Signing in with a subscription, from the interface's side.

Copilot and a ChatGPT plan have no API key to paste: the server asks the
provider for a short code, the person types it into a page, and it polls
until they have. Three things went wrong in the version this replaced and
must not come back:

  - the code has to be READABLE and copyable, because it is typed somewhere
    else, often on another device;
  - the browser tab is opened by a click, never on its own — a tab that
    appears while someone is reading is a tab the browser blocks;
  - an abandoned flow is cancelled, so it does not sit in the server's
    memory until it expires.

The adapter's behaviour — the "complete" verification URL that carries the
code, the backend's own error message surviving, and pending / authorised /
expired being told apart — is exercised in
`studio/checks/device.check.mjs`. The route side is pinned in
tests/test_device_flow_routes.py.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_UI = (_REPO / "studio" / "src" / "screens" / "settings" / "DeviceSignIn.tsx").read_text(encoding="utf-8")
_ADAPTER = (_REPO / "studio" / "src" / "adapters" / "deviceAuth.ts").read_text(encoding="utf-8")
_SETTINGS = (_REPO / "studio" / "src" / "screens" / "Settings.tsx").read_text(encoding="utf-8")


def test_both_subscription_providers_are_offered():
    for provider in ("copilot", "chatgpt-subscription"):
        assert f"id: '{provider}'" in _ADAPTER, provider
    assert "'/api/copilot'" in _ADAPTER
    assert "'/api/chatgpt-subscription'" in _ADAPTER


def test_it_is_reachable_from_the_models_settings():
    assert "DeviceSignIn" in _SETTINGS
    assert "Sign in with a subscription" in _SETTINGS


def test_the_three_device_endpoints_are_all_used():
    for path in ("/device/start", "/device/poll", "/device/cancel"):
        assert path in _ADAPTER, f"{path} is never called"


def test_the_code_is_shown_and_can_be_copied():
    assert 'data-testid="device-code"' in _UI
    assert "start.userCode" in _UI
    assert "clipboard.writeText" in _UI, "the code is typed elsewhere; it must be copyable"


def test_the_tab_opens_on_a_click_not_on_its_own():
    """`window.open` must sit inside an onClick, never in an effect."""
    assert "window.open" in _UI
    opener = _UI.split("window.open", 1)[0]
    tail = opener[-400:]
    assert "onClick" in tail, "the tab must be opened by a button, not automatically"
    assert "useEffect(() => {\n      window.open" not in _UI


def test_an_abandoned_flow_is_cancelled():
    assert "cancelDeviceFlow" in _UI, "leaving must drop the pending flow"


def test_a_refusal_and_an_expiry_are_told_apart():
    assert "access_denied" in _UI and "expired_token" in _UI, (
        "'refused' and 'expired' need different words: one is a decision, the "
        "other is just time"
    )


_CHECK = _REPO / "studio" / "checks" / "device.check.mjs"
_HAS_NODE = shutil.which("node") is not None
_HAS_ESBUILD = (_REPO / "node_modules" / "esbuild" / "lib" / "main.js").exists()


@pytest.mark.skipif(not (_HAS_NODE and _HAS_ESBUILD), reason="node + node_modules/esbuild needed")
def test_the_adapter_behaves():
    proc = subprocess.run(
        ["node", str(_CHECK)], capture_output=True, text=True,
        encoding="utf-8", cwd=str(_REPO), timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "ALL OK" in proc.stdout
    assert "FAIL" not in proc.stdout
