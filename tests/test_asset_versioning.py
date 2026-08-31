"""Content-hashed asset versions (FAUSTUS).

The bug this removes: `style.css?v=<hand-typed token>`. Every CSS change needed
a second, unrelated edit to be visible, and forgetting it produced the worst
kind of bug report — "your fix did nothing" — from a browser serving last
week's stylesheet.
"""

from pathlib import Path

from src.app_helpers import asset_version, substitute_asset_versions

ROOT = Path(__file__).resolve().parents[1]
HTML = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


class TestVersion:
    def test_version_follows_the_content(self, tmp_path):
        f = tmp_path / "style.css"
        f.write_text("a{}", encoding="utf-8")
        first = asset_version(str(f))
        f.write_text("a{color:red}", encoding="utf-8")
        assert asset_version(str(f)) != first

    def test_same_content_same_version(self, tmp_path):
        f = tmp_path / "style.css"
        f.write_text("a{}", encoding="utf-8")
        assert asset_version(str(f)) == asset_version(str(f))

    def test_missing_file_degrades_instead_of_raising(self, tmp_path):
        assert asset_version(str(tmp_path / "nope.css"))


class TestSubstitution:
    def test_placeholder_is_replaced_with_the_hash(self, tmp_path):
        (tmp_path / "style.css").write_text("body{}", encoding="utf-8")
        out = substitute_asset_versions(
            '<link href="/static/style.css?v={{ASSET_V:style.css}}">', str(tmp_path))
        assert "{{ASSET_V" not in out
        assert asset_version(str(tmp_path / "style.css")) in out

    def test_traversal_is_refused(self, tmp_path):
        """The placeholder names a file; it must not become a file-read oracle."""
        secret = tmp_path.parent / "secret.env"
        secret.write_text("KEY=1", encoding="utf-8")
        out = substitute_asset_versions("{{ASSET_V:../secret.env}}", str(tmp_path))
        assert asset_version(str(secret)) not in out

    def test_unknown_asset_still_produces_a_token(self, tmp_path):
        out = substitute_asset_versions("{{ASSET_V:ghost.css}}", str(tmp_path))
        assert out and "{{" not in out


class TestPage:
    def test_index_uses_the_placeholder_for_the_stylesheet(self):
        assert "style.css?v={{ASSET_V:style.css}}" in HTML

    def test_js_modules_keep_literal_tokens(self):
        """Their tokens also appear in `import ... from './x.js?v=…'`; templating
        one side would load the same module twice under two URLs."""
        assert "{{ASSET_V:js/" not in HTML
