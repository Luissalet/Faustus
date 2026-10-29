"""The design check of UI edits (src/design_hook): files to HTML, exceptions, the per-edit note and the closing pass.

The design workshop is replaced by a fake client: what is under test is what Faustus sends to it, what it keeps
of the answer and when it stays silent. The live path (a real workshop, a real render) is exercised by hand
and recorded in docs.
"""
import asyncio
import threading

import pytest

from src import design_hook
from src import lifecycle_hooks as hooks
from src.design_hook import client, source

BASE = "http://workshop.test"


def finding(rule="no-px-font", tier="floor", **kw):
    return {"check_id": rule, "tier": tier, "engine": "static", "title": rule, "detail": f"{rule} detail",
            "fix": "use rem", "evidence": kw.pop("evidence", rule), **kw}


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "Card.tsx").write_text(
        "export const Card = () => <div style={{ fontSize: 11, color: '#777' }}>hola</div>;\n", encoding="utf-8")
    (tmp_path / "index.html").write_text(
        '<!doctype html><html lang="es"><head><link rel="stylesheet" href="site.css"></head>'
        "<body><p>hola</p></body></html>", encoding="utf-8")
    (tmp_path / "site.css").write_text("p { color: #888; }\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("not a UI file", encoding="utf-8")
    return tmp_path


@pytest.fixture
def workshop(monkeypatch):
    """A workshop that answers: static lint returns `state['static']`, renders return `state['critique']`."""
    state = {"static": [finding()], "critique": {"findings": [], "gate": {"state": "pass"}, "rules_run": 9},
             "alive": True, "lint_calls": [], "renders": []}
    client.reset_cache()
    monkeypatch.setattr(client, "base_if_running", lambda force=False: BASE if state["alive"] else None)

    def lint(base, html):
        state["lint_calls"].append(html)
        return list(state["static"]) if state["static"] is not None else None

    def render(base, html, title=""):
        state["renders"].append((title, html))
        return {"id": f"r{len(state['renders'])}"}

    monkeypatch.setattr(client, "lint_static", lint)
    monkeypatch.setattr(client, "render_page", render)
    monkeypatch.setattr(client, "critique_render", lambda base, rid, waivers: state["critique"])
    design_hook._SESSIONS.clear()
    return state


# ---------------------------------------------------------------------------
# Files to HTML
# ---------------------------------------------------------------------------

def test_which_files_are_ui_sources():
    for name in ("a.tsx", "b.JSX", "c.css", "d.html", "e.htm", "f.vue"):
        assert source.is_design_file(name)
    for name in ("a.py", "b.md", "c.json", "d.ts"):
        assert not source.is_design_file(name)


def test_css_and_html_pass_through_as_a_document():
    css = source.to_html("a.css", "p { margin: 0 }")
    assert "<style>" in css and "p { margin: 0 }" in css and "<body>" in css
    assert source.to_html("a.html", "<p>x</p>") == "<p>x</p>"
    assert source.to_html("a.py", "x = 1") is None
    assert source.to_html("a.css", "") is None


def test_jsx_keeps_what_is_written_and_drops_what_is_an_expression():
    html = source.to_html("Card.tsx",
                          "export const C = ({t}) => <section className=\"box\" style={{ fontSize: 11, opacity: 0.5 }}>"
                          "<img src=\"a.png\" /><h1>{t}</h1><p>Hola</p></section>;")
    assert "font-size" in html and "11px" in html
    assert "opacity" in html and "0.5" in html  # unitless stays unitless
    assert "Hola" in html and "box" in html
    assert "{t}" not in html  # an expression is not a value


def test_vue_takes_the_template_and_the_styles_and_drops_bound_attributes():
    html = source.to_html("A.vue", "<template><div :style=\"s\" class=\"a\"><p>Hola</p></div></template>"
                                   "<script>export default {}</script><style>.a{color:red}</style>")
    assert "Hola" in html and ".a{color:red}" in html
    assert ":style" not in html and "export default" not in html


def test_a_file_over_the_limit_is_not_read():
    assert source.to_html("a.css", "a{}" * source.MAX_BYTES) is None


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

def test_waivers_need_a_real_reason_and_each_its_own():
    ok = design_hook.sanitize_waivers([{"rule": "r1", "reason": "  the brand asks for this exact grey on dark  "}])
    assert ok == [{"rule": "r1", "reason": "the brand asks for this exact grey on dark"}]
    assert design_hook.sanitize_waivers(None) == [] and design_hook.sanitize_waivers([]) == []
    with pytest.raises(ValueError, match="at least"):
        design_hook.sanitize_waivers([{"rule": "r1", "reason": "too short"}])
    same = "the same long reason for two different rules"
    with pytest.raises(ValueError, match="own reason"):
        design_hook.sanitize_waivers([{"rule": "r1", "reason": same}, {"rule": "r2", "reason": same.upper()}])
    with pytest.raises(ValueError, match="twice"):
        design_hook.sanitize_waivers([{"rule": "r1", "reason": "first reason that is long enough"},
                                      {"rule": "r1", "reason": "second reason that is long enough"}])
    with pytest.raises(ValueError):
        design_hook.sanitize_waivers("nope")
    with pytest.raises(ValueError):
        design_hook.sanitize_waivers([{"reason": "a reason that is long enough here"}])


def test_options_come_from_the_given_options_without_touching_the_store():
    got = design_hook.options_for(options={"design_hook": True, "design_waivers": [{"rule": "x", "reason": "y" * 30}],
                                           "project_id": "p1"})
    assert got == {"enabled": True, "waivers": [{"rule": "x", "reason": "y" * 30}], "project_id": "p1"}
    assert design_hook.options_for(options={})["enabled"] is False


# ---------------------------------------------------------------------------
# check()
# ---------------------------------------------------------------------------

def test_stopped_workshop_answers_so_and_asks_nothing(workspace, workshop):
    workshop["alive"] = False
    out = design_hook.check(str(workspace), ["src/Card.tsx"])
    assert out["ok"] is False and out["running"] is False and out["reason"] == "workshop_stopped"
    assert workshop["lint_calls"] == []


def test_static_level_keeps_only_floor_findings_and_never_renders(workspace, workshop):
    workshop["static"] = [finding("a", "floor"), finding("b", "reflex"), finding("c", "advisory")]
    out = design_hook.check(str(workspace), ["src/Card.tsx", "notes.md"], level="static")
    assert out["ok"] and [f["rule"] for f in out["findings"]] == ["a"]
    assert out["gate"] == "fix" and out["counts"] == {"floor": 1, "reflex": 0}
    assert out["files"] == [{"path": "src/Card.tsx", "checked": True, "rules": 3}]  # notes.md is not a UI file
    assert workshop["renders"] == []


def test_full_level_adds_open_reflex_findings_but_not_the_waived_ones(workspace, workshop):
    workshop["static"] = [finding("a", "floor"), finding("b", "reflex"), finding("c", "reflex")]
    waivers = [{"rule": "c", "reason": "a deliberate choice written for the test"}]
    out = design_hook.check(str(workspace), ["src/Card.tsx"], level="full", waivers=waivers)
    assert sorted(f["rule"] for f in out["findings"]) == ["a", "b"]
    assert out["counts"] == {"floor": 1, "reflex": 1}


def test_rules_about_a_whole_page_are_not_applied_to_a_fragment_of_it(workspace, workshop):
    workshop["static"] = [finding("a11y.no-lang", "reflex"), finding("layout.no-max-width", "reflex"),
                          finding("perf.images-unsized", "reflex")]
    out = design_hook.check(str(workspace), ["src/Card.tsx"], level="full")
    assert [f["rule"] for f in out["findings"]] == ["perf.images-unsized"]


def test_full_level_renders_the_page_a_touched_stylesheet_belongs_to(workspace, workshop):
    out = design_hook.check(str(workspace), ["site.css"], level="full")
    assert out["ok"] and [p["path"] for p in out["pages"]] == ["index.html"]
    assert out["pages"][0]["rendered"] is True
    (title, html), = workshop["renders"]
    assert title == "index.html" and "p { color: #888; }" in html and 'href="site.css"' not in html  # inlined


def test_a_render_that_fails_is_reported_not_raised(workspace, workshop, monkeypatch):
    monkeypatch.setattr(client, "render_page", lambda *a, **k: None)
    out = design_hook.check(str(workspace), ["index.html"], level="full")
    assert out["ok"] and out["pages"][0]["rendered"] is False and out["findings"] == [] or out["gate"] in ("pass", "fix")


def test_critique_findings_are_deduplicated_with_the_static_ones(workspace, workshop):
    dup = finding("a", "floor")
    workshop["static"] = [dup]
    workshop["critique"] = {"findings": [dup, finding("z", "reflex")], "gate": {"state": "fix"}, "rules_run": 12}
    out = design_hook.check(str(workspace), ["index.html"], level="full")
    assert sorted(f["rule"] for f in out["findings"]) == ["a", "z"]


def test_paths_outside_the_workspace_are_ignored(workspace, workshop, tmp_path_factory):
    other = tmp_path_factory.mktemp("elsewhere") / "x.css"
    other.write_text("a{}", encoding="utf-8")
    out = design_hook.check(str(workspace), [str(other), "../x.css"])
    assert out["ok"] is False and out["reason"] == "no_design_files"


# ---------------------------------------------------------------------------
# Closing pass
# ---------------------------------------------------------------------------

def test_closing_pass_runs_once_per_contents_and_hands_back_a_message(workspace, workshop):
    first = design_hook.closing_pass("s1", str(workspace), ["src/Card.tsx"])
    assert first["ran"] and first["gate"] == "fix" and "[Design check" in first["message"]
    assert "not from the user" in first["message"]
    again = design_hook.closing_pass("s1", str(workspace), ["src/Card.tsx"])
    assert again == {"ran": False, "reason": "unchanged"}
    (workspace / "src" / "Card.tsx").write_text("export const Card = () => <p>otro</p>;\n", encoding="utf-8")
    assert design_hook.closing_pass("s1", str(workspace), ["src/Card.tsx"])["ran"] is True
    assert design_hook.closing_pass("other-chat", str(workspace), ["src/Card.tsx"])["ran"] is True


def test_a_change_of_exceptions_runs_the_pass_again(workspace, workshop):
    design_hook.closing_pass("s1", str(workspace), ["src/Card.tsx"])
    waivers = [{"rule": "no-px-font", "reason": "the exception was added by the project owner"}]
    assert design_hook.closing_pass("s1", str(workspace), ["src/Card.tsx"], waivers=waivers)["ran"] is True


def test_clean_pass_has_no_message_and_stopped_workshop_stays_silent(workspace, workshop):
    workshop["static"] = []
    clean = design_hook.closing_pass("s1", str(workspace), ["src/Card.tsx"])
    assert clean["ran"] and clean["gate"] == "pass" and "message" not in clean
    workshop["alive"] = False
    (workspace / "src" / "Card.tsx").write_text("export const Card = () => <p>x</p>;\n", encoding="utf-8")
    assert design_hook.closing_pass("s1", str(workspace), ["src/Card.tsx"]) == {"ran": False, "reason": "workshop_stopped"}


def test_fix_rounds_count_per_chat_and_reset_on_a_new_turn():
    design_hook._SESSIONS.clear()
    assert design_hook.fix_rounds("s1") == 0
    assert design_hook.note_fix_round("s1") == 1 and design_hook.fix_rounds("s1") == 1
    assert design_hook.fix_rounds("s2") == 0
    design_hook.on_turn_start({"session_id": "s1"})
    assert design_hook.fix_rounds("s1") == 0


# ---------------------------------------------------------------------------
# Note after an edit
# ---------------------------------------------------------------------------

def ctx(workspace, tool="write_file", paths=("src/Card.tsx",), **extra):
    base = {"tool": tool, "paths": list(paths), "workspace": str(workspace), "session_id": "s1", "result": {"ok": True},
            "owner": None, "project_id": "p1"}
    base.update(extra)
    return base


@pytest.fixture
def switched_on(monkeypatch):
    monkeypatch.setattr(design_hook, "options_for", lambda *a, **k: {"enabled": True, "waivers": [], "project_id": "p1"})


def test_edit_note_names_the_file_and_is_not_repeated_for_the_same_rules(workspace, workshop, switched_on):
    note = design_hook.on_post_tool(ctx(workspace))
    assert note and "src" in note and "no-px-font" in note and "Fix:" in note
    assert design_hook.on_post_tool(ctx(workspace)) is None  # same rules open: silence
    workshop["static"] = [finding("no-px-font"), finding("other-rule")]
    assert "other-rule" in design_hook.on_post_tool(ctx(workspace))  # the set changed


def test_edit_note_is_silent_when_off_stopped_or_not_an_edit(workspace, workshop, monkeypatch):
    monkeypatch.setattr(design_hook, "options_for", lambda *a, **k: {"enabled": False, "waivers": [], "project_id": ""})
    assert design_hook.on_post_tool(ctx(workspace)) is None
    assert workshop["lint_calls"] == []
    monkeypatch.setattr(design_hook, "options_for", lambda *a, **k: {"enabled": True, "waivers": [], "project_id": "p"})
    assert design_hook.on_post_tool(ctx(workspace, tool="read_file")) is None
    assert design_hook.on_post_tool(ctx(workspace, paths=("notes.md",))) is None
    assert design_hook.on_post_tool(ctx(workspace, result={"error": "boom"})) is None
    workshop["alive"] = False
    assert design_hook.on_post_tool(ctx(workspace)) is None
    assert workshop["lint_calls"] == []


def test_edit_note_never_raises(workspace, workshop, switched_on, monkeypatch):
    monkeypatch.setattr(design_hook, "check", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert design_hook.on_post_tool(ctx(workspace)) is None


# ---------------------------------------------------------------------------
# Lifecycle wiring
# ---------------------------------------------------------------------------

def test_the_post_tool_listener_runs_off_the_event_loop_and_its_note_reaches_the_agent(workspace, workshop, switched_on):
    seen = {}
    real = design_hook.check

    def spy(*a, **k):
        seen["thread"] = threading.current_thread()
        return real(*a, **k)

    design_hook.check = spy
    design_hook.uninstall()
    design_hook.install()
    try:
        results = asyncio.run(hooks.run_async("post_tool", ctx(workspace)))
    finally:
        design_hook.check = real
        design_hook.uninstall()
    assert seen["thread"] is not threading.main_thread()
    injected = [r for r in results if r.action == "inject" and r.hook_id == design_hook.LISTENER_ID]
    assert injected and "[Design check]" in injected[0].output


def test_install_is_idempotent():
    design_hook.uninstall()
    design_hook.install()
    design_hook.install()
    try:
        entries = [e for e in hooks._LISTENERS.get("post_tool", ()) if e.get("id") == design_hook.LISTENER_ID]
        assert len(entries) == 1 and entries[0]["threaded"] is True
    finally:
        design_hook.uninstall()


def test_status_reports_the_workshop_and_the_last_pass(workspace, workshop, monkeypatch):
    monkeypatch.setattr(client, "info", lambda force=False: {"running": True, "layout": "host", "design": "on", "base": BASE})
    design_hook.closing_pass("s9", str(workspace), ["src/Card.tsx"])
    out = design_hook.status("s9")
    assert out["running"] is True and out["workshop_running"] is True and out["design_module"] == "on"
    assert out["workshop_url"] and ".tsx" in out["extensions"]
    assert out["last_pass"]["gate"] == "fix"
    assert "last_pass" not in design_hook.status()


def test_status_tells_a_module_that_is_off_from_a_workshop_that_is_stopped(monkeypatch):
    monkeypatch.setattr(client, "info", lambda force=False: {"running": True, "layout": "host", "design": "off", "base": None})
    off = design_hook.status()
    assert off["running"] is False and off["workshop_running"] is True and off["design_module"] == "off"
    monkeypatch.setattr(client, "info", lambda force=False: {"running": False, "layout": None, "design": None, "base": None})
    down = design_hook.status()
    assert down["running"] is False and down["workshop_running"] is False and down["design_module"] is None


# ---------------------------------------------------------------------------
# Finding the design rules: the cold host and its design module
# ---------------------------------------------------------------------------

class _Reply:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        return self._data


def health(modules=None, service="vitruvius-hoard"):
    body = {"ok": True, "service": service}
    if modules is not None:
        body["modules"] = [{"module": name, "state": state} for name, state in modules.items()]
    return _Reply(body)


def test_the_host_is_usable_only_with_its_design_module_on(monkeypatch):
    monkeypatch.setattr(client.httpx, "get", lambda url, timeout=None: health({"design": "on", "babel": "off"}))
    assert client._probe(BASE) == {"running": True, "layout": "host", "design": "on", "base": BASE + "/design"}
    monkeypatch.setattr(client.httpx, "get", lambda url, timeout=None: health({"design": "off"}))
    assert client._probe(BASE) == {"running": True, "layout": "host", "design": "off", "base": None}
    monkeypatch.setattr(client.httpx, "get", lambda url, timeout=None: health({"babel": "on"}))
    assert client._probe(BASE)["design"] == "off"  # no design row: nothing to ask


def test_a_workshop_that_serves_the_rules_itself_is_usable_as_it_is(monkeypatch):
    monkeypatch.setattr(client.httpx, "get", lambda url, timeout=None: health())
    assert client._probe(BASE) == {"running": True, "layout": "app", "design": "on", "base": BASE}


def test_something_else_on_the_port_or_nothing_is_not_the_workshop(monkeypatch):
    monkeypatch.setattr(client.httpx, "get", lambda url, timeout=None: health(service="other-app"))
    assert client._probe(BASE)["running"] is False
    monkeypatch.setattr(client.httpx, "get", lambda url, timeout=None: _Reply({}, 503))
    assert client._probe(BASE)["running"] is False

    def boom(url, timeout=None):
        raise OSError("refused")
    monkeypatch.setattr(client.httpx, "get", boom)
    assert client._probe(BASE) == {"running": False, "layout": None, "design": None, "base": None}


def test_the_probe_is_cached_for_a_burst_of_edits(monkeypatch):
    calls = []
    monkeypatch.setattr(client, "workshop_url", lambda: BASE)
    monkeypatch.setattr(client.httpx, "get", lambda url, timeout=None: calls.append(url) or health({"design": "on"}))
    client.reset_cache()
    for _ in range(5):
        assert client.base_if_running() == BASE + "/design"
    assert len(calls) == 1
    client.base_if_running(force=True)
    assert len(calls) == 2
    client.reset_cache()


def test_starting_the_module_is_an_explicit_call_that_only_turns_design_on(monkeypatch):
    state = {"design": "off"}
    posts = []
    monkeypatch.setattr(client, "workshop_url", lambda: BASE)
    monkeypatch.setattr(client.httpx, "get", lambda url, timeout=None: health({"design": state["design"]}))

    def post(url, json=None, timeout=None):
        posts.append((url, json))
        state["design"] = "on"
        return _Reply({})
    monkeypatch.setattr(client.httpx, "post", post)
    client.reset_cache()
    out = client.start_design_module()
    assert out["started"] is True and out["base"] == BASE + "/design"
    assert posts == [(BASE + "/api/ui/call", {"name": "software_module_set",
                                              "arguments": {"module": "design", "enabled": True, "automatic": False}})]
    assert client.start_design_module()["started"] is False and len(posts) == 1  # already on: nothing asked
    client.reset_cache()


def test_starting_the_module_does_not_start_a_stopped_workshop(monkeypatch):
    monkeypatch.setattr(client, "workshop_url", lambda: BASE)
    monkeypatch.setattr(client.httpx, "get", lambda url, timeout=None: (_ for _ in ()).throw(OSError("refused")))
    monkeypatch.setattr(client.httpx, "post", lambda *a, **k: pytest.fail("nothing may be posted to a stopped workshop"))
    client.reset_cache()
    out = client.start_design_module()
    assert out["started"] is False and out["error"] == "workshop_stopped"
    client.reset_cache()


def test_closing_pass_cause_is_registered_with_the_extra_round_policy():
    from src.extra_round_policy import CAUSES
    assert CAUSES["design_fix"].family == "verification"


def test_edit_note_uses_the_project_settings_the_turn_resolved(workspace, workshop, monkeypatch):
    # No project store is consulted: the turn hands its resolved `design_hook` and exceptions in the context.
    monkeypatch.setattr("services.projects.project_for_session", lambda *a, **k: (_ for _ in ()).throw(AssertionError("looked up")))
    on = ctx(workspace)
    on.update(design_hook=True, design_waivers=[], project_id="p1")
    assert "no-px-font" in (design_hook.on_post_tool(on) or "")
    off = ctx(workspace)
    off.update(design_hook=False, design_waivers=[], project_id="p1")
    assert design_hook.on_post_tool(off) is None