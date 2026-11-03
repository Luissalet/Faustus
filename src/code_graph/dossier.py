"""src/code_graph/dossier.py -- repository intake dossier, "where do I change
X" and diff between two dossiers.

This is a composition layer, not a second mapper: structure comes from
`communities`, main flows from `flows`, routes/index health from
`get_architecture`, symbol lookup from `code_index.search`. What the code graph
does not hold -- build/run/test commands, entry-point declarations in config,
data stores, outbound hosts, environment variables, screens -- is read from the
files themselves with line-anchored patterns.

HONESTY CONTRACT. Every fact carries `evidence` (`path:line`) and a
`certainty`:

* `code`      -- read from source code (a literal line says so).
* `config`    -- read from configuration or documentation (package.json,
                 pyproject, Makefile, Dockerfile, README...).
* `inferred`  -- a heuristic conclusion (a cluster, a conventional command, a
                 URL that is not visibly used by a client call).

Commands are quoted from the file, never composed. What static reading cannot
settle is listed under `uncertainties`, not guessed.

A dossier is persisted per git revision (the working-tree state is part of the
revision id when it is dirty) in the code-graph store, so `dossier_diff` can say
what changed between two revisions of the same repository.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.context_engine import code_index
from src.context_engine import store as ce_store

from .query import (DEFAULT_OUTPUT_CHARS, _clip, _is_test_path, _resolve_symbol, _root,
                    get_architecture)

logger = logging.getLogger(__name__)

DOSSIER_VERSION = 1

ce_store.register_schema("code_graph_dossiers", (
    """
    CREATE TABLE IF NOT EXISTS code_graph_dossiers (
        workspace   TEXT NOT NULL DEFAULT '',
        project_id  TEXT NOT NULL DEFAULT '',
        revision    TEXT NOT NULL DEFAULT '',
        data        TEXT NOT NULL DEFAULT '{}',
        created_at  TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (workspace, project_id, revision)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_code_graph_dossiers_scope "
    "ON code_graph_dossiers(workspace, project_id, created_at)",
))

_INDEX_BUDGET_FILES = 20000
_MAX_FILES = 20000
_MAX_FILE_BYTES = 512_000
_SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", "dist", "build",
              ".tox", ".mypy_cache", ".pytest_cache", "target", ".next", "site-packages"}
