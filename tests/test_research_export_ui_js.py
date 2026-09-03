"""The research panel's export control, pinned by reading its source.

`static/js/research/panel.js` can't be executed here — it imports a synapse
visualiser and touches the DOM at module scope — so this asserts the contract
of the wiring instead, which is where the mistakes are:

  * downloading through `window.open` instead of fetch + Blob. The panel does
    that for the *visual report* quite correctly, but an export can fail with a
    415 whose body names the package to install, and `window.open` cannot see a
    response body: the user would get a blank tab.
  * offering PDF on a server that has no reportlab, when a probe already says
    so.
"""

import re
from pathlib import Path

import pytest

_PANEL = Path(__file__).resolve().parents[1] / "static" / "js" / "research" / "panel.js"


@pytest.fixture(scope="module")
def source():
    return _PANEL.read_text(encoding="utf-8")


def test_the_finished_report_card_has_an_export_button(source):
    assert 'data-action="export"' in source
    assert re.search(r'querySelector\(\'\[data-action="export"\]\'\)\s*\.addEventListener',
                     source)


def test_the_menu_offers_markdown_word_and_pdf(source):
    menu = re.search(r"_REPORT_EXPORT_FORMATS\s*=\s*\[(.*?)\]", source, re.S)
    assert menu, "the export format list is gone"
    assert re.findall(r"id:\s*'([a-z]+)'", menu.group(1)) == ["md", "docx", "pdf"]
    assert "Word (.docx)" in menu.group(1)


def test_the_download_goes_through_the_shared_export_client(source):
    """fetch + Blob, so a failed export can show what the server said."""
    assert "import { downloadExport } from '../chatExport.js'" in source
    assert "downloadExport(" in source
    assert "/api/research/export/${encodeURIComponent(job.id)}?format=${fmt}" in source


def test_a_failed_export_shows_the_servers_own_message(source):
    """The 415 body names the missing package — a generic failure loses that."""
    assert re.search(r"onError:\s*\(msg\)\s*=>\s*alert\(msg\)", source)


def test_the_menu_greys_out_what_the_server_cannot_produce(source):
    assert "/api/research/export-formats" in source
    assert re.search(r"available\[row\.dataset\.exportFmt\]\s*!==\s*false", source)
    assert "row.disabled = true" in source


def test_the_export_button_does_not_open_the_visual_report(source):
    """The card's own click handler opens the report; the button must not."""
    handler = re.search(
        r'\[data-action="export"\]\'\)\.addEventListener\(\'click\', \(e\) => \{(.*?)\}\);',
        source, re.S)
    assert handler
    assert "e.stopPropagation();" in handler.group(1)
    assert "window.open" not in handler.group(1)
