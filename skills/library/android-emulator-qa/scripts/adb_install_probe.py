"""Read storage and the installed version before retrying an emulator APK install."""

import argparse
import json
from pathlib import Path
import re
import subprocess


def available_bytes(output):
    mounts = [
        int(match[1]) * 1024
        for line in output.splitlines()
        if (match := re.fullmatch(r"\S+\s+\d+\s+\d+\s+(\d+)\s+\d+%\s+/data(?:/\S+)?", line.strip()))
    ]
    if len(mounts) != 1:
        raise ValueError("the emulator did not report one /data allocation")
    return mounts[0]


def installed_version(output, package, user):
    versions = {int(code) for code in re.findall(r"^\s*versionCode=(\d+)\b", output, re.M)}
    if not versions and f"Unable to find package: {package}" in output:
        return None
    states = set(re.findall(rf"^\s*User {user}: [^\r\n]*\binstalled=(true|false)\b", output, re.M))
    if states == {"false"}:
        return None
    if states == {"true"} and len(versions) == 1:
        return versions.pop()
    raise ValueError("the package version could not be determined for the active user")


def inspect_device(read_adb, avd, package, apk_bytes):
    if read_adb("get-state").strip() != "device":
        raise ValueError("the authorized emulator is unavailable")
    names = [line.strip() for line in read_adb("emu", "avd", "name").splitlines()
             if line.strip() not in ("", "OK")]
    if names != [avd]:
        raise ValueError("the connected AVD differs from the authorized AVD")
    free = available_bytes(read_adb("shell", "df", "-k", "/data"))
    boot_id = read_adb("shell", "cat", "/proc/sys/kernel/random/boot_id").strip()
    if not re.fullmatch(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", boot_id):
        raise ValueError("the guest did not report a valid boot ID")
    boot_completed = read_adb("shell", "getprop", "sys.boot_completed").strip() == "1"
    result = {
        "avd": avd,
        "package": package,
        "bootId": boot_id,
        "bootCompleted": boot_completed,
        "readiness": "booting",
        "androidUser": None,
        "installedVersionCode": None,
        "dataAvailableBytes": free,
        "apkBytes": apk_bytes,
        "rawApkFitsBySize": free >= apk_bytes,
    }
    if not boot_completed:
        return result
    phase = "activity"
    try:
        user = int(read_adb("shell", "cmd", "activity", "get-current-user").strip())
        result["androidUser"] = user
        phase = "package"
        result["installedVersionCode"] = installed_version(
            read_adb("shell", "dumpsys", "package", package), package, user,
        )
    except (ValueError, subprocess.SubprocessError) as error:
        return {**result, "readiness": f"{phase}-unavailable", "reason": str(error)}
    return {**result, "readiness": "ready"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--avd", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--apk", type=Path, required=True)
    args = parser.parse_args()
    if not args.adb.is_absolute() or not args.adb.is_file():
        parser.error("--adb must be an absolute path to the SDK executable")
    if not re.fullmatch(r"emulator-\d+", args.serial):
        parser.error("--serial must identify the authorized emulator")
    if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", args.package, re.ASCII):
        parser.error("--package must be an Android package name")
    if args.apk.suffix.lower() != ".apk" or not args.apk.is_file():
        parser.error("--apk must name the built APK")
    command = ()

    def read_adb(*args_for_device):
        nonlocal command
        command = args_for_device[:3]
        return subprocess.check_output(
            [str(args.adb), "-s", args.serial, *args_for_device],
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", timeout=12,
        )
    try:
        result = inspect_device(read_adb, args.avd, args.package, args.apk.stat().st_size)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(json.dumps({"serial": args.serial, "ok": False, "probe": command, "reason": str(error)}))
        return 2
    ok = result["readiness"] in ("booting", "ready")
    print(json.dumps({"serial": args.serial, "ok": ok, **result}))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
