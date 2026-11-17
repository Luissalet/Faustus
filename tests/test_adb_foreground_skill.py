from collections import deque
import subprocess
from types import SimpleNamespace
from pathlib import Path
import runpy

CHECK = runpy.run_path(str(
    Path(__file__).resolve().parents[1]
    / "skills/library/android-emulator-qa/scripts/adb_foreground.py"
))["check_foreground"]


def test_foreground_check_accepts_launcher_alias_of_the_expected_package():
    result = CHECK(
        "topResumedActivity=ActivityRecord{10 u0 com.example.dex/.LauncherAlias t4}",
        "mCurrentFocus=Window{10 u0 com.example.dex/com.example.dex.LauncherAlias}",
        "com.example.dex",
    )
    assert result["matched"] is True


def test_foreground_check_detects_a_reference_app_or_system_overlay():
    expected = "com.example.dex"
    activity = "topResumedActivity=ActivityRecord{10 u0 com.example.reference/.Main t4}"
    window = "mCurrentFocus=Window{10 u0 com.example.reference/.Main}"
    assert CHECK(activity, window, expected)["resumedPackage"] == "com.example.reference"
    assert CHECK(activity, window, expected)["matched"] is False
    resumed = "topResumedActivity=ActivityRecord{10 u0 com.example.dex/.Main t4}"
    overlay = "mCurrentFocus=Window{20 u0 com.android.settings/.Permission}"
    assert CHECK(resumed, overlay, expected)["matched"] is False


def test_foreground_check_requires_both_observations():
    assert CHECK("topResumedActivity=null", "mCurrentFocus=null", "com.example.dex")["matched"] is False



def test_ime_expectation_uses_window_insets_and_ignores_stale_input_state():
    activity = "topResumedActivity=ActivityRecord{10 u0 com.example.dex/.Main t4}"
    window = "mCurrentFocus=Window{10 u0 com.example.dex/.Main}\nmIsInputViewShown=true\n"
    source = "InsetsSource id=3 type=ime frame=[0,0][0,0] visibleFrame=[0,1523][720,1600] visible="
    hidden = CHECK(activity, window + source + "false", "com.example.dex", "hidden")
    shown = CHECK(activity, window + source + "true", "com.example.dex", "hidden")
    assert hidden["matched"] and hidden["imeMatched"] and hidden["imeVisible"] is False
    assert shown["matched"] and not shown["imeMatched"] and shown["imeVisible"] is True
    assert CHECK(activity, window + source + "true", "com.example.dex", "visible")["imeMatched"]
    assert "imeVisible" not in CHECK(activity, window, "com.example.dex")


def test_ime_expectation_fails_when_window_insets_are_missing_or_conflict():
    activity = "topResumedActivity=ActivityRecord{10 u0 com.example.dex/.Main t4}"
    window = "mCurrentFocus=Window{10 u0 com.example.dex/.Main}\n"
    for insets in ("", "InsetsSource id=3 type=ime visible=false\nInsetsSource id=3 type=ime visible=true"):
        result = CHECK(activity, window + insets, "com.example.dex", "hidden")
        assert result["matched"] and not result["imeMatched"] and result["imeVisible"] is None


QUERY = CHECK.__globals__["query_foreground"]
DEX = "com.example.dex"
ACTIVITY = "topResumedActivity=ActivityRecord{10 u0 com.example.dex/.Main t4}"
WINDOW = "mCurrentFocus=Window{10 u0 com.example.dex/.Main}\nInsetsSource id=3 type=ime visible="
ADB = ["adb", "-s", "emulator-5554", "shell", "dumpsys"]


