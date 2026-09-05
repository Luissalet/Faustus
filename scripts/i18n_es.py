"""Generate studio/src/i18n/es.ts from docs/ui/i18n/es.tsv.

The table is the source of truth for Spanish: one line per string,
`English<TAB>Español`, sorted. Keys are the English strings as written at the
call sites (`t('Save')`); a `#`-suffixed key (`{n} pinned#`) is a plural or
gender form English does not distinguish, and studio/src/i18n/en.ts maps it
back to the plain English. Run after editing the table:

    python scripts/i18n_es.py

`--check` exits non-zero when a key used in the code has no Spanish row.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TSV = os.path.join(ROOT, "docs", "ui", "i18n", "es.tsv")
OUT = os.path.join(ROOT, "studio", "src", "i18n", "es.ts")
SRC = os.path.join(ROOT, "studio", "src")


def load() -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    with io.open(TSV, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if "\t" in line:
                k, v = line.split("\t", 1)
                rows.append((k, v))
    rows.sort()
    return rows


def used_keys() -> set[str]:
    keys: set[str] = set()
    for base, _dirs, files in os.walk(SRC):
        if os.path.basename(base) in ("i18n", "gallery"):
            continue
        for name in files:
            if not name.endswith((".ts", ".tsx")):
                continue
            s = io.open(os.path.join(base, name), encoding="utf-8").read()
            for m in re.finditer(r"(?<![A-Za-z_.])t\(\s*'((?:[^'\\]|\\.)*)'", s):
                keys.add(m.group(1).replace("\\'", "'"))
            for m in re.finditer(r'(?<![A-Za-z_.])t\(\s*"((?:[^"\\]|\\.)*)"', s):
                keys.add(m.group(1))
            for m in re.finditer(r"tn\([^,]+,\s*'((?:[^'\\]|\\.)*)',\s*'((?:[^'\\]|\\.)*)'", s):
                keys.add(m.group(1).replace("\\'", "'"))
                keys.add(m.group(2).replace("\\'", "'"))
    return keys


def main() -> int:
    rows = load()
    body = ",\n".join("  %s: %s" % (json.dumps(k, ensure_ascii=False), json.dumps(v, ensure_ascii=False)) for k, v in rows)
    text = (
        "/* eslint-disable */\n/**\n * Spanish. Keys are the English source strings; generated from the\n"
        " * translation table (docs/ui/i18n/es.tsv) by scripts/i18n_es.py -- edit the\n * table, not this file.\n */\n"
        "export const es: Record<string, string> = {\n" + body + ",\n};\n"
    )
    with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("es.ts: %d strings" % len(rows))
    if "--check" in sys.argv:
        have = {k for k, _ in rows}
        missing = sorted(k for k in used_keys() if k not in have)
        for k in missing:
            print("missing:", k)
        return 1 if missing else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
