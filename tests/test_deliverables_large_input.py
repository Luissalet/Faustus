"""Differential coverage for the historical deliverable extraction grammar."""
import itertools
import random
import re
import subprocess
import sys
from pathlib import Path

from src import deliverables as d


def original(text):
    result = []
    for rx in (d._ASK, d._ASK_AFTER):
        for match in rx.finditer(str(text or "")):
            name = re.split(r"[\\/]", match.group(1))[-1]
            if name and name.lower() not in (value.lower() for value in result):
                result.append(name)
    return result


def assert_equivalent(text):
    raw = str(text or "")
    assert list(d._files_before_description(raw)) == [m.group(1) for m in d._ASK_AFTER.finditer(raw)], repr(text)
    assert d.requested_files(text) == original(text), repr(text)


def test_differential_boundaries_extensions_and_greedy_names():
    paths = ["a", "-", "a-b", "a/b", "a\\b", "./a", "/a", "a.md-b", "漢/İıſK", "á/ñ", "_", "12"]
    tails = [" con x", " with x", " containing x", " que contenga x", "-con", "/with", " con.md con", " con-a.txt with", "\ncon", ".con", " without", " con_", " " * 40 + "con", " " * 41 + "con"]
    prefixes = ["", "Escribe ", "Guarda ", "Write ", "create ", "x/", "a ", "con ", "\n", "漢-"]
    extensions = ["md", "markdown", "txt", "csv", "tsv", "json", "yaml", "yml", "html", "docx", "xlsx", "py", "js", "ts", "sql", "MD", "ſql", "jſ", "mda"]
    for prefix, path, ext, tail in itertools.product(prefixes, paths, extensions, tails):
        text = prefix + path + "." + ext + tail
        assert_equivalent(text)


def test_differential_seeded_mixed_requests_and_nonoverlap():
    rng = random.Random(876)
    atoms = ["a", "b", "-", ".", "/", "\\", "_", "漢", "İ", "ı", "ſ", "K", "\n", " ", "md", "txt", "con", "with", "que contenga", "containing", "crea", "write", "guarda", "a.md", "b.txt con"]
    for _ in range(20000):
        text = "".join(rng.choices(atoms, k=rng.randrange(1, 35)))
        assert_equivalent(text)
    for text in ["a.md.md con", "a.md-con.txt with", "a.md con a.txt con", "a.md con/b.txt with", "write A.MD; a.md con", None, 0]:
        assert_equivalent(text)


def test_original_3900000_character_request_and_path_variants_complete():
    # A subprocess timeout contains a regression without wedging the test runner.
    code = """
from src.deliverables import requested_files
plain = 'current-data-' * 300000
assert requested_files(plain) == []
assert requested_files('https://a.test ' + plain) == []
assert requested_files(plain + ' https://a.test') == []
assert requested_files(plain + ' report.md con detalles') == ['report.md']
assert requested_files('a.md con ' + plain) == ['a.md']
assert requested_files(('a-b/' * 250000) + 'report.md con detalles') == ['report.md']
"""
    subprocess.run([sys.executable, "-B", "-c", code], cwd=Path(__file__).resolve().parents[1], check=True, timeout=15)
