"""The checkout's own .env never configures the test process (tests/conftest.py).

app.py calls load_dotenv() at import. On a machine whose .env said
AUTH_ENABLED=false, every test that ran after an `import app` on the same
xdist worker saw auth turned off, so owner-scope and 401/403 tests failed only
under xdist. These pin the guard so the leak cannot come back.
"""
import os

import dotenv


def test_discovery_load_is_a_no_op(monkeypatch, tmp_path):
    (tmp_path / ".env").write_text("FAUSTUS_HERMETIC_PROBE=leaked\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAUSTUS_HERMETIC_PROBE", raising=False)
    assert dotenv.load_dotenv() is False
    assert dotenv.load_dotenv(encoding="utf-8-sig") is False
    assert "FAUSTUS_HERMETIC_PROBE" not in os.environ


def test_explicit_path_still_loads(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("FAUSTUS_HERMETIC_PROBE=explicit\n", encoding="utf-8")
    monkeypatch.delenv("FAUSTUS_HERMETIC_PROBE", raising=False)
    try:
        assert dotenv.load_dotenv(str(env_file)) is True
        assert os.environ["FAUSTUS_HERMETIC_PROBE"] == "explicit"
    finally:
        os.environ.pop("FAUSTUS_HERMETIC_PROBE", None)


def test_importing_app_does_not_turn_auth_off(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    import app  # noqa: F401  (its import-time load_dotenv is the old leak)
    from src.owner_identity import auth_disabled
    assert os.environ.get("AUTH_ENABLED") is None
    assert auth_disabled() is False
