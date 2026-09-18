"""Hints attached to degraded services (FAUSTUS).

A status word without a next step is why status panels get ignored. These lock
the mapping from probe result -> actionable hint, and the two rules that keep
the panel trustworthy: nothing is said about a healthy or disabled service, and
no hint ever interpolates something from the probe meta (URLs there can carry
credentials).
"""

from src.service_hints import attach_hints, hint_for


def svc(name, status, **meta):
    return {"name": name, "status": status, "detail": "", "meta": meta}


class TestNoise:
    def test_ok_and_disabled_get_no_hint(self):
        assert hint_for(svc("chromadb", "ok", rag=True, memory=True)) is None
        assert hint_for(svc("email", "disabled")) is None
        assert hint_for(svc("ntfy", "disabled")) is None

    def test_unknown_service_gets_no_hint(self):
        assert hint_for(svc("quantum_flux", "down")) is None

    def test_garbage_input_does_not_raise(self):
        assert hint_for(None) is None
        assert hint_for({}) is None
        assert hint_for({"name": "chromadb"}) is None


class TestChromaDB:
    def test_never_initialized_says_restart_not_reconnect(self):
        """`disabled` here means "never built" — the one disabled state that
        still needs explaining, because retrieval is keyword-only all run."""
        for status in ("disabled", "down"):
            hint = hint_for(svc("chromadb", status, rag=None, memory=None))
            assert "restart" in hint["text"].lower()
            assert "keyword-only" in hint["text"]
            assert hint["command"] == "docker start odysseus-chromadb"

    def test_one_store_down_points_at_reconnect(self):
        hint = hint_for(svc("chromadb", "degraded", rag=True, memory=False))
        assert "Reconnect" in hint["text"]
        assert hint["command"] == ""

    def test_both_down_explains_the_silent_fallback(self):
        hint = hint_for(svc("chromadb", "down", rag=False, memory=False))
        assert "keyword" in hint["text"].lower()
        assert "docker start" in hint["command"]


class TestFanoutServices:
    def test_provider_category_comes_from_the_first_failing_endpoint(self):
        hint = hint_for(svc("providers", "degraded", endpoints=[
            {"ok": True, "error": None},
            {"ok": False, "error": "no_models"},
        ]))
        assert "ollama list" == hint["command"]

    def test_refused_endpoint_suggests_starting_ollama(self):
        hint = hint_for(svc("providers", "down", endpoints=[
            {"ok": False, "error": "connection_refused"}]))
        assert hint["command"] == "ollama serve"

    def test_email_auth_failure_points_at_the_app_password(self):
        hint = hint_for(svc("email", "down", accounts=[
            {"ok": False, "error": "auth_or_protocol_error"}]))
        assert "password" in hint["text"].lower()

    def test_unmapped_category_falls_back_instead_of_raising(self):
        hint = hint_for(svc("providers", "down", endpoints=[
            {"ok": False, "error": "moon_phase_error"}]))
        assert hint["text"]
        assert hint["command"] == ""


class TestSearxng:
    def test_no_host_is_a_settings_problem_not_a_docker_one(self):
        hint = hint_for(svc("searxng", "down", error="no_host"))
        assert hint["command"] == ""
        assert "configure" in hint["text"].lower() or "Settings" in hint["text"]

    def test_unreachable_suggests_bringing_it_up(self):
        hint = hint_for(svc("searxng", "down", error="connection_refused"))
        assert "docker compose" in hint["command"]


class TestAttach:
    def test_only_failing_services_get_a_hint_key(self):
        report = {"overall": "degraded", "services": [
            svc("chromadb", "ok", rag=True, memory=True),
            svc("searxng", "down", error="timeout"),
            svc("ntfy", "disabled"),
        ]}
        attach_hints(report)
        assert "hint" not in report["services"][0]
        assert report["services"][1]["hint"]["text"]
        assert "hint" not in report["services"][2]

    def test_hints_never_echo_probe_meta(self):
        """Meta can hold URLs; hints are static text, so nothing can leak."""
        report = {"services": [svc("searxng", "down", error="timeout",
                                   instance="https://user:pw@searx.example/x")]}
        attach_hints(report)
        assert "searx.example" not in report["services"][0]["hint"]["text"]

    def test_empty_and_malformed_reports_survive(self):
        assert attach_hints({}) == {}
        assert attach_hints({"services": []}) == {"services": []}
        assert attach_hints("nope") == "nope"
