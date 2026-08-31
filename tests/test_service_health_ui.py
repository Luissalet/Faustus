"""Wiring for the service-health readout (FAUSTUS).

The bug being guarded against is not a broken function, it is a module nobody
loads: `/api/diagnostics/services` shipped with no caller for months. So these
assert the page actually loads the module, the module actually calls the
endpoints, and the CSS survives the two global rules in `style.css` that
silently break new buttons (`button { height: 32px }` and `button:hover`).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "js" / "serviceHealth.js").read_text(encoding="utf-8")
CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


class TestPageLoadsIt:
    def test_module_is_referenced_as_a_module(self):
        assert re.search(r'<script type="module" src="/static/js/serviceHealth\.js', HTML)

    def test_the_anchor_the_chip_mounts_into_exists(self):
        assert 'id="sidebar-user-bar"' in HTML
        assert 'class="user-bar-actions"' in HTML
        assert "#sidebar-user-bar .user-bar-actions" in JS

    def test_stylesheet_version_is_derived_from_the_file(self):
        """Hand-typed ?v= tokens got forgotten, so new CSS was invisible without
        a hard reload. The token is now the file's content hash — see
        src/app_helpers.substitute_asset_versions."""
        assert "style.css?v={{ASSET_V:style.css}}" in HTML


class TestModuleBehaviour:
    def test_calls_both_endpoints(self):
        assert "'/api/diagnostics/services'" in JS
        assert "'/api/diagnostics/services/reconnect'" in JS
        assert "method: 'POST'" in JS

    def test_gives_up_on_403_instead_of_hammering(self):
        assert "res.status === 401 || res.status === 403" in JS
        assert "function disable()" in JS
        assert "clearInterval" in JS

    def test_polls_and_rechecks_on_focus(self):
        assert "setInterval" in JS
        assert "visibilitychange" in JS

    def test_announces_the_transition_into_degraded(self):
        assert "showToast" in JS
        assert "_lastOverall === 'ok'" in JS

    def test_sends_credentials_like_the_rest_of_the_app(self):
        assert JS.count("credentials: 'same-origin'") >= 2


class TestCssGlobalTraps:
    """`static/style.css` opens with type selectors that outrank class rules."""

    def _block(self, selector):
        match = re.search(re.escape(selector) + r"[^{]*\{([^}]*)\}", CSS)
        assert match, f"no rule for {selector}"
        return match.group(1)

    def test_panel_buttons_declare_their_own_height(self):
        # button { height: 32px } clamps anything that doesn't say otherwise.
        for selector in (".svc-copy-btn,", ".svc-panel-close"):
            assert "height:" in self._block(selector)

    def test_hover_rules_outrank_the_global_button_hover(self):
        # button:hover is (0,1,1); a single class is (0,1,0) and loses.
        for selector in (".svc-copy-btn:hover:not(:disabled)",
                         ".svc-foot-btn-primary:hover:not(:disabled)"):
            assert selector in CSS

    def test_primary_button_states_its_background_on_hover(self):
        # A filter-only hover washes out in the light theme.
        assert "background:" in self._block(".svc-foot-btn-primary:hover:not(:disabled)")

    def test_chip_dot_has_a_colour_per_status(self):
        for status in ("ok", "degraded", "down"):
            assert f'.svc-health-chip[data-status="{status}"] .svc-health-dot' in CSS
