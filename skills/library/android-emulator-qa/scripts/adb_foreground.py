"""Confirm the expected Android package before a coordinate-based QA phase."""
import argparse
import json
from pathlib import Path
import re
import subprocess

COMPONENT = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)/[\w.$]+")


def package_for(lines, marker):
    packages = {
        match.group(1)
        for line in lines.splitlines()
        if line.lstrip().startswith(marker)
        for match in COMPONENT.finditer(line)
    }
    return next(iter(packages)) if len(packages) == 1 else None


def ime_visibility(window_dump):
    states = {
        match.group(1)
        for line in window_dump.splitlines()
        if (match := re.match(r"\s*InsetsSource\s+.*\btype=ime\b.*\bvisible=(true|false)\b", line))
    }
    return next(iter(states)) == "true" if len(states) == 1 else None


def check_foreground(activity_dump, window_dump, expected, expect_ime=None):
    activity = package_for(activity_dump, "topResumedActivity=")
    if activity is None and "topResumedActivity=" not in activity_dump:
        activity = package_for(activity_dump, "mResumedActivity:")
    window = package_for(window_dump, "mCurrentFocus=")
    result = {
        "expectedPackage": expected,
        "resumedPackage": activity,
        "focusedPackage": window,
        "matched": activity == window == expected,
    }
    if expect_ime is not None:
        visible = ime_visibility(window_dump)
        result.update(imeVisible=visible, imeMatched=visible is (expect_ime == "visible"))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--package", required=True)
    parser.add_argument("--expect-ime", choices=("hidden", "visible"))
    args = parser.parse_args()
    if not args.adb.is_absolute() or not args.adb.is_file():
        parser.error("use the absolute path to the Android SDK adb executable")
    base = [str(args.adb), "-s", args.serial, "shell", "dumpsys"]
    try:
        activity = subprocess.check_output(base + ["activity", "activities"], text=True, errors="replace", timeout=12)
        window = subprocess.check_output(base + ["window"], text=True, errors="replace", timeout=12)
    except (subprocess.SubprocessError, OSError) as error:
        print(json.dumps({"serial": args.serial, "matched": False, "error": str(error)}))
        return 2
    result = check_foreground(activity, window, args.package, args.expect_ime)
    print(json.dumps({"serial": args.serial, **result}))
    return 0 if result["matched"] and result.get("imeMatched", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
