"""Binding a folder must be reachable, not buried three levels down.

Measured in the running app before this: the chat opens in Chat mode, the
workspace pill is `display:none` until a folder already exists, and its title
reads "Workspace - click to clear" — the control existed only to *remove* the
folder. The single entry point was `#overflow-workspace-btn`, inside the "+"
overflow menu, itself only rendered in Agent mode. Four clicks (Agent →
chevron → Workspace → Use this folder) with nothing on screen saying a folder
was needed at all, while the empty state spent its best space on rotating tips.

These pin the two things that fixed it, at the source level (the JS is
browser-only):

  * the pill is present for the whole of Agent mode and *opens the picker*
    when nothing is bound — while the ✕ keeps clearing, and must not fall
    through into the picker;
  * the empty chat says so, in Agent mode, with a button onto the same dialog.
"""

from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_INDEX = (_REPO / "static" / "index.html").read_text(encoding="utf-8")
_WORKSPACE_JS = (_REPO / "static" / "js" / "workspace.js").read_text(encoding="utf-8")
_CSS = (_REPO / "static" / "style.css").read_text(encoding="utf-8")


def _body(src: str, header: str) -> str:
    """The `header` declaration plus its brace-balanced body.

    The parameter list is skipped first: default values like `options = {}`
    would otherwise be mistaken for the start of the function body.
    """
    i = src.index(header)
    p = src.index("(", i)
    depth = 0
    for k in range(p, len(src)):
        if src[k] == "(":
            depth += 1
        elif src[k] == ")":
            depth -= 1
            if depth == 0:
                p = k
                break
    j = src.index("{", p)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


# ── The indicator ────────────────────────────────────────────────────────

def test_indicator_is_no_longer_a_clear_only_button():
    """The old markup announced itself as a delete control and nothing else."""
    assert 'title="Workspace - click to clear"' not in _INDEX
    assert 'aria-label="Clear workspace"' not in _INDEX
    btn = _INDEX[_INDEX.index('id="workspace-indicator-btn"') - 400:]
    btn = btn[:btn.index("</button>")]
    # Its resting state is "nothing bound yet, click me", and it says so.
    assert "workspace-unset" in btn
    assert "No folder" in btn


def test_indicator_is_visible_for_all_of_agent_mode_not_only_once_bound():
    """`(path && !chat)` was the bug: the only toolbar entry point appeared
    after the thing it exists to help you do."""
    body = _body(_WORKSPACE_JS, "export function syncWorkspaceIndicator(")
    assert "pill.style.display = chat ? 'none' : '';" in body
    assert "(path && !chat)" not in body
    # And it swaps identity with the bound state rather than disappearing.
    assert "pill.classList.toggle('workspace-unset', !path);" in body
    assert "NO_FOLDER_LABEL" in body


def test_indicator_states_have_their_own_title_and_aria_label():
    body = _body(_WORKSPACE_JS, "export function syncWorkspaceIndicator(")
    unset = body[body.index("} else {"):]
    assert "pill.title" in unset and "click to choose one" in unset
    assert "pill.setAttribute('aria-label', 'Choose a workspace folder');" in unset
    bound = body[body.index("if (path) {"):body.index("} else {")]
    assert "pill.title" in bound
    assert "aria-label" in bound and "change folder" in bound


def test_clicking_the_indicator_opens_the_picker():
    body = _body(_WORKSPACE_JS, "export function initWorkspace(")
    assert "openWorkspaceBrowser();" in body
    # The old wiring was the whole pill == clear.
    assert "pill.addEventListener('click', clearWorkspace)" not in body


def test_the_x_still_clears_and_does_not_fall_through_to_the_picker():
    """The one gesture that must survive: ✕ removes the folder, silently."""
    body = _body(_WORKSPACE_JS, "export function initWorkspace(")
    handler = body[body.index("pill.addEventListener("):]
    x_idx = handler.index(".tool-indicator-x")
    clear_idx = handler.index("clearWorkspace();")
    open_idx = handler.index("openWorkspaceBrowser();")
    # Hit-test first, clear, and return before the picker call is reached.
    assert x_idx < clear_idx < open_idx
    assert "return;" in handler[clear_idx:open_idx]
    # Clearing is gated on there being something to clear.
    assert "getWorkspace()" in handler[x_idx:clear_idx]


