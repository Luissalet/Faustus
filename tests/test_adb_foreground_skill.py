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