def query_fixture(monkeypatch, responses):
    queued = deque(responses)
    clock = [0.0]
    calls = []

    def check_output(command, *, text, errors, timeout):
        calls.append((command, timeout))
        reply = queued.popleft()
        if isinstance(reply, Exception):
            raise reply
        return reply

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setitem(QUERY.__globals__, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setitem(QUERY.__globals__, "subprocess", SimpleNamespace(
        check_output=check_output, TimeoutExpired=subprocess.TimeoutExpired, SubprocessError=subprocess.SubprocessError,
        CalledProcessError=subprocess.CalledProcessError,
    ))
    return calls


def test_wait_observes_delayed_keyboard_without_input(monkeypatch):
    calls = query_fixture(monkeypatch, [ACTIVITY, WINDOW + "false", ACTIVITY, WINDOW + "true"])
    assert QUERY(ADB, DEX, "visible", 500)["imeMatched"]
    assert [command[5] for command, _ in calls] == ["activity", "window", "activity", "window"]
    assert all(0 < timeout < .5 for _, timeout in calls[2:])


def test_wait_stops_as_soon_as_another_package_takes_focus(monkeypatch):
    overlay = "mCurrentFocus=Window{10 u0 com.android.settings/.Permission}"
    calls = query_fixture(monkeypatch, [ACTIVITY, WINDOW + "false", ACTIVITY, overlay])
    result = QUERY(ADB, DEX, "visible", 500)
    assert not result["matched"]
    assert result["focusedPackage"] == "com.android.settings"
    assert len(calls) == 4


def test_short_wait_preserves_the_last_mismatch(monkeypatch):
    calls = query_fixture(monkeypatch, [ACTIVITY, WINDOW + "false"])
    result = QUERY(ADB, DEX, "visible", 100)
    assert result["matched"] and not result["imeMatched"]
    assert len(calls) == 2


def test_initial_activity_timeout_identifies_the_unresponsive_probe(monkeypatch):
    calls = query_fixture(monkeypatch, [subprocess.TimeoutExpired(ADB, 12)])
    result = QUERY(ADB, DEX, "visible", 1500)
    assert not result["matched"] and result["probe"] == "activity"
    assert "timed out" in result["error"]
    assert len(calls) == 1


def test_wait_rechecks_both_probes_after_temporary_window_failure(monkeypatch):
    error = subprocess.CalledProcessError(1, "dumpsys window")
    calls = query_fixture(monkeypatch, [ACTIVITY, error, ACTIVITY, WINDOW + "false"])
    assert QUERY(ADB, DEX, "hidden", 500)["imeMatched"]
    assert [command[5] for command, _ in calls] == ["activity", "window", "activity", "window"]
    assert all(0 < timeout < .5 for _, timeout in calls[2:])


def test_window_command_failure_keeps_its_probe_without_wait_or_after_deadline(monkeypatch):
    error = subprocess.CalledProcessError(1, "dumpsys window")
    for wait in (0, 100):
        calls = query_fixture(monkeypatch, [ACTIVITY, error])
        result = QUERY(ADB, DEX, "hidden", wait)
        assert not result["matched"] and result["probe"] == "window" and "error" in result
        assert len(calls) == 2


DISMISS = CHECK.__globals__["dismiss_visible_ime"]


def test_dismiss_visible_keyboard_dispatches_one_back_then_observes_hidden(monkeypatch):
    calls = query_fixture(monkeypatch, [ACTIVITY, WINDOW + "true", "", ACTIVITY, WINDOW + "false"])
    result = DISMISS(ADB[:3], DEX, 1500)
    assert result["matched"] and result["imeMatched"] and result["imeVisible"] is False
    assert result["phase"] == "after-back" and result["backDispatched"] is True
    assert [command for command, _ in calls if command[4] == "input"] == [
        ADB[:3] + ["shell", "input", "keyevent", "KEYCODE_BACK"],
    ]


def test_dismiss_stops_without_input_if_keyboard_is_hidden_ambiguous_or_other_app(monkeypatch):
    for window in (WINDOW + "false", WINDOW.rsplit("\n", 1)[0],
                   WINDOW.replace(DEX, "com.example.other") + "true"):
        calls = query_fixture(monkeypatch, [ACTIVITY, window])
        result = DISMISS(ADB[:3], DEX, 1500)
        assert result["phase"] == "before-back" and result["backDispatched"] is False
        assert len(calls) == 2


def test_dismiss_reports_an_unknown_back_dispatch_and_never_retries(monkeypatch):
    calls = query_fixture(monkeypatch, [ACTIVITY, WINDOW + "true", subprocess.TimeoutExpired("input", 12)])
    result = DISMISS(ADB[:3], DEX, 1500)
    assert result["phase"] == "dispatch" and result["backDispatched"] is None
    assert "error" in result and len(calls) == 3


def test_dismiss_rechecks_the_package_after_dispatch(monkeypatch):
    overlay = "mCurrentFocus=Window{10 u0 com.android.settings/.Permission}"
    calls = query_fixture(monkeypatch, [ACTIVITY, WINDOW + "true", "", ACTIVITY, overlay])
    result = DISMISS(ADB[:3], DEX, 1500)
    assert not result["matched"] and result["backDispatched"] is True
    assert result["focusedPackage"] == "com.android.settings"
    assert len(calls) == 5