_CODE_EXT = (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_SECTIONS = ("structure", "entry_points", "build_run", "test", "data", "integrations",
             "screens", "main_flows", "uncertainties")
_NOISE_HOSTS = ("w3.org", "example.com", "example.org", "localhost", "schemas.", "xmlns",
                "json-schema.org", "mozilla.org", "creativecommons.org", "apache.org",
                "opensource.org", "github.com/", "wikipedia.org", "shields.io")
_CLIENT_CALL_RE = re.compile(
    r"requests\.|httpx\.|urlopen|aiohttp|fetch\(|axios|http\.client|urllib|\.get\(|\.post\(|"
    r"WebSocket|curl", re.I)


def _fact(key: str, fact: str, evidence: str, certainty: str, **extra: Any) -> Dict[str, Any]:
    out = {"key": key, "fact": fact, "evidence": evidence, "certainty": certainty}
    out.update({k: v for k, v in extra.items() if v not in (None, "")})
    return out


# ── revision ─────────────────────────────────────────────────────────────

def _git(root: str, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                              timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _revision(root: str, files: Sequence[str]) -> Tuple[str, str]:
    """(revision id, kind). A clean git checkout is its HEAD; a dirty one adds a
    hash of the uncommitted diff; a non-git tree is a hash of file stats."""
    head = (_git(root, "rev-parse", "HEAD") or "").strip()
    if head:
        status = (_git(root, "status", "--porcelain") or "")
        if status.strip():
            diff = _git(root, "diff", "HEAD") or ""
            h = hashlib.sha1((status + diff).encode("utf-8", "replace")).hexdigest()[:8]
            return f"{head[:12]}+dirty-{h}", "git-dirty"
        return head[:12], "git"
    h = hashlib.sha1()
    for rel in sorted(files):
        try:
            st = os.stat(os.path.join(root, rel))
            h.update(f"{rel}|{st.st_size}|{int(st.st_mtime)}\n".encode())
        except OSError:
            continue
    return "fs-" + h.hexdigest()[:12], "filesystem"


# ── file access ──────────────────────────────────────────────────────────

class _Files:
    def __init__(self, root: str):
        self.root = root
        self.paths = self._list()
        self._cache: Dict[str, List[str]] = {}

    def _list(self) -> List[str]:
        out = _git(self.root, "ls-files", "--cached", "--others", "--exclude-standard")
        paths: List[str] = []
        if out is not None and out.strip():
            for rel in out.splitlines():
                rel = rel.strip().replace("\\", "/")
                if rel and not any(p in _SKIP_DIRS for p in rel.split("/")[:-1]) \
                        and os.path.isfile(os.path.join(self.root, rel)):
                    paths.append(rel)
        else:
            for dirpath, dirnames, filenames in os.walk(self.root):
                dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
                for name in filenames:
                    rel = os.path.relpath(os.path.join(dirpath, name), self.root)
                    paths.append(rel.replace(os.sep, "/"))
                if len(paths) >= _MAX_FILES:
                    break
        return sorted(paths)[:_MAX_FILES]

    def lines(self, rel: str) -> List[str]:
        if rel in self._cache:
            return self._cache[rel]
        full = os.path.join(self.root, *rel.split("/"))
        try:
            if os.path.getsize(full) > _MAX_FILE_BYTES:
                self._cache[rel] = []
                return []
            with open(full, "r", encoding="utf-8", errors="replace") as handle:
                data = handle.read().split("\n")
        except OSError:
            data = []
        self._cache[rel] = data
        return data

    def text(self, rel: str) -> str:
        return "\n".join(self.lines(rel))

    def find(self, pattern: str) -> List[str]:
        rx = re.compile(pattern)
        return [p for p in self.paths if rx.search(p)]

    def grep(self, rel: str, rx: "re.Pattern[str]") -> Iterable[Tuple[int, str, "re.Match[str]"]]:
        for i, line in enumerate(self.lines(rel), 1):
            m = rx.search(line)
            if m:
                yield i, line, m

    def code_files(self, include_tests: bool = False) -> List[str]:
        return [p for p in self.paths if p.endswith(_CODE_EXT)
                and (include_tests or not _is_test_path(p))]


def _find_line(lines: Sequence[str], needle: str, start: int = 0) -> int:
    for i in range(start, len(lines)):
        if needle in lines[i]:
            return i + 1
    return 1


def _short(text: str, n: int = 160) -> str:
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# ── section builders ─────────────────────────────────────────────────────

def _structure(root: str, project_id: str, files: _Files, top: int = 12) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []
    try:
        from .communities import communities as _communities
        res = _communities(root, project_id=project_id, level=0)
        recs = sorted(res.get("communities") or [], key=lambda r: -len(r.get("files") or []))
        for r in recs[:top]:
            ks = (r.get("key_symbols") or [{}])[0]
            ev = f"{ks['path']}:{ks['line']}" if ks.get("path") else f"{(r.get('files') or [''])[0]}:1"
            facts.append(_fact(f"community:{r.get('name')}", f"{r.get('name')}: {len(r.get('files') or [])} files",
                               ev, "inferred", value=len(r.get("files") or []),
                               purpose=_short(str(r.get("purpose") or ""), 120),
                               language=r.get("dominant_language") or ""))
    except Exception as exc:  # noqa: BLE001
        logger.debug("dossier.structure communities failed: %s", exc)
    if not facts:  # directory fallback
        counts: Dict[str, int] = {}
        first: Dict[str, str] = {}
        for p in files.code_files(include_tests=True):
            area = p.split("/")[0] if "/" in p else "(root)"
            counts[area] = counts.get(area, 0) + 1
            first.setdefault(area, p)
        for area, n in sorted(counts.items(), key=lambda kv: -kv[1])[:top]:
            facts.append(_fact(f"dir:{area}", f"{area}/: {n} code files", f"{first[area]}:1",
                               "inferred", value=n))
    return facts


_MAIN_RE = re.compile(r"""^if\s+__name__\s*==\s*['"]__main__['"]""")
_WEB_PY_RE = re.compile(r"\b(FastAPI|Flask|Starlette|Quart|Sanic)\(|uvicorn\.run\(|\bAPIRouter\(")
_WEB_JS_RE = re.compile(r"\bexpress\(\)|\bfastify\(|createServer\(|\bnew Koa\(|\bapp\.listen\(")
_CLI_RE = re.compile(r"argparse\.ArgumentParser\(|@click\.(?:command|group)|typer\.Typer\(")


def _toml(files: _Files, rel: str) -> Dict[str, Any]:
    try:
        import tomllib  # py3.11+
    except ImportError:  # pragma: no cover
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            return {}
    try:
        return tomllib.loads(files.text(rel))
    except Exception:  # noqa: BLE001
        return {}


def _package_jsons(files: _Files) -> List[str]:
    return [p for p in files.paths if p.endswith("package.json") and p.count("/") <= 2]


def _pkg(files: _Files, rel: str) -> Dict[str, Any]:
    try:
        data = json.loads(files.text(rel))
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _entry_points(root: str, project_id: str, files: _Files) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []
    for rel in files.code_files():
        if rel.endswith(".py"):
            for i, line, _m in files.grep(rel, _MAIN_RE):
                facts.append(_fact(f"main:{rel}", f"script entry (`__main__` guard) in {rel}", f"{rel}:{i}", "code"))
            for i, line, m in files.grep(rel, _WEB_PY_RE):
                facts.append(_fact(f"web:{rel}:{i}", f"web app/route module: `{_short(line, 90)}`", f"{rel}:{i}", "code"))
            for i, line, _m in files.grep(rel, _CLI_RE):
                facts.append(_fact(f"cli:{rel}:{i}", f"CLI parser: `{_short(line, 90)}`", f"{rel}:{i}", "code"))
        else:
            for i, line, _m in files.grep(rel, _WEB_JS_RE):
                facts.append(_fact(f"web:{rel}:{i}", f"JS server: `{_short(line, 90)}`", f"{rel}:{i}", "code"))
    for rel in _package_jsons(files):
        data, lines = _pkg(files, rel), files.lines(rel)
        for name, cmd in sorted((data.get("scripts") or {}).items()):
            facts.append(_fact(f"npm-script:{rel}:{name}", f"{rel} script `{name}`: {cmd}",
                               f"{rel}:{_find_line(lines, json.dumps(name))}", "config", value=str(cmd)))
        for field in ("main", "bin"):
            if data.get(field):
                facts.append(_fact(f"npm-{field}:{rel}", f"{rel} `{field}`: {data[field]}",
                                   f"{rel}:{_find_line(lines, json.dumps(field))}", "config",
                                   value=json.dumps(data[field])))
    for rel in files.find(r"(^|/)pyproject\.toml$"):
        data, lines = _toml(files, rel), files.lines(rel)
        scripts = dict((data.get("project") or {}).get("scripts") or {})
        scripts.update(((data.get("tool") or {}).get("poetry") or {}).get("scripts") or {})
        for name, target in sorted(scripts.items()):
            facts.append(_fact(f"py-script:{rel}:{name}", f"{rel} console script `{name}` -> {target}",
                               f"{rel}:{_find_line(lines, name)}", "config", value=str(target)))
    for rel in files.find(r"(^|/)setup\.py$"):
        for i, line, _m in files.grep(rel, re.compile(r"console_scripts")):
            facts.append(_fact(f"py-script:{rel}", f"{rel} declares console_scripts", f"{rel}:{i}", "config"))
    for rel in files.find(r"(^|/)Dockerfile[^/]*$"):
        for i, line, m in files.grep(rel, re.compile(r"^\s*(CMD|ENTRYPOINT|EXPOSE)\b")):
            facts.append(_fact(f"docker:{rel}:{m.group(1)}:{i}", f"{rel}: {_short(line, 120)}", f"{rel}:{i}", "config"))
    for rel in files.find(r"(^|/)(docker-)?compose[^/]*\.ya?ml$"):
        lines = files.lines(rel)
        in_services, indent = False, None
        for i, line in enumerate(lines, 1):
            if re.match(r"^services:\s*$", line):
                in_services = True
                continue
            if in_services:
                if line and not line.startswith(" "):
                    in_services = False
                    continue
                m = re.match(r"^(\s+)([A-Za-z0-9_.-]+):\s*$", line)
                if m and (indent is None or len(m.group(1)) == indent):
                    indent = len(m.group(1))
                    facts.append(_fact(f"compose:{rel}:{m.group(2)}", f"{rel} service `{m.group(2)}`", f"{rel}:{i}", "config"))
    try:
        arch = get_architecture(root, project_id=project_id)
        by_file: Dict[str, List[Dict[str, Any]]] = {}
        for r in arch.get("routes") or []:
            by_file.setdefault(r["path"], []).append(r)
        for path, rs in sorted(by_file.items()):
            facts.append(_fact(f"routes:{path}", f"HTTP route module {path}: {len(rs)} route(s), e.g. {rs[0]['route']}",
                               f"{path}:{rs[0]['line']}", "code", value=len(rs)))
    except Exception as exc:  # noqa: BLE001
        logger.debug("dossier.entry_points routes failed: %s", exc)
    return facts


_CMD_RE = re.compile(r"^\s*[$>]?\s*((?:pip3?|python3?|npm|npx|yarn|pnpm|make|docker|docker-compose|uvicorn|"
                     r"cargo|go|poetry|uv|pytest|node|bash|sh|\./)\b.*\S)\s*$")


def _build_run(files: _Files) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []
    for rel in _package_jsons(files):
        data, lines = _pkg(files, rel), files.lines(rel)
        for name, cmd in sorted((data.get("scripts") or {}).items()):
            if re.search(r"test|jest|vitest|mocha", name + " " + str(cmd)):
                continue
            facts.append(_fact(f"npm-script:{rel}:{name}", f"npm run {name}  ->  {cmd}",
                               f"{rel}:{_find_line(lines, json.dumps(name))}", "config",
                               quote=str(cmd)))
        if data.get("dependencies") or data.get("devDependencies"):
            facts.append(_fact(f"npm-deps:{rel}", f"{rel} declares {len(data.get('dependencies') or {})} dependencies "
                               f"and {len(data.get('devDependencies') or {})} devDependencies (install: npm install, conventional)",
                               f"{rel}:{_find_line(files.lines(rel), 'dependencies')}", "inferred"))
    for rel in files.find(r"(^|/)pyproject\.toml$"):
        data, lines = _toml(files, rel), files.lines(rel)
        if data.get("build-system"):
            facts.append(_fact(f"py-build:{rel}", f"{rel} build-system: {json.dumps(data['build-system'])[:120]}",
                               f"{rel}:{_find_line(lines, '[build-system]')}", "config"))
        deps = (data.get("project") or {}).get("dependencies") or []
        if deps:
            facts.append(_fact(f"py-deps:{rel}", f"{rel} declares {len(deps)} dependencies",
                               f"{rel}:{_find_line(lines, 'dependencies')}", "config"))
    for rel in files.find(r"(^|/)requirements[^/]*\.txt$"):
        n = sum(1 for ln in files.lines(rel) if ln.strip() and not ln.strip().startswith("#"))
        facts.append(_fact(f"py-reqs:{rel}", f"{rel} lists {n} requirement(s); `pip install -r {rel}` is the conventional install (not stated in code)",
                           f"{rel}:1", "inferred"))
    for rel in files.find(r"(^|/)Makefile$"):
        for i, line in enumerate(files.lines(rel), 1):
            m = re.match(r"^([A-Za-z0-9_.-]+):(?!=)", line)
            if m and not m.group(1).startswith("."):
                facts.append(_fact(f"make:{rel}:{m.group(1)}", f"make {m.group(1)}", f"{rel}:{i}", "config", quote=line.strip()))
    for rel in [p for p in files.paths if re.match(r"^README[^/]*\.md$", p, re.I)]:
        in_block, n = False, 0
        for i, line in enumerate(files.lines(rel), 1):
            if line.lstrip().startswith("```"):
                in_block = not in_block
                continue
            if in_block and n < 12:
                m = _CMD_RE.match(line)
                if m:
                    n += 1
                    facts.append(_fact(f"readme:{rel}:{i}", f"README command: {m.group(1)}", f"{rel}:{i}",
                                       "config", quote=m.group(1)))
    return facts


_PY_TEST_RE = re.compile(r"^\s*(?:async\s+)?def\s+test_\w+")
_JS_TEST_RE = re.compile(r"^\s*(?:it|test)(?:\.each\([^)]*\))?\(\s*['\"`]")


def _tests(files: _Files) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []
    per_area: Dict[str, List[Any]] = {}
    for rel in files.paths:
        if not _is_test_path(rel) or not rel.endswith(_CODE_EXT):
            continue
        rx = _PY_TEST_RE if rel.endswith(".py") else _JS_TEST_RE
        hits = [i for i, _l, _m in files.grep(rel, rx)]
        if not hits:
            continue
        parts = rel.split("/")
        area = "/".join(parts[:-1][:2]) or "(root)"
        slot = per_area.setdefault(area, [0, 0, f"{rel}:{hits[0]}"])
        slot[0] += len(hits)
        slot[1] += 1
    for area, (n, nfiles, ev) in sorted(per_area.items(), key=lambda kv: -kv[1][0]):
        facts.append(_fact(f"tests:{area}", f"{area}: {n} test(s) in {nfiles} file(s)", ev, "code", value=n))
    cfg = [("pytest.ini", None), ("tox.ini", None), ("conftest.py", None), ("setup.cfg", "[tool:pytest]"),
           ("pyproject.toml", "[tool.pytest"), ("jest.config", None), ("vitest.config", None), ("playwright.config", None)]
    for rel in files.paths:
        base = rel.rsplit("/", 1)[-1]
        for name, marker in cfg:
            if base.startswith(name) and (marker is None or marker in files.text(rel)):
                ln = 1 if marker is None else _find_line(files.lines(rel), marker)
                facts.append(_fact(f"runner:{rel}", f"test runner config: {rel}", f"{rel}:{ln}", "config"))
                break
    for rel in _package_jsons(files):
        lines = files.lines(rel)
        for name, cmd in sorted((_pkg(files, rel).get("scripts") or {}).items()):
            if re.search(r"test|jest|vitest|mocha", name + " " + str(cmd)):
                facts.append(_fact(f"npm-test:{rel}:{name}", f"npm run {name}  ->  {cmd}",
                                   f"{rel}:{_find_line(lines, json.dumps(name))}", "config", quote=str(cmd)))
    if per_area and not any(f["key"].startswith("npm-test") for f in facts):
        py = any(f["key"].startswith("tests:") for f in facts) and any(p.endswith(".py") and _is_test_path(p) for p in files.paths)
        if py:
            facts.append(_fact("test-command:pytest", "`python -m pytest` is the conventional runner for test_*.py files (not stated in the repo)",
                               next(iter(per_area.values()))[2], "inferred"))
    return facts


_ORM_RE = re.compile(r"declarative_base\(|class\s+\w+\((?:[\w.]*\.)?(?:Base|Model|SQLModel)\)|models\.Model\)|"
                     r"new\s+Schema\(|sequelize\.define\(|\bTable\(")
_DBCONN_RE = re.compile(r"sqlite3\.connect\(|create_engine\(|psycopg2?\.connect\(|MongoClient\(|redis\.(?:Redis|from_url)\(|createConnection\(|new Pool\(")


def _data(files: _Files) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []
    mig = [p for p in files.paths if re.search(r"(^|/)(migrations|alembic/versions|db/migrate)/", p)]
    if mig:
        facts.append(_fact("migrations", f"{len(mig)} migration file(s), first {mig[0]}", f"{mig[0]}:1", "code", value=len(mig)))
    for rel in [p for p in files.paths if p.endswith(".sql")][:15]:
        facts.append(_fact(f"sql:{rel}", f"SQL file {rel}", f"{rel}:1", "code"))
    for rel in [p for p in files.paths if p.endswith("schema.prisma")]:
        facts.append(_fact(f"prisma:{rel}", f"Prisma schema {rel}", f"{rel}:1", "config"))
    for rel in files.code_files():
        for i, line, _m in files.grep(rel, _ORM_RE):
            facts.append(_fact(f"orm:{rel}:{i}", f"ORM/model definition: `{_short(line, 90)}`", f"{rel}:{i}", "code"))
        for i, line, _m in files.grep(rel, _DBCONN_RE):
            facts.append(_fact(f"db:{rel}:{i}", f"database connection: `{_short(line, 90)}`", f"{rel}:{i}", "code"))
        for i, line, _m in files.grep(rel, re.compile(r"CREATE TABLE", re.I)):
            facts.append(_fact(f"ddl:{rel}:{i}", f"inline DDL: `{_short(line, 90)}`", f"{rel}:{i}", "code"))
    dirs: Dict[str, List[str]] = {}
    for rel in files.paths:
        top = rel.split("/")[0]
        if "/" in rel and top.lower() in ("data", "fixtures", "seeds", "datasets", "db"):
            dirs.setdefault(top, []).append(rel)
    for top, rels in sorted(dirs.items()):
        facts.append(_fact(f"datadir:{top}", f"data folder {top}/ with {len(rels)} file(s) (purpose inferred from the name)",
                           f"{rels[0]}:1", "inferred", value=len(rels)))
    return facts[:80]


_ENV_RES = [
    (re.compile(r"""os\.environ\[\s*['"]([A-Z0-9_]+)['"]\s*\]"""), False),
    (re.compile(r"""os\.environ\.get\(\s*['"]([A-Z0-9_]+)['"]\s*(,)?"""), True),
    (re.compile(r"""os\.getenv\(\s*['"]([A-Z0-9_]+)['"]\s*(,)?"""), True),
    (re.compile(r"""process\.env\.([A-Z0-9_]+)(\s*(?:\|\||\?\?))?"""), True),
    (re.compile(r"""process\.env\[\s*['"]([A-Z0-9_]+)['"]\s*\]"""), False),
]
_URL_RE = re.compile(r"""https?://([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)""")
_PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([A-Za-z_][\w]*)[\w.]*\s+import|import\s+([A-Za-z_][\w]*))")
_JS_IMPORT_RE = re.compile(r"""(?:from\s+|require\(\s*|import\s+)['"]((?:@[\w.-]+/)?[A-Za-z][\w.-]*)""")
_STDLIB = set(getattr(sys, "stdlib_module_names", ())) | {"__future__"}


def _integrations(files: _Files) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []
    hosts: Dict[str, List[Any]] = {}
    envs: Dict[str, List[Any]] = {}
    sdks: Dict[str, str] = {}
    local = {p.split("/")[0].rsplit(".", 1)[0] for p in files.paths}
    for rel in files.code_files():
        for i, line in enumerate(files.lines(rel), 1):
            for rx, may_default in _ENV_RES:
                for m in rx.finditer(line):
                    has_default = bool(may_default and m.lastindex and m.lastindex >= 2 and m.group(2))
                    slot = envs.setdefault(m.group(1), [f"{rel}:{i}", has_default])
                    slot[1] = slot[1] or has_default
            if "http" in line:
                for m in _URL_RE.finditer(line):
                    host = m.group(1).lower()
                    if any(n in host or n in line for n in _NOISE_HOSTS if n.endswith("/")) or \
                            any(n in host for n in _NOISE_HOSTS if not n.endswith("/")):
                        continue
                    slot = hosts.setdefault(host, [f"{rel}:{i}", False, 0])
                    slot[2] += 1
                    slot[1] = slot[1] or bool(_CLIENT_CALL_RE.search(line))
            if rel.endswith(".py"):
                m = _PY_IMPORT_RE.match(line)
                if m:
                    top = m.group(1) or m.group(2)
                    if top and top not in _STDLIB and top not in local and top not in sdks:
                        sdks[top] = f"{rel}:{i}"
            else:
                for m in _JS_IMPORT_RE.finditer(line):
                    if m.group(1) not in sdks and not line.lstrip().startswith("//"):
                        sdks[m.group(1)] = f"{rel}:{i}"
    for host, (ev, client, n) in sorted(hosts.items()):
        facts.append(_fact(f"host:{host}", f"outbound host {host} ({n} literal reference(s))", ev,
                           "code" if client else "inferred", value=n))
    for name, (ev, has_default) in sorted(envs.items()):
        facts.append(_fact(f"env:{name}", f"env var {name} ({'with default' if has_default else 'no default in code'})",
                           ev, "code", has_default=has_default))
    for name, ev in sorted(sdks.items())[:60]:
        facts.append(_fact(f"sdk:{name}", f"external package imported: {name}", ev, "code"))
    return facts


_ROUTE_DEF_RES = [
    re.compile(r"<Route\b[^>]*\bpath=['\"{`]+([^'\"`}]+)"),
    re.compile(r"""\bpath:\s*['"`](/[^'"`]*)['"`]"""),
    re.compile(r"""router\.(?:push|replace)\(\s*['"`](/[^'"`]*)"""),
]
_ROUTER_HINT_RE = re.compile(r"react-router|createBrowserRouter|vue-router|VueRouter|createRouter|<Routes|@angular/router|svelte-routing")


def _screens(files: _Files) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []
    for rel in [p for p in files.paths if p.endswith((".html", ".htm"))][:40]:
        title_ln = next(((i, ln) for i, ln, _m in files.grep(rel, re.compile(r"<title>", re.I))), None)
        if title_ln:
            t = re.sub(r"<[^>]+>", "", title_ln[1]).strip()
            facts.append(_fact(f"page:{rel}", f"HTML page {rel}: \"{_short(t, 80)}\"", f"{rel}:{title_ln[0]}", "code"))
        else:
            facts.append(_fact(f"page:{rel}", f"HTML page {rel}", f"{rel}:1", "code"))
    for rel in files.code_files():
        if rel.endswith(".py"):
            continue
        if not _ROUTER_HINT_RE.search(files.text(rel)) and not re.search(r"routes?\b", rel, re.I):
            continue
        for i, line in enumerate(files.lines(rel), 1):
            for rx in _ROUTE_DEF_RES:
                m = rx.search(line)
                if m:
                    facts.append(_fact(f"route:{rel}:{m.group(1)}", f"frontend route {m.group(1)}", f"{rel}:{i}", "code"))
                    break
    for rel in files.paths:
        if re.search(r"(^|/)(app|pages)/.*(page|index)\.(tsx|jsx|js|vue)$", rel):
            facts.append(_fact(f"filepage:{rel}", f"file-based page {rel} (route derived from the path by convention)",
                               f"{rel}:1", "inferred"))
    return facts[:120]


def _main_flows(root: str, project_id: str, top_n: int) -> List[Dict[str, Any]]:
    facts: List[Dict[str, Any]] = []
    try:
        from .flows import flows as _flows
        res = _flows(root, project_id=project_id, limit=top_n)
    except Exception as exc:  # noqa: BLE001
        logger.debug("dossier.main_flows failed: %s", exc)
        return facts
    for f in res.get("flows") or []:
        sym = _resolve_symbol(f["entry_symbol"], root=root, project_id=project_id) or {}
        ev = f"{sym.get('path') or (f.get('files') or [''])[0]}:{sym.get('start_line') or 1}"
        facts.append(_fact(f"flow:{f['entry_symbol']}", f"{f['name']} ({f['size']} step(s), {f['entry_reason']})", ev,
                           "code", value=f.get("criticality"), criticality=f.get("criticality"),
                           flow_id=f.get("id")))
    return facts


_DYN_RES = [
    (re.compile(r"importlib\.import_module\("), "dynamic import via importlib"),
    (re.compile(r"\b__import__\("), "dynamic import via __import__"),
    (re.compile(r"\b(?:exec|eval)\("), "exec/eval of runtime strings"),
    (re.compile(r"""\brequire\(\s*[^'"\s)]"""), "require() with a non-literal argument"),
    (re.compile(r"""\bimport\(\s*[^'"\s)]"""), "import() with a non-literal argument"),
    (re.compile(r"\.add_api_route\(|\.add_url_rule\(|\.add_route\("), "routes registered programmatically"),
    (re.compile(r"(?<![\w])entry_points\(|pkg_resources\.iter_entry_points"), "plugins discovered via entry points"),
]


def _uncertainties(root: str, project_id: str, files: _Files,
                   sections: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    readme = next((p for p in files.paths if re.match(r"^README", p, re.I)), "(no README)")
    seen_kind: Dict[str, int] = {}
    for rel in files.code_files():
        for i, line in enumerate(files.lines(rel), 1):
            if line.lstrip().startswith(("#", "//")) or "re.compile" in line:
                continue
            for rx, why in _DYN_RES:
                if rx.search(line) and seen_kind.get(why, 0) < 6:
                    seen_kind[why] = seen_kind.get(why, 0) + 1
                    out.append(_fact(f"dynamic:{rel}:{i}", f"{why}: what it reaches is not determinable statically",
                                     f"{rel}:{i}", "inferred", quote=_short(line, 120)))
    mains = [f for f in sections["entry_points"] if f["key"].startswith("main:")]
    web = [f for f in sections["entry_points"] if f["key"].startswith("web:")]
    if len(mains) + len(web) > 1:
        ev = (mains or web)[0]["evidence"]
        out.append(_fact("ambiguous-entry", f"{len(mains)} script entry point(s) and {len(web)} web-app marker(s): "
                         "which one is the primary way to run this repo is not determinable statically", ev, "inferred"))
    stacks = [r for r in files.paths if r in ("package.json", "pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod")]
    if len({s.split(".")[0] for s in stacks if s != "requirements.txt"} | ({"py"} if "requirements.txt" in stacks else set())) > 1:
        out.append(_fact("multi-stack", "several ecosystems declared at the root (" + ", ".join(stacks) +
                         "); how they fit together is not stated", f"{stacks[0]}:1", "inferred"))
    for f in sections["integrations"]:
        if f["key"].startswith("env:") and not f.get("has_default"):
            out.append(_fact(f"env-required:{f['key'][4:]}", f"{f['key'][4:]} is read with no default: the value needed to run is unknown",
                             f["evidence"], "inferred"))
    if not sections["build_run"]:
        out.append(_fact("no-build", "no build/run command found in package.json, Makefile or README code blocks", f"{readme}:0", "inferred"))
    if not sections["test"]:
        out.append(_fact("no-tests", "no test files recognised", f"{readme}:0", "inferred"))
    try:
        arch = get_architecture(root, project_id=project_id)
        lex = (arch.get("by_certainty") or {}).get("lexical", 0)
        langs = [l for l in (arch.get("languages") or {}) if l not in ("python", "py")]
        if lex:
            out.append(_fact("lexical-edges", f"{lex} call edge(s) are lexical matches, not resolved calls; flows built on them may be wrong",
                             "(code graph)", "inferred"))
        if langs:
            out.append(_fact("lexical-langs", "languages other than Python are indexed lexically only: " + ", ".join(sorted(langs)),
                             "(code graph)", "inferred"))
    except Exception:  # noqa: BLE001
        pass
    return out


# ── persistence ──────────────────────────────────────────────────────────

def _save(root: str, project_id: str, revision: str, data: Dict[str, Any]) -> None:
    try:
        with ce_store.db() as conn:
            conn.execute("INSERT OR REPLACE INTO code_graph_dossiers "
                         "(workspace, project_id, revision, data, created_at) VALUES (?,?,?,?,?)",
                         (root, project_id, revision, ce_store.dumps(data), data.get("created_at", "")))
    except (ce_store.ContextStoreError, sqlite3.Error) as exc:
        logger.warning("code_graph.dossier: save failed: %s", exc)


def _load(root: str, project_id: str, revision: str) -> Optional[Dict[str, Any]]:
    try:
        with ce_store.db() as conn:
            row = conn.execute("SELECT data FROM code_graph_dossiers WHERE workspace=? AND project_id=? AND revision=?",
                               (root, project_id, revision)).fetchone()
    except (ce_store.ContextStoreError, sqlite3.Error):
        return None
    if not row:
        return None
    data = ce_store.loads(row["data"], None)
    return data if isinstance(data, dict) and data.get("version") == DOSSIER_VERSION else None


def _list_revisions(root: str, project_id: str) -> List[Dict[str, str]]:
    try:
        with ce_store.db() as conn:
            rows = conn.execute("SELECT revision, created_at FROM code_graph_dossiers WHERE workspace=? AND project_id=? "
                                "ORDER BY created_at DESC, rowid DESC", (root, project_id)).fetchall()
    except (ce_store.ContextStoreError, sqlite3.Error):
        return []
    return [{"revision": r["revision"], "created_at": r["created_at"]} for r in rows]


def _find_revision(root: str, project_id: str, wanted: str) -> Optional[str]:
    revs = [r["revision"] for r in _list_revisions(root, project_id)]
    if wanted in revs:
        return wanted
    cands = [r for r in revs if r.startswith(wanted)]
    return cands[0] if len(cands) >= 1 else None


# ── rendering ────────────────────────────────────────────────────────────

def _render(d: Dict[str, Any], per_section: int = 6) -> str:
    lines = [f"Dossier of {d['root']} @ {d['revision']} ({d['revision_kind']}), {d['files']} files, {d['symbols']} symbols"]
    for sec in _SECTIONS:
        facts = d["sections"].get(sec) or []
        lines.append(f"## {sec} ({len(facts)})")
        for f in facts[:per_section]:
            lines.append(f"- [{f['certainty']}] {f['fact']}  ({f['evidence']})")
        if len(facts) > per_section:
            lines.append(f"  … {len(facts) - per_section} more in the structured result")
    return "\n".join(lines)


# ── public API ───────────────────────────────────────────────────────────

def dossier(root: str = "", *, project_id: str = "", refresh: bool = False, top_flows: int = 8,
            output_chars: int = 6000) -> Dict[str, Any]:
    """Intake dossier of a repository at its current git revision. Cached per
    revision; `refresh=True` recomputes."""
    try:
        resolved = _root(root)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    files = _Files(resolved)
    revision, kind = _revision(resolved, files.paths)
    if not refresh:
        cached = _load(resolved, project_id, revision)
        if cached:
            return {"output": _clip(_render(cached), output_chars), "exit_code": 0, "root": resolved,
                    "revision": revision, "cached": True, "dossier": cached}
    try:
        code_index.refresh(resolved, project_id=project_id, budget_files=_INDEX_BUDGET_FILES)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.dossier: index refresh failed: %s", exc)
    status = code_index.status(resolved, project_id=project_id)

    sections: Dict[str, List[Dict[str, Any]]] = {s: [] for s in _SECTIONS}
    builders = (
        ("structure", lambda: _structure(resolved, project_id, files)),
        ("entry_points", lambda: _entry_points(resolved, project_id, files)),
        ("build_run", lambda: _build_run(files)),
        ("test", lambda: _tests(files)),
        ("data", lambda: _data(files)),
        ("integrations", lambda: _integrations(files)),
        ("screens", lambda: _screens(files)),
        ("main_flows", lambda: _main_flows(resolved, project_id, top_flows)),
    )
    for name, fn in builders:
        try:
            sections[name] = fn()
        except Exception as exc:  # noqa: BLE001
            logger.warning("code_graph.dossier: section %s failed: %s", name, exc)
            sections[name] = []
            sections["uncertainties"].append(_fact(f"section-failed:{name}", f"section {name} could not be computed: {exc}",
                                                   "(dossier)", "inferred"))
    sections["uncertainties"].extend(_uncertainties(resolved, project_id, files, sections))
    data = {"version": DOSSIER_VERSION, "root": resolved, "revision": revision, "revision_kind": kind,
            "created_at": ce_store.now_iso(), "files": len(files.paths),
            "symbols": status.get("symbols", 0), "sections": sections}
    _save(resolved, project_id, revision, data)
    return {"output": _clip(_render(data), output_chars), "exit_code": 0, "root": resolved,
            "revision": revision, "cached": False, "dossier": data}


def dossier_diff(root: str = "", *, project_id: str = "", from_revision: str = "", to_revision: str = "",
                 output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    """What changed in the dossier between two revisions. `to_revision` empty
    means the current one (computed if needed); `from_revision` empty means the
    most recent stored dossier of another revision."""
    try:
        resolved = _root(root)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    if to_revision:
        to_rev = _find_revision(resolved, project_id, to_revision)
        if not to_rev:
            return {"error": f"no stored dossier for revision {to_revision!r}", "exit_code": 1, "root": resolved}
        new = _load(resolved, project_id, to_rev)
    else:
        cur = dossier(resolved, project_id=project_id)
        if cur.get("exit_code") != 0:
            return cur
        new, to_rev = cur["dossier"], cur["revision"]
    if from_revision:
        from_rev = _find_revision(resolved, project_id, from_revision)
    else:
        from_rev = next((r["revision"] for r in _list_revisions(resolved, project_id) if r["revision"] != to_rev), None)
    if not from_rev or from_rev == to_rev:
        return {"error": "no earlier dossier to compare against; run dossier on another revision first",
                "exit_code": 1, "root": resolved, "to_revision": to_rev}
    old = _load(resolved, project_id, from_rev)
    if not old or not new:
        return {"error": "stored dossier missing or from an older format", "exit_code": 1, "root": resolved}
    changes: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    total = 0
    for sec in _SECTIONS:
        o = {f["key"]: f for f in old["sections"].get(sec) or []}
        n = {f["key"]: f for f in new["sections"].get(sec) or []}
        added = [n[k] for k in n if k not in o]
        removed = [o[k] for k in o if k not in n]
        changed = [{"key": k, "before": o[k]["fact"], "after": n[k]["fact"], "evidence": n[k]["evidence"],
                    "certainty": n[k]["certainty"]}
                   for k in n if k in o and (o[k]["fact"] != n[k]["fact"] or o[k].get("value") != n[k].get("value"))]
        if added or removed or changed:
            changes[sec] = {"added": added, "removed": removed, "changed": changed}
            total += len(added) + len(removed) + len(changed)
    lines = [f"Dossier diff {from_rev} -> {to_rev}: {total} change(s)"]
    for sec, c in changes.items():
        lines.append(f"## {sec}: +{len(c['added'])} -{len(c['removed'])} ~{len(c['changed'])}")
        lines += [f"  + {f['fact']} ({f['evidence']})" for f in c["added"][:6]]
        lines += [f"  - {f['fact']} ({f['evidence']})" for f in c["removed"][:6]]
        lines += [f"  ~ {x['before']}  =>  {x['after']} ({x['evidence']})" for x in c["changed"][:6]]
    return {"output": _clip("\n".join(lines), output_chars), "exit_code": 0, "root": resolved,
            "from_revision": from_rev, "to_revision": to_rev, "total": total, "changes": changes}


_STOP = set("""the a an of to in on for and or how where what which who is are be do does did i we you it this that with
from by at as can could should would change changed add adding fix where's el la los las un una de del en y o que
como donde dónde cual cuál se es son al por para con sin hay quiero cambiar modificar añadir arreglar""".split())


def locate(root: str = "", question: str = "", *, project_id: str = "", limit: int = 8,
           output_chars: int = DEFAULT_OUTPUT_CHARS) -> Dict[str, Any]:
    """"Where do I change X": candidate symbols for a plain-language question,
    from lexical search per keyword plus the semantic lane when an embedder is
    available, each with the execution flows and tests that touch it. These are
    candidates, ranked by match strength -- never a verified answer."""
    from .query import impact as _impact
    try:
        resolved = _root(root)
    except ValueError as exc:
        return {"error": str(exc), "exit_code": 1}
    q = str(question or "").strip()
    if not q:
        return {"error": "locate: a question is required", "exit_code": 1, "root": resolved}
    try:
        cap = max(1, min(int(limit or 8), 25))
    except (TypeError, ValueError):
        cap = 8
    try:
        code_index.refresh(resolved, project_id=project_id, budget_files=_INDEX_BUDGET_FILES)
    except Exception as exc:  # noqa: BLE001
        logger.warning("code_graph.locate: index refresh failed: %s", exc)
    tokens: List[str] = []
    for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", q):
        low = t.lower()
        if low not in _STOP and low not in tokens:
            tokens.append(low)
    tokens = tokens[:6]
    cands: Dict[str, Dict[str, Any]] = {}

    def add(hits: Sequence[Dict[str, Any]], lane: str, weight: float) -> None:
        for rank, h in enumerate(hits):
            sid = str(h.get("id") or "")
            if not sid:
                continue
            c = cands.setdefault(sid, {"hit": h, "score": 0.0, "matched_by": []})
            c["score"] += weight / (1 + rank)
            if lane not in c["matched_by"]:
                c["matched_by"].append(lane)

    semantic = False
    try:
        add(code_index.search(q, workspace=resolved, project_id=project_id, k=15), "lexical:question", 1.0)
        for t in tokens:
            add(code_index.search(t, workspace=resolved, project_id=project_id, k=8), f"lexical:{t}", 0.6)
        from .semantic import _embedder
        emb = _embedder()
        if emb is not None:
            semantic = True
            add(code_index.search(q, workspace=resolved, project_id=project_id, k=15, embedder=emb), "semantic", 1.0)
    except Exception as exc:  # noqa: BLE001
        logger.debug("code_graph.locate search failed: %s", exc)
    ranked = sorted(cands.values(), key=lambda c: (-c["score"], c["hit"].get("path", "")))
    prod = [c for c in ranked if not _is_test_path(str(c["hit"].get("path")))]
    ranked = (prod or ranked)[:cap]
    out: List[Dict[str, Any]] = []
    for c in ranked:
        h = c["hit"]
        entry = {"label": "candidate", "symbol": h.get("qualname"), "kind": h.get("kind"),
                 "path": h.get("path"), "start_line": h.get("start_line"), "end_line": h.get("end_line"),
                 "signature": h.get("signature") or "", "score": round(c["score"], 3),
                 "matched_by": c["matched_by"], "flows": [], "tests": []}
        if len(out) < 5:
            try:
                imp = _impact(str(h.get("qualname")), workspace=resolved, project_id=project_id, depth=2,
                              include_history=False, include_risk=False, output_chars=500)
                entry["flows"] = [{"id": f["id"], "name": f["name"], "criticality": f["criticality"]}
                                  for f in (imp.get("affected_flows") or [])[:3]]
                entry["tests"] = (imp.get("affected_tests") or [])[:5]
            except Exception as exc:  # noqa: BLE001
                logger.debug("code_graph.locate impact failed: %s", exc)
        out.append(entry)
    lines = [f"Candidates for {q!r} (ranked by match, not verified):"]
    for e in out:
        lines.append(f"  {e['path']}:{e['start_line']}  {e['kind']} {e['symbol']}  [{', '.join(e['matched_by'][:3])}]")
        for f in e["flows"]:
            lines.append(f"      flow {f['name']} (criticality {f['criticality']:.2f})")
        if e["tests"]:
            lines.append("      tests: " + ", ".join(e["tests"][:3]))
    if not out:
        lines.append("  no candidate found")
    return {"output": _clip("\n".join(lines), output_chars), "exit_code": 0, "root": resolved,
            "question": q, "semantic": semantic, "tokens": tokens, "candidates": out,
            "note": "candidates only: ranked by name/path/text match; confirm by reading the snippet"}


__all__ = ["dossier", "dossier_diff", "locate", "DOSSIER_VERSION"]
