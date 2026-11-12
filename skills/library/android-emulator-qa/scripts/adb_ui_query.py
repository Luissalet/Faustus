"""Read enabled nodes in the current unobscured Android UI without sending input."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import time
import uuid
import xml.etree.ElementTree as ET

from adb_foreground import check_foreground

BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


def find_nodes(xml, package, label, rect, class_name=None, min_height=1):
    left, top, right, bottom = rect
    result = []
    for node in ET.fromstring(xml).iter("node"):
        if node.get("package") != package or node.get("enabled") != "true":
            continue
        labels = (node.get("content-desc"), node.get("text"))
        observed_label = label if label is not None else next((value for value in labels if value), None)
        if not observed_label or observed_label not in labels:
            continue
        if class_name is not None and node.get("class") != class_name:
            continue
        match = BOUNDS.fullmatch(node.get("bounds", ""))
        if match is None:
            continue
        x1, y1, x2, y2 = map(int, match.groups())
        if not (left <= x1 < x2 <= right and top <= y1 < y2 <= bottom and y2 - y1 >= min_height):
            continue
        result.append({
            "label": observed_label,
            "text": node.get("text", ""),
            "class": node.get("class", ""),
            "bounds": [x1, y1, x2, y2],
            "center": [(x1 + x2) // 2, (y1 + y2) // 2],
            "focused": node.get("focused") == "true",
        })
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--adb", type=Path, required=True)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--package", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--label", help="return one exact label")
    mode.add_argument("--list", action="store_true", help="list up to 80 labeled controls within the rectangle")
    parser.add_argument("--rect", type=int, nargs=4, metavar=("LEFT", "TOP", "RIGHT", "BOTTOM"), required=True)
    parser.add_argument("--class", dest="class_name")
    parser.add_argument("--min-height", type=int, default=1)
    parser.add_argument("--expect-ime", choices=("hidden", "visible"))
    parser.add_argument("--wait-tree-ms", type=int, default=0,
                        help="retry fresh tree reads while the expected app and IME still match")
    args = parser.parse_args()
    if not 0 <= args.wait_tree_ms <= 15000:
        parser.error("tree wait must be between 0 and 15000 ms")
    if not args.adb.is_absolute() or not args.adb.is_file():
        parser.error("use the absolute path to the Android SDK adb executable")
    left, top, right, bottom = args.rect
    if not (0 <= left < right and 0 <= top < bottom and args.min_height >= 1):
        parser.error("provide a positive minimum height and an observed unobscured rectangle")

    adb = [str(args.adb), "-s", args.serial]
    result = {"serial": args.serial, "matched": False}
    try:
        def observe(*command):
            return subprocess.check_output(adb + list(command), timeout=15)

        def foreground():
            return check_foreground(
                observe("shell", "dumpsys", "activity", "activities").decode(errors="replace"),
                observe("shell", "dumpsys", "window").decode(errors="replace"),
                args.package, args.expect_ime,
            )

        deadline = time.monotonic() + args.wait_tree_ms / 1000

        def wait_for_next_tree():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(.15, remaining))
            return time.monotonic() < deadline

        while True:
            before = foreground()
            if not before["matched"] or not before.get("imeMatched", True):
                print(json.dumps({**result, "foreground": before}))
                return 1
            remote = "/data/local/tmp/faustus-ui-" + uuid.uuid4().hex + ".xml"
            try:
                try:
                    observe("shell", "uiautomator", "dump", remote)
                    xml = observe("exec-out", "cat", remote)
                finally:
                    observe("shell", "rm", "-f", remote)
                nodes = find_nodes(xml, args.package, args.label, args.rect, args.class_name, args.min_height)
            except (subprocess.CalledProcessError, ET.ParseError):
                if wait_for_next_tree():
                    continue
                raise
            after = foreground()
            if not after["matched"] or not after.get("imeMatched", True):
                print(json.dumps({**result, "foreground": after}))
                return 1
            ready = bool(nodes) if args.list else len(nodes) == 1
            if ready or not wait_for_next_tree():
                break
    except (OSError, subprocess.SubprocessError, ET.ParseError) as error:
        print(json.dumps({**result, "error": str(error)}))
        return 2
    if args.list:
        print(json.dumps({**result, "matched": bool(nodes), "nodes": nodes[:80], "matchCount": len(nodes)}))
        return 0 if nodes else 1
    if len(nodes) != 1:
        print(json.dumps({**result, "matchCount": len(nodes)}))
        return 1
    print(json.dumps({**result, "matched": True, "node": nodes[0]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
