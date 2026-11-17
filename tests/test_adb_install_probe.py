from pathlib import Path
import runpy

import pytest


SKILL = Path(__file__).resolve().parents[1] / "skills/library/android-emulator-qa/scripts/adb_install_probe.py"
PROBE = runpy.run_path(str(SKILL))
PACKAGE = "com.example.dex"


def test_toybox_storage_probe_reads_kibibytes_on_data_mount():
    output = "Filesystem 1K-blocks Used Available Use% Mounted on\r\n/dev/block/dm-6 16337876 13528376 2662044 84% /data/user/0\r\n"
    assert PROBE["available_bytes"](output) == 2662044 * 1024
    with pytest.raises(ValueError, match="/data"):
        PROBE["available_bytes"](output.replace("/data/user/0", "/system"))


def test_probe_checks_the_expected_avd_before_storage_or_package():
    calls = []

    def read(*command):
        calls.append(command)
        return "device" if command == ("get-state",) else "Other_AVD\r\nOK\r\n"

    with pytest.raises(ValueError, match="authorized AVD"):
        PROBE["inspect_device"](read, "Task_AVD", PACKAGE, 10)
    assert calls == [("get-state",), ("emu", "avd", "name")]


def test_probe_reports_raw_space_and_installed_version_without_installing():
    replies = {
        ("get-state",): "device\n",
        ("emu", "avd", "name"): "Task_AVD\nOK\n",
        ("shell", "df", "-k", "/data"): "Filesystem 1K-blocks Used Available Use% Mounted on\n/dev/root 100 90 10 90% /data\n",
        ("shell", "getprop", "sys.boot_completed"): "1\n",
        ("shell", "cmd", "activity", "get-current-user"): "0\n",
        ("shell", "dumpsys", "package", PACKAGE): "    versionCode=803 minSdk=24 targetSdk=36\n    User 0: installed=true\n",
    }
    result = PROBE["inspect_device"](lambda *command: replies.pop(command), "Task_AVD", PACKAGE, 12000)
    assert result["installedVersionCode"] == 803
    assert result["dataAvailableBytes"] == 10240
    assert result["rawApkFitsBySize"] is False
    assert replies == {}


def test_version_probe_does_not_treat_missing_or_ambiguous_output_as_installed():
    observed = PROBE["installed_version"]
    assert observed(f"Unable to find package: {PACKAGE}\n", PACKAGE, 0) is None
    with pytest.raises(ValueError, match="could not be determined"):
        observed("versionCode=802\nversionCode=803\n", PACKAGE, 0)


def test_a_package_retained_with_data_is_not_reported_as_installed():
    text = "versionCode=803\n    User 0: ceDataInode=1016949 installed=false hidden=false\n"
    assert PROBE["installed_version"](text, PACKAGE, 0) is None


def test_transport_can_be_ready_while_the_guest_is_still_booting():
    replies = {
        ("get-state",): "device\n",
        ("emu", "avd", "name"): "Task_AVD\nOK\n",
        ("shell", "df", "-k", "/data"): "/dev/root 100 20 80 20% /data\n",
        ("shell", "getprop", "sys.boot_completed"): "\n",
    }
    result = PROBE["inspect_device"](lambda *command: replies.pop(command), "Task_AVD", PACKAGE, 2000)
    assert result["bootCompleted"] is False
    assert result["installedVersionCode"] is None
    assert replies == {}