def test_clearing_stays_reachable_from_the_keyboard():
    """The pill's default action is now "choose a folder", so unbinding needs a
    home that is not a mouse-only hit box inside a button."""
    assert "id=\"workspace-clear\"" in _WORKSPACE_JS
    body = _body(_WORKSPACE_JS, "export async function openWorkspaceBrowser(")
    # Shown only for the global workspace (the project editor borrows the same
    # dialog through onPick) and only when there is something to clear.
    assert "clearBtn.hidden = !!_onPick || !getWorkspace();" in body


# ── The empty state ──────────────────────────────────────────────────────

def test_empty_state_has_a_workspace_slot_inside_the_welcome_screen():
    """Inside #welcome-screen, so it is shown exactly when the chat is empty
    (chatRenderer toggles .hidden on that element) with no extra plumbing."""
    welcome = _INDEX[_INDEX.index('<div id="welcome-screen">'):]
    welcome = welcome[:welcome.index('<div id="chat-history"')]
    assert 'id="welcome-workspace"' in welcome
    assert 'id="welcome-workspace-btn"' in welcome
    assert 'id="welcome-workspace-text"' in welcome
    # Hidden until JS decides — chat mode never shows it.
    assert "hidden" in welcome[welcome.index('id="welcome-workspace"'):
                               welcome.index('id="welcome-workspace-text"')]


def test_agent_mode_without_a_folder_gets_the_call_to_action():
    body = _body(_WORKSPACE_JS, "function _syncWelcomeWorkspace(")
    assert "box.hidden = !!chat;" in body          # chat mode: not applicable
    unset = body[body.index("} else {"):]
    assert "No folder linked" in unset
    assert "Choose folder" in unset


def test_with_a_folder_the_empty_state_names_it():
    body = _body(_WORKSPACE_JS, "function _syncWelcomeWorkspace(")
    bound = body[body.index("if (path) {"):body.index("} else {")]
    assert "Working in ${_basename(path)}" in bound
    assert "Change folder" in bound


def test_the_call_to_action_reuses_the_existing_picker():
    """No second folder dialog: the button opens workspace.js's own browser."""
    body = _body(_WORKSPACE_JS, "export function initWorkspace(")
    assert "welcome-workspace-btn" in body
    wired = body[body.index("welcome-workspace-btn"):]
    assert "openWorkspaceBrowser()" in wired


def test_empty_state_sync_is_driven_by_the_same_call_as_the_pill():
    """One code path keeps mode, pill and empty state consistent — applyMode()
    and setWorkspace() both go through syncWorkspaceIndicator()."""
    body = _body(_WORKSPACE_JS, "export function syncWorkspaceIndicator(")
    assert "_syncWelcomeWorkspace(path, chat);" in body
    assert "syncWorkspaceIndicator(getWorkspace());" in _body(
        _WORKSPACE_JS, "export function applyMode(")
    assert "syncWorkspaceIndicator(path || '');" in _body(
        _WORKSPACE_JS, "export function setWorkspace(")


def test_a_rotating_tip_mentions_the_workspace_on_both_device_lists():
    """Complementary, not the fix — but the tip list had nothing about the one
    thing agent mode cannot work without."""
    script = _INDEX[_INDEX.index("var desktop = ["):_INDEX.index("var tips = mobile")]
    desktop = script[:script.index("var phone = [")]
    phone = script[script.index("var phone = ["):]
    assert "workspace pill" in desktop
    assert "workspace pill" in phone


# ── Presentation ─────────────────────────────────────────────────────────

def test_the_unset_pill_hides_the_clear_affordance():
    """Nothing to clear, so no ✕ — and it has to beat the mobile rule that
    force-shows it (`.tool-indicator .tool-indicator-x { display: … !important }`)."""
    assert ("#workspace-indicator-btn.workspace-unset .tool-indicator-x "
            "{ display: none !important; }") in _CSS


def test_the_call_to_action_is_clickable_through_the_welcome_overlay():
    """#welcome-screen is pointer-events:none; anything interactive in it has
    to opt back in or the button is dead on arrival."""
    block = _CSS[_CSS.index(".welcome-workspace {"):]
    block = block[:block.index("@media (max-height: 380px)")]
    assert block.count("pointer-events: auto;") >= 2


def test_both_themes_are_driven_by_tokens_not_hardcoded_colours():
    """theme.js writes --bg/--fg straight onto :root for every palette, light
    or dark, so a literal hex here would be wrong in one of them."""
    block = _CSS[_CSS.index(".welcome-workspace {"):]
    block = block[:block.index("@media (max-height: 380px)")]
    import re
    declarations = re.sub(r"/\*.*?\*/", "", block, flags=re.S)
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b", declarations), declarations
