"""Confirm the Android foreground package or dismiss a verified visible IME."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time

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


def query_foreground(base, package, expect_ime, wait_ime_ms):
    deadline = None
    previous = None
    probes = (("activity", "activities"), ("window",))
    while True:
        dumps = []
        for probe in probes:
            remaining = 12 if deadline is None else min(12, deadline - time.monotonic())
            if remaining <= 0:
                return previous
            try:
                dumps.append(subprocess.check_output(
                    base + list(probe), text=True, errors="replace", timeout=remaining,
                ))
            except subprocess.TimeoutExpired as error:
                if deadline is not None:
                    return previous
                return {"matched": False, "probe": probe[0], "error": str(error)}
            except subprocess.CalledProcessError as error:
                result = {"matched": False, "probe": probe[0], "error": str(error)}
                break
            except (subprocess.SubprocessError, OSError) as error:
                return {"matched": False, "probe": probe[0], "error": str(error)}
        else:
            result = check_foreground(*dumps, package, expect_ime)
            if not result["matched"] or result.get("imeMatched", True):
                return result
        if not wait_ime_ms:
            return result
        previous = result
        if deadline is None:
            deadline = time.monotonic() + wait_ime_ms / 1000
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return result
        time.sleep(min(.15, remaining))


def dismiss_visible_ime(device, package, wait_ime_ms):
    base = device + ["shell", "dumpsys"]
    before = query_foreground(base, package, "visible", 0)
    if not before["matched"] or not before.get("imeMatched"):
        return {**before, "action": "dismiss-ime", "phase": "before-back", "backDispatched": False}
    try:
        subprocess.check_output(
            device + ["shell", "input", "keyevent", "KEYCODE_BACK"],
            text=True, errors="replace", timeout=12,
        )
    except (subprocess.SubprocessError, OSError) as error:
        return {"matched": False, "action": "dismiss-ime", "phase": "dispatch",
                "backDispatched": None, "error": str(error)}
    after = query_foreground(base, package, "hidden", wait_ime_ms)
    return {**after, "action": "dismiss-ime", "phase": "after-back", "backDispatched": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--package", required=True)
    ime = parser.add_mutually_exclusive_group()
    ime.add_argument("--expect-ime", choices=("hidden", "visible"))
    ime.add_argument("--dismiss-ime", action="store_true")
    parser.add_argument("--wait-ime-ms", type=int)
    args = parser.parse_args()
    wait_ime_ms = args.wait_ime_ms if args.wait_ime_ms is not None else (1500 if args.dismiss_ime else 0)
    if not 0 <= wait_ime_ms <= 4000 or (wait_ime_ms and not (args.expect_ime or args.dismiss_ime)):
        parser.error("--wait-ime-ms must be between 0 and 4000 and requires --expect-ime or --dismiss-ime")
    if not args.adb.is_absolute() or not args.adb.is_file():
        parser.error("use the absolute path to the Android SDK adb executable")
    device = [str(args.adb), "-s", args.serial]
    if args.dismiss_ime:
        result = dismiss_visible_ime(device, args.package, wait_ime_ms)
    else:
        result = query_foreground(device + ["shell", "dumpsys"], args.package, args.expect_ime, wait_ime_ms)
    print(json.dumps({"serial": args.serial, **result}))
    if "error" in result:
        return 2
    return 0 if result["matched"] and result.get("imeMatched", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
