"""Getting a folder attached, from the two places it is missed.

In agent mode without a folder the agent cannot read or edit anything, and
the failure is silent: it answers about code it never saw. Two entry points
fix that, and both were once broken in the same way — the control existed
but only appeared once a folder was already chosen, which is exactly when it
is not needed.

  1. The composer chip: visible in agent mode whether or not a folder is set,
     saying which state it is in, opening the picker on click, and clearing
     with its own × that does not fall through to the picker.
  2. The empty state: with no folder it says so and offers the same picker —
     not a second dialog of its own.
"""
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_COMPOSER = (_REPO / "studio" / "src" / "screens" / "studio" / "Composer.tsx").read_text(encoding="utf-8")
_STUDIO = (_REPO / "studio" / "src" / "screens" / "Studio.tsx").read_text(encoding="utf-8")


def _chip() -> str:
    """The folder chip and its ×, as one block."""
    start = _COMPOSER.index('className="fs-studio__chipgroup"')
    return _COMPOSER[start:_COMPOSER.index("</span>", _COMPOSER.index("fs-studio__chip-x", start))]


def test_the_indicator_is_not_a_clear_only_button():
    """It opens the picker. A control that can only remove what you have is
    no use when you have nothing."""
    assert "onClick={onPickWorkspace}" in _chip()


def test_the_indicator_is_there_whether_or_not_a_folder_is_set():
    """`workspace ? … : …` inside the label, not `workspace && <button>`
    around it: the empty state is the one that needs the button."""
    chip = _chip()
    assert "workspace ? basename(workspace) : t('Choose folder')" in chip
    assert "{workspace && (\n                  <button\n                    type=\"button\"\n                    className=\"fs-studio__chip\"" not in chip


def test_each_state_says_what_it_is():
    """Both in the tooltip and to a screen reader: an icon alone does not
    distinguish "no folder" from "a folder whose name is off-screen"."""
    chip = _chip()
    assert "aria-pressed={Boolean(workspace)}" in chip
    assert "No folder: the agent cannot read or edit files" in chip
    assert "t('Folder')" in chip


def test_clearing_is_its_own_control_and_does_not_open_the_picker():
    chip = _chip()
    x = chip[chip.index("fs-studio__chip-x"):]
    assert "onClick={onClearWorkspace}" in x
    assert "onPickWorkspace" not in x, "the × must not fall through to the picker"


def test_clearing_stays_reachable_from_the_keyboard():
    """A real <button> with a label, not an × painted on the chip."""
    chip = _chip()
    x = chip[chip.rindex("<button", 0, chip.index("fs-studio__chip-x")):]
    assert 'type="button"' in x
    assert "aria-label={t('Remove the folder')}" in x


def test_the_call_to_action_reuses_the_existing_picker():
    """No second folder dialog: the empty state opens the same one."""
    assert "onPickWorkspace={() => void pickWorkspace()}" in _STUDIO
    assert _STUDIO.count("const pickWorkspace") == 1, (
        "one picker; a second implementation is how the two drifted apart"
    )
