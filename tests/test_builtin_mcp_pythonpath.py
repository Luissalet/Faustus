import os

from src.builtin_mcp import builtin_python_env


def test_builtin_python_env_preserves_existing_pythonpath(monkeypatch):
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(["/app/venv/lib/python3.13/site-packages", "/app", "/extra"]),
    )

    env = builtin_python_env("/app")

    assert env["PYTHONPATH"] == os.pathsep.join(["/app", "/app/venv/lib/python3.13/site-packages", "/extra"])


def test_builtin_python_env_uses_app_root_without_existing_pythonpath(monkeypatch):
    monkeypatch.delenv("PYTHONPATH", raising=False)

    assert builtin_python_env("/srv/odysseus")["PYTHONPATH"] == "/srv/odysseus"


def test_builtin_channel_env_targets_parent_api_and_internal_identity(monkeypatch):
    from core.middleware import INTERNAL_TOOL_TOKEN
    monkeypatch.setenv("APP_PORT", "63364")
    env = builtin_python_env("/srv/odysseus")
    assert env["FAUSTUS_CHANNELS_API_URL"] == "http://127.0.0.1:63364"
    assert env["FAUSTUS_CHANNELS_INTERNAL_TOKEN"] == INTERNAL_TOOL_TOKEN


def test_builtin_groups_use_parent_api_and_internal_identity(monkeypatch):
    from core.middleware import INTERNAL_TOOL_TOKEN
    from src.builtin_mcp import _BUILTIN_SERVERS
    monkeypatch.setenv('APP_PORT','63401')
    env=builtin_python_env('/srv/faustus')
    assert env['FAUSTUS_INTERNAL_API_URL']=='http://127.0.0.1:63401'
    assert env['FAUSTUS_INTERNAL_TOOL_TOKEN']==INTERNAL_TOOL_TOKEN
    assert 'groups' in _BUILTIN_SERVERS
