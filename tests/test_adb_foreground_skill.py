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
