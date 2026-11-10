"""src/loop_arms.py -- experiment arms and the improvement loop on top of a Loop Card.

A Loop Card (`src.loop_card`) says what number tells whether something got better and
how much that number moves on its own. This module uses it to decide between changes:

* **Arms** (`run_arms`). Several variants -- each a command, a folder, or both -- are
  measured against a *control* that does the same work (the card's own command in the
  card's folder), in paired, interleaved rounds: every round runs the control and every
  arm once, in a shuffled order, so drift (a warm cache, a busy GPU) hits all of them
  alike. Optionally a second, identical control (A/A) measures the noise floor. Each arm
  then gets a verdict from `arm_stats`, gates in this order:

    1. reliability -- an arm that fails more rounds than the control: two or more extra
       failures DROP, one INCONCLUSIVE;
    2. data -- fewer complete pairs than `min_pairs`: INCONCLUSIVE;
    3. effect -- KEEP when it wins every pair, the median paired improvement reaches the
       keep bar and exceeds the A/A noise, and the bootstrap 95 % interval of that median
       excludes zero; MINOR when the interval excludes zero but the bar or the every-round
       rule is missed; DROP when the median is not an improvement or the interval is
       entirely on the wrong side; INCONCLUSIVE otherwise.

  The paired gates follow the keep/kill statistics of github/lever's experiment-arms
  skill (arm_stats.py, MIT).

* **Improvement loop** (`start_improve`). Measure, propose, measure, keep: a model writes
  N different hypotheses for the card's goal (rejected ideas from the ledger are shown
  so they are not retried), N fresh workers each rewrite the named files for ONE
  hypothesis, every proposal becomes an isolated alternative (`src.alternatives`: a git
  worktree, or a copy of a plain folder) next to an untouched control alternative, an
  optional guard command (the tests) must pass in each, and the arms are measured in
  paired rounds. The best KEEP is applied to the real folder with the three-way merge
  of `alternatives.apply_alternative` (a manual edit made meanwhile is never lost) and
  measured again there; with `iterations` > 1 the loop starts over from the new code.
  Every arm, kept or dropped, is written to the card's ledger with its numbers.

Jobs run as asyncio tasks of the process that started them; their state is a JSON file
under DATA_DIR/loop_cards/<owner>/_improve/, so any process can read it.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import random
import re
import secrets
import shutil
import statistics as st
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from src import constants, loop_card
from src.loop_card import LoopError

MAX_ARMS = 8
MAX_ROUNDS = 30
DEFAULT_ROUNDS = 6
DEFAULT_MIN_PAIRS = 6
BOOT = 2000
MAX_PROPOSALS = 6
MAX_FILES = 6
MAX_FILE_BYTES = 80_000
MAX_SNAPSHOT_BYTES = 300 * 1024 * 1024
JOB_ID_RE = re.compile(r"^imp-\d{8}-\d{6}-[0-9a-f]{6}$")

_TASKS: Dict[str, asyncio.Task] = {}
_CANCEL: set = set()


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
def _boot_ci(xs: List[float], boot: int = BOOT) -> Tuple[float, float]:
    if len(xs) < 2:
        return float("nan"), float("nan")
    rnd = random.Random(0)
    meds = sorted(st.median(rnd.choices(xs, k=len(xs))) for _ in range(boot))
    return meds[int(0.025 * boot)], meds[int(0.975 * boot) - 1]


def _mann_whitney_p(a: List[float], b: List[float]) -> float:
    if not a or not b:
        return float("nan")
    allv = sorted((v, i) for i, v in enumerate(a + b))
    ranks = [0.0] * len(allv)
    i = 0
    while i < len(allv):
        j = i
        while j + 1 < len(allv) and allv[j + 1][0] == allv[i][0]:
            j += 1
        for k in range(i, j + 1):
            ranks[allv[k][1]] = (i + j) / 2 + 1
        i = j + 1
    n1, n2 = len(a), len(b)
    u = sum(ranks[:n1]) - n1 * (n1 + 1) / 2
    mu, sd = n1 * n2 / 2, math.sqrt(n1 * n2 * (n1 + n2 + 1) / 12)
    if sd == 0:
        return 1.0
    z = (abs(u - mu) - 0.5) / sd
    return max(0.0, min(1.0, math.erfc(z / math.sqrt(2))))


def _num(x: Any) -> Optional[float]:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else x


def arm_stats(samples: List[Mapping[str, Any]], control: str, *, aa: Optional[str] = None,
              higher_is_better: bool = False, bar: float = 0.0, bar_pct: float = 0.0,
              min_pairs: int = DEFAULT_MIN_PAIRS) -> Dict[str, Any]:
    """Verdict per arm from paired samples [{round, arm, value, valid}] (see module doc)."""
    by: Dict[str, Dict[int, List[float]]] = {}
    raw: Dict[str, List[float]] = {}
    seen: Dict[str, set] = {}
    for r in samples:
        arm, rnd = str(r["arm"]), int(r["round"])
        seen.setdefault(arm, set()).add(rnd)
        if r.get("valid", True) is False or r.get("value") is None:
            continue
        by.setdefault(arm, {}).setdefault(rnd, []).append(float(r["value"]))
        raw.setdefault(arm, []).append(float(r["value"]))
    if control not in by:
        return {"error": f"the control ({control}) has no valid run, so nothing can be compared"}

    def improvement(c: float, t: float) -> float:
        return t - c if higher_is_better else c - t

    def paired(arm: str) -> List[float]:
        rounds = sorted(set(by.get(arm, {})) & set(by[control]))
        return [improvement(st.median(by[control][k]), st.median(by[arm][k])) for k in rounds]

    def failures(arm: str, rounds: List[int]) -> int:
        return sum(1 for k in rounds if k not in by.get(arm, {}))

    ctrl_med = st.median(raw[control])
    eff_bar = max(float(bar or 0), abs(ctrl_med) * float(bar_pct or 0) / 100)
    noise = 0.0
    out: Dict[str, Any] = {"control": control, "control_median": ctrl_med, "control_n": len(raw[control]),
                           "higher_is_better": higher_is_better, "bar": eff_bar, "min_pairs": min_pairs, "arms": []}
    warnings: List[str] = []
    if aa:
        aa_d = paired(aa)
        noise = max((abs(x) for x in aa_d), default=0.0)
        out["aa"] = {"arm": aa, "pairs": len(aa_d), "noise_floor": noise, "deltas": aa_d}
        if eff_bar and eff_bar <= noise:
            warnings.append(f"the keep bar {eff_bar:.4g} is inside the A/A noise {noise:.4g}: add rounds or "
                            "steady the measurement before believing a KEEP")
    for arm in sorted(seen):
        if arm in (control, aa):
            continue
        attempted = sorted(seen[arm] | seen[control])
        tf, cf = failures(arm, attempted), failures(control, attempted)
        d = paired(arm)
        row: Dict[str, Any] = {"arm": arm, "pairs": len(d), "failed": tf, "control_failed": cf,
                               "median": st.median(raw[arm]) if raw.get(arm) else None}
        verdict, why = None, ""
        if tf - cf >= 2:
            verdict, why = "DROP", f"failed {tf} of {len(attempted)} rounds, the control {cf}"
        elif tf > cf:
            verdict, why = "INCONCLUSIVE", f"failed {tf} of {len(attempted)} rounds, the control {cf}; add rounds"
        elif len(d) < min_pairs:
            verdict, why = "INCONCLUSIVE", f"needs {min_pairs} complete pairs, has {len(d)}"
        if d:
            wins = sum(x > 0 for x in d)
            m = st.median(d)
            lo, hi = _boot_ci(d)
            row.update({"wins": wins, "median_delta": m, "delta_pct": (100 * m / abs(ctrl_med)) if ctrl_med else None,
                        "ci_low": _num(lo), "ci_high": _num(hi), "p_value": _num(_mann_whitney_p(raw[arm], raw[control]))})
            if verdict is None:
                if m <= 0 or (not math.isnan(hi) and hi <= 0):
                    verdict, why = "DROP", "not better than the control"
                elif wins == len(d) and m >= eff_bar and m > noise and lo > 0:
                    verdict, why = "KEEP", "won every pair, reached the keep bar and the interval excludes zero"
                elif lo > 0:
                    verdict, why = "MINOR", "a real improvement that misses the keep bar or lost a pair"
                else:
                    verdict, why = "INCONCLUSIVE", "inside the noise; add rounds or drop it"
        elif verdict is None:
            verdict, why = "INCONCLUSIVE", "no complete pair"
        row.update({"verdict": verdict, "why": why})
        out["arms"].append(row)
    out["warnings"] = warnings
    return out


# ---------------------------------------------------------------------------
# paired measurement
# ---------------------------------------------------------------------------
def _measure_once(runner: loop_card.Runner, command: str, cwd: Optional[str], extract: Mapping[str, Any],
                  timeout_s: float) -> Tuple[Optional[float], str]:
    try:
        code, output, seconds = runner(command, cwd, timeout_s)
    except LoopError as exc:
        return None, exc.detail
    if code != 0:
        return None, f"exit code {code}: {output.strip()[-200:]}"
    try:
        return loop_card.read_metric(extract, output, seconds), ""
    except LoopError as exc:
        return None, exc.detail


def measure_rounds(variants: List[Dict[str, Any]], extract: Mapping[str, Any], rounds: int, timeout_s: float,
                   runner: Optional[loop_card.Runner] = None, seed: int = 0,
                   should_stop: Optional[Callable[[], bool]] = None,
                   progress: Optional[Callable[[int, int], None]] = None) -> List[Dict[str, Any]]:
    """Run every variant {name, command, cwd} once per round, in a shuffled order per round."""
    runner = runner or loop_card.default_runner
    rnd = random.Random(seed)
    samples: List[Dict[str, Any]] = []
    for r in range(1, rounds + 1):
        order = list(variants)
        rnd.shuffle(order)
        for v in order:
            if should_stop and should_stop():
                return samples
            value, error = _measure_once(runner, v["command"], v.get("cwd"), extract, timeout_s)
            row = {"round": r, "arm": v["name"], "value": value, "valid": value is not None}
            if error:
                row["error"] = error[:300]
            samples.append(row)
        if progress:
            progress(r, rounds)
    return samples


def _int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _keep_bar(card: Mapping[str, Any]) -> Tuple[float, float]:
    kb = card.get("keep_bar") or {}
    return float(kb.get("abs") or 0), float(kb.get("pct") or 0)


def _ledger_rows(card: Mapping[str, Any], stats: Mapping[str, Any], labels: Mapping[str, str], source: str,
                 n_pairs_note: str = "") -> List[Dict[str, Any]]:
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    rows = []
    for a in stats.get("arms", []):
        rows.append({"at": now, "source": source, "arm": a["arm"], "change": labels.get(a["arm"], a["arm"]),
                     "control_median": stats.get("control_median"), "arm_median": a.get("median"),
                     "delta": a.get("median_delta"), "delta_pct": a.get("delta_pct"), "pairs": a.get("pairs"),
                     "wins": a.get("wins"), "ci": [a.get("ci_low"), a.get("ci_high")],
                     "noise_floor": (stats.get("aa") or {}).get("noise_floor"),
                     "verdict": a["verdict"], "why": a.get("why", "") + n_pairs_note})
    return rows


def append_ledger(owner: str, card_id: str, rows: List[Dict[str, Any]], project: Optional[str] = None) -> None:
    if not rows:
        return
    card = loop_card.get(owner, card_id, project)
    card.setdefault("ledger", []).extend(rows)
    card["ledger"] = card["ledger"][-loop_card.MAX_LEDGER:]
    card["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    loop_card._save(owner, card)


def run_arms(owner: str, args: Mapping[str, Any], runner: Optional[loop_card.Runner] = None) -> Dict[str, Any]:
    """Measure arms [{name, command?, cwd?}] against the card's control in paired rounds."""
    card_id = str(args.get("id") or args.get("card_id") or "")
    card = loop_card.get(owner, card_id, args.get("project") or None)
    raw_arms = args.get("arms")
    if isinstance(raw_arms, str):
        raw_arms = [{"name": f"arm{i + 1}", "command": line.strip()} for i, line in enumerate(raw_arms.splitlines()) if line.strip()]
    if not isinstance(raw_arms, list) or not raw_arms:
        raise LoopError("`arms` is required: a list of {name, command?, cwd?} (each needs a command or a cwd that differs)")
    if len(raw_arms) > MAX_ARMS:
        raise LoopError(f"at most {MAX_ARMS} arms per run")
    arms, labels, names = [], {}, set()
    for i, a in enumerate(raw_arms):
        a = a if isinstance(a, Mapping) else {"command": str(a)}
        name = re.sub(r"[^\w.-]+", "-", str(a.get("name") or f"arm{i + 1}")).strip("-")[:40] or f"arm{i + 1}"
        if name in names or name in ("control", "control-aa"):
            name = f"{name}-{i + 1}"
        names.add(name)
        command = str(a.get("command") or "").strip() or card["command"]
        cwd = str(a.get("cwd") or "").strip() or card.get("cwd")
        if cwd and not os.path.isdir(cwd):
            raise LoopError(f"arm {name}: `cwd` is not a folder: {cwd}")
        if command == card["command"] and (cwd or None) == (card.get("cwd") or None):
            raise LoopError(f"arm {name} is identical to the control; give it a different command or folder")
        arms.append({"name": name, "command": command, "cwd": cwd})
        labels[name] = str(a.get("label") or a.get("hypothesis") or (command if command != card["command"] else f"cwd={cwd}"))[:300]
    rounds = _int(args.get("rounds"), DEFAULT_ROUNDS, 2, MAX_ROUNDS)
    min_pairs = _int(args.get("min_pairs"), int(card.get("min_pairs") or min(DEFAULT_MIN_PAIRS, rounds)), 2, MAX_ROUNDS)
    aa = bool(args.get("aa", True))
    guard = str(args.get("guard_command") or card.get("guard_command") or "").strip()
    runner = runner or loop_card.default_runner
    timeout_s = float(card.get("timeout_s") or loop_card.DEFAULT_TIMEOUT_S)

    guard_fail: Dict[str, str] = {}
    if guard:
        for a in arms:
            try:
                code, out, _ = runner(guard, a["cwd"], timeout_s)
            except LoopError as exc:
                code, out = 1, exc.detail
            if code != 0:
                guard_fail[a["name"]] = out.strip()[-300:]
    measured = [a for a in arms if a["name"] not in guard_fail]
    variants = [{"name": "control", "command": card["command"], "cwd": card.get("cwd")}] + measured
    if aa:
        variants.append({"name": "control-aa", "command": card["command"], "cwd": card.get("cwd")})
    samples = measure_rounds(variants, card["extract"], rounds, timeout_s, runner, seed=int(args.get("seed") or 0))
    bar_abs, bar_pct = _keep_bar(card)
    stats = arm_stats(samples, "control", aa="control-aa" if aa else None, higher_is_better=card["direction"] == "up",
                      bar=float(args.get("keep_bar_abs") or bar_abs), bar_pct=float(args.get("keep_bar_pct") or bar_pct),
                      min_pairs=min_pairs)
    if stats.get("error"):
        raise LoopError(stats["error"], "loop.command_failed")
    for name, out in guard_fail.items():
        stats["arms"].append({"arm": name, "pairs": 0, "verdict": "DROP", "why": f"the guard command failed: {out}"})
    rows = _ledger_rows(card, stats, labels, "arms")
    if args.get("record", True):
        append_ledger(owner, card["id"], rows, card.get("project"))
    return {"id": card["id"], "goal": card["goal"], "rounds": rounds, "arms_spec": arms, "stats": stats,
            "samples": samples, "ledger_rows": rows, "keep": [a["arm"] for a in stats["arms"] if a["verdict"] == "KEEP"]}


# ---------------------------------------------------------------------------
# improvement loop: storage
# ---------------------------------------------------------------------------
def _job_dir(owner: str) -> Path:
    return Path(constants.DATA_DIR) / "loop_cards" / loop_card._safe(owner or "_") / "_improve"


def _job_path(owner: str, job_id: str) -> Path:
    if not JOB_ID_RE.match(job_id or ""):
        raise LoopError(f"no such improvement run: {job_id}", "loop.not_found")
    return _job_dir(owner) / f"{job_id}.json"


def _save_job(owner: str, job: Dict[str, Any]) -> None:
    p = _job_path(owner, job["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(job, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, p)


def get_job(owner: str, job_id: str) -> Dict[str, Any]:
    p = _job_path(owner, job_id)
    if not p.is_file():
        raise LoopError(f"no such improvement run: {job_id}", "loop.not_found")
    job = json.loads(p.read_text(encoding="utf-8"))
    if job.get("status") in ("queued", "running") and job_id not in _TASKS and \
            time.time() - float(job.get("heartbeat") or 0) > 900:
        job["status"] = "interrupted"
    return job


def list_jobs(owner: str, card_id: Optional[str] = None) -> List[Dict[str, Any]]:
    d = _job_dir(owner)
    rows = []
    if d.is_dir():
        for p in d.glob("imp-*.json"):
            try:
                job = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if card_id and job.get("card_id") != card_id:
                continue
            rows.append({k: job.get(k) for k in ("id", "card_id", "goal", "status", "phase", "created", "finished",
                                                 "winner", "applied", "iteration", "iterations", "error")})
    rows.sort(key=lambda r: r.get("created") or "", reverse=True)
    return rows


def delete_job(owner: str, job_id: str) -> Dict[str, Any]:
    job = get_job(owner, job_id)
    if job.get("status") in ("queued", "running") and job_id in _TASKS:
        raise LoopError("the run is still going; cancel it first")
    _cleanup_experiments(owner, job)
    _job_path(owner, job_id).unlink(missing_ok=True)
    return {"deleted": job_id}


def cancel_job(owner: str, job_id: str) -> Dict[str, Any]:
    job = get_job(owner, job_id)
    _CANCEL.add(job_id)
    task = _TASKS.get(job_id)
    if task is None and job.get("status") in ("queued", "running"):
        job["status"] = "cancelled"
        _save_job(owner, job)
    return {"id": job_id, "cancel_requested": True}


def _cleanup_experiments(owner: str, job: Mapping[str, Any]) -> None:
    from src import alternatives
    for it in job.get("history") or []:
        exp = it.get("experiment_id")
        if exp and not it.get("kept_experiment"):
            try:
                alternatives.delete_experiment(owner, exp)
            except Exception:  # noqa: BLE001 - already gone
                pass


# ---------------------------------------------------------------------------
# improvement loop: prompts
# ---------------------------------------------------------------------------
HYPOTHESES_SYSTEM = (
    "You plan performance and quality experiments. You get a goal, how it is measured and the source "
    "files that may change. Reply with ONLY a JSON array of distinct, concrete, single-idea hypotheses "
    "(strings, one sentence each). Each must be testable by editing only the given files, must keep the "
    "program's observable behaviour, and must differ from the others and from the rejected ideas.")

PROPOSER_SYSTEM = (
    "You change source files to test ONE hypothesis. Keep the program's observable behaviour and output "
    "exactly the same; do not touch anything the hypothesis does not need. Reply in exactly this format and "
    "nothing else:\nHYPOTHESIS: <the hypothesis in one line>\n=== FILE: <relative path> ===\n<the complete new "
    "content of that file>\n=== END ===\nRepeat the FILE block for every file you change (complete content, "
    "never a diff, no markdown fences).")


def _files_block(root: str, files: List[str]) -> str:
    parts = []
    for rel in files:
        text = Path(root, rel).read_text(encoding="utf-8", errors="replace")
        parts.append(f"=== FILE: {rel} ===\n{text}\n=== END ===")
    return "\n".join(parts)


def _card_brief(card: Mapping[str, Any]) -> str:
    base = card.get("baseline") or {}
    better = "lower" if card.get("direction") == "down" else "higher"
    return (f"Goal: {card.get('goal')}\nMeasured by running: {card.get('command')}\n"
            f"Metric: {loop_card.describe_extract(card.get('extract') or {})}; {better} is better. "
            f"Baseline mean {base.get('mean')}, noise (sd) {base.get('stdev')}.")


def _rejected(card: Mapping[str, Any]) -> List[str]:
    out = []
    for row in card.get("ledger") or []:
        if row.get("verdict") in ("DROP", "INCONCLUSIVE", "MINOR", "loss", "noise"):
            out.append(f"{row.get('change')} -> {row.get('verdict')} ({row.get('why') or ''})"[:240])
    return out[-20:]


def parse_proposal(text: str, allowed: List[str]) -> Tuple[str, Dict[str, str]]:
    hyp = ""
    m = re.search(r"(?im)^\s*HYPOTHESIS:\s*(.+)$", text or "")
    if m:
        hyp = m.group(1).strip()
    files: Dict[str, str] = {}
    norm = {a.replace("\\", "/").lower(): a for a in allowed}
    for fm in re.finditer(r"(?ms)^=== FILE:\s*(.+?)\s*===\s*\n(.*?)(?=^=== END ===|^=== FILE:|\Z)", text or ""):
        rel = fm.group(1).strip().strip("`").replace("\\", "/")
        body = fm.group(2)
        body = re.sub(r"^\s*```[\w+-]*\n", "", body)
        body = re.sub(r"\n```\s*$", "", body.rstrip()) + "\n"
        key = norm.get(rel.lower())
        if key:
            files[key] = body
    return hyp, files


def parse_hypotheses(text: str, n: int) -> List[str]:
    from src import fresh_llm
    data = fresh_llm.parse_json(text)
    items: List[str] = []
    if isinstance(data, list):
        for x in data:
            if isinstance(x, str):
                items.append(x)
            elif isinstance(x, Mapping):
                items.append(str(x.get("hypothesis") or x.get("idea") or x.get("text") or ""))
    if not items:
        items = [re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", ln).strip() for ln in (text or "").splitlines()]
    seen, out = set(), []
    for it in items:
        it = it.strip().strip('"')
        if len(it) > 8 and it.lower() not in seen:
            seen.add(it.lower())
            out.append(it[:400])
    return out[:n]


# ---------------------------------------------------------------------------
# improvement loop: the run
# ---------------------------------------------------------------------------
def _folder_bytes(root: str, cap: int) -> int:
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules", "venv", ".venv", "__pycache__")]
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except OSError:
                pass
            if total > cap:
                return total
    return total


def start_improve(owner: str, args: Mapping[str, Any], *, ask=None, runner: Optional[loop_card.Runner] = None,
                  background: bool = True) -> Dict[str, Any]:
    """Validate and schedule an improvement run; returns the job at once (poll `get_job`)."""
    card = loop_card.get(owner, str(args.get("id") or args.get("card_id") or ""), args.get("project") or None)
    workspace = os.path.realpath(str(args.get("workspace") or card.get("cwd") or "").strip() or ".")
    if not os.path.isdir(workspace) or not (args.get("workspace") or card.get("cwd")):
        raise LoopError("the improvement loop needs a folder: the card's `cwd` or `workspace`")
    files = args.get("files")
    if isinstance(files, str):
        files = [f.strip() for f in re.split(r"[\n,]", files) if f.strip()]
    if not isinstance(files, list) or not files:
        raise LoopError("`files` is required: the files (relative to the folder) a proposal may change")
    if len(files) > MAX_FILES:
        raise LoopError(f"at most {MAX_FILES} files")
    clean = []
    for f in files:
        rel = str(f).replace("\\", "/").strip().lstrip("/")
        full = os.path.realpath(os.path.join(workspace, rel))
        if not full.startswith(workspace + os.sep) or ".." in rel.split("/"):
            raise LoopError(f"{f} is outside the folder")
        if not os.path.isfile(full):
            raise LoopError(f"{f} does not exist in {workspace}")
        if os.path.getsize(full) > MAX_FILE_BYTES:
            raise LoopError(f"{f} is larger than {MAX_FILE_BYTES // 1000} KB; name smaller files")
        clean.append(rel)
    from src import git_panel
    if not git_panel.repo_toplevel(workspace) and _folder_bytes(workspace, MAX_SNAPSHOT_BYTES) > MAX_SNAPSHOT_BYTES:
        raise LoopError("the folder is not a git repository and is too large to copy once per proposal")
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    job = {
        "id": f"imp-{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}", "card_id": card["id"],
        "project": card.get("project"), "goal": card["goal"], "workspace": workspace, "files": clean,
        "proposals": _int(args.get("proposals"), 3, 1, MAX_PROPOSALS),
        "iterations": _int(args.get("iterations"), 1, 1, 5), "rounds": _int(args.get("rounds"), DEFAULT_ROUNDS, 2, MAX_ROUNDS),
        "min_pairs": _int(args.get("min_pairs"), int(card.get("min_pairs") or 0) or 0, 0, MAX_ROUNDS) or None,
        "guard_command": str(args.get("guard_command") or card.get("guard_command") or "").strip(),
        "apply": bool(args.get("apply", True)), "keep_alternatives": bool(args.get("keep_alternatives", False)),
        "model": str(args.get("model") or ""), "endpoint_id": str(args.get("endpoint_id") or ""),
        "hints": str(args.get("hints") or "")[:2000],
        "status": "queued", "phase": "queued", "created": now, "heartbeat": time.time(), "log": [],
        "history": [], "iteration": 0, "winner": None, "applied": False, "error": None,
    }
    _save_job(owner, job)
    if background:
        task = asyncio.ensure_future(_run_job(owner, job["id"], ask=ask, runner=runner))
        _TASKS[job["id"]] = task
        task.add_done_callback(lambda _t, jid=job["id"]: (_TASKS.pop(jid, None), _CANCEL.discard(jid)))
    return job


async def _default_ask(owner: str, job: Mapping[str, Any], messages: List[Dict[str, Any]], run_id: str) -> str:
    from src import fresh_llm
    text, _meta = await fresh_llm.ask(owner, messages, model=job.get("model") or "",
                                      endpoint_id=job.get("endpoint_id") or "", run_id=run_id, timeout=600,
                                      parallel=int(job.get("proposals") or 1))
    return text


async def _run_job(owner: str, job_id: str, *, ask=None, runner: Optional[loop_card.Runner] = None) -> Dict[str, Any]:
    from src import alternatives
    job = get_job(owner, job_id)
    ask = ask or _default_ask
    runner = runner or loop_card.default_runner

    def log(msg: str, phase: Optional[str] = None) -> None:
        if phase:
            job["phase"] = phase
        job["log"].append({"at": time.strftime("%H:%M:%S"), "msg": msg[:500]})
        job["log"] = job["log"][-200:]
        job["heartbeat"] = time.time()
        _save_job(owner, job)

    def stopped() -> bool:
        return job_id in _CANCEL

    job["status"] = "running"
    try:
        for it in range(1, int(job["iterations"]) + 1):
            if stopped():
                break
            job["iteration"] = it
            card = loop_card.get(owner, job["card_id"], job.get("project"))
            entry: Dict[str, Any] = {"iteration": it, "arms": [], "experiment_id": None}
            job["history"].append(entry)
            files_text = _files_block(job["workspace"], job["files"])
            brief = _card_brief(card)
            rejected = _rejected(card)
            log(f"iteration {it}: asking for {job['proposals']} hypotheses", "hypotheses")
            hyp_prompt = (f"{brief}\nFiles that may change: {', '.join(job['files'])}\n"
                          + (f"Hints from the user: {job['hints']}\n" if job.get("hints") else "")
                          + ("Rejected before (do not repeat):\n- " + "\n- ".join(rejected) + "\n" if rejected else "")
                          + f"\n{files_text}\n\nGive {job['proposals']} hypotheses as a JSON array of strings.")
            text = await ask(owner, job, [{"role": "system", "content": HYPOTHESES_SYSTEM},
                                          {"role": "user", "content": hyp_prompt}], f"{job_id}-h{it}")
            hyps = parse_hypotheses(text, int(job["proposals"]))
            if not hyps:
                raise LoopError("the model gave no usable hypothesis")
            entry["hypotheses"] = hyps
            log(f"{len(hyps)} hypotheses; {len(hyps)} workers are writing one proposal each", "proposals")

            async def propose(i: int, h: str) -> Tuple[int, str, Dict[str, str], str]:
                prompt = (f"{brief}\nHypothesis to test: {h}\nFiles you may change: {', '.join(job['files'])}\n\n{files_text}")
                try:
                    reply = await ask(owner, job, [{"role": "system", "content": PROPOSER_SYSTEM},
                                                   {"role": "user", "content": prompt}], f"{job_id}-p{it}-{i}")
                except Exception as exc:  # noqa: BLE001 - one proposer never sinks the run
                    return i, h, {}, f"{type(exc).__name__}: {exc}"[:300]
                got_h, files = parse_proposal(reply, job["files"])
                return i, h or got_h, files, "" if files else "the reply had no FILE block for the named files"

            results = await asyncio.gather(*(propose(i, h) for i, h in enumerate(hyps)))
            if stopped():
                break
            log("creating the isolated alternatives", "isolating")
            exp = await asyncio.to_thread(alternatives.create_experiment, owner, job.get("project") or "",
                                          f"Loop {card['id']}: {card['goal']}"[:300], job["workspace"])
            entry["experiment_id"] = exp["id"]
            entry["kept_experiment"] = bool(job.get("keep_alternatives"))
            control = await asyncio.to_thread(alternatives.add_alternative, owner, exp["id"], "control")
            current = {rel: Path(job["workspace"], rel).read_text(encoding="utf-8", errors="replace") for rel in job["files"]}
            _write_files(control["path"], current)
            arms: List[Dict[str, Any]] = []
            for i, h, files, err in results:
                arm = {"name": f"h{i + 1}", "hypothesis": h, "error": err, "files": sorted(files)}
                if files:
                    same = all(Path(job["workspace"], rel).read_text(encoding="utf-8", errors="replace") == body
                               for rel, body in files.items())
                    if same:
                        arm["error"] = "the proposal did not change anything"
                if not arm["error"]:
                    alt = await asyncio.to_thread(alternatives.add_alternative, owner, exp["id"], f"h{i + 1}: {h}"[:120])
                    _write_files(alt["path"], {**current, **files})
                    arm["alternative_id"] = alt["id"]
                    arm["path"] = alt["path"]
                arms.append(arm)
            entry["arms"] = arms
            entry["control_path"] = control["path"]
            ws = job["workspace"]

            def cmd_for(path: str) -> str:
                return card["command"].replace(ws, path) if ws in card["command"] else card["command"]

            timeout_s = float(card.get("timeout_s") or loop_card.DEFAULT_TIMEOUT_S)
            if job.get("guard_command"):
                log("running the guard command in every proposal", "guard")
                for arm in arms:
                    if arm.get("path"):
                        g = job["guard_command"].replace(ws, arm["path"])
                        code, out, _ = await asyncio.to_thread(_safe_run, runner, g, arm["path"], timeout_s)
                        if code != 0:
                            arm["error"] = f"the guard command failed: {out.strip()[-300:]}"
            measured = [a for a in arms if a.get("path") and not a.get("error")]
            labels = {a["name"]: a["hypothesis"] for a in arms}
            stats: Dict[str, Any] = {"arms": []}
            if measured:
                variants = [{"name": "control", "command": cmd_for(control["path"]), "cwd": control["path"]}] + \
                           [{"name": a["name"], "command": cmd_for(a["path"]), "cwd": a["path"]} for a in measured] + \
                           [{"name": "control-aa", "command": cmd_for(control["path"]), "cwd": control["path"]}]
                log(f"measuring {len(measured)} proposals against the control in {job['rounds']} paired rounds", "measuring")
                samples = await asyncio.to_thread(
                    measure_rounds, variants, card["extract"], int(job["rounds"]), timeout_s, runner, it, stopped,
                    lambda r, n: log(f"round {r}/{n} done"))
                bar_abs, bar_pct = _keep_bar(card)
                min_pairs = int(job.get("min_pairs") or card.get("min_pairs") or min(DEFAULT_MIN_PAIRS, int(job["rounds"])))
                stats = arm_stats(samples, "control", aa="control-aa", higher_is_better=card["direction"] == "up",
                                  bar=bar_abs, bar_pct=bar_pct, min_pairs=min_pairs)
                if stats.get("error"):
                    raise LoopError(stats["error"], "loop.command_failed")
                entry["samples"] = samples
            for a in arms:
                if a.get("error"):
                    stats.setdefault("arms", []).append({"arm": a["name"], "pairs": 0, "verdict": "DROP", "why": a["error"]})
            entry["stats"] = stats
            append_ledger(owner, card["id"], _ledger_rows(card, stats, labels, f"improve {job_id}"), card.get("project"))
            keeps = sorted((s for s in stats.get("arms", []) if s.get("verdict") == "KEEP"),
                           key=lambda s: s.get("median_delta") or 0, reverse=True)
            if not keeps:
                log("no proposal cleared the keep bar; nothing applied", "done")
                break
            best = keeps[0]
            win = next(a for a in arms if a["name"] == best["arm"])
            entry["winner"] = {"arm": win["name"], "hypothesis": win["hypothesis"], "median_delta": best.get("median_delta"),
                               "delta_pct": best.get("delta_pct")}
            job["winner"] = entry["winner"]
            if not job.get("apply"):
                entry["kept_experiment"] = True
                log(f"winner {win['name']} ({win['hypothesis']}); not applied (apply=false), the alternatives are kept", "done")
                break
            log(f"applying {win['name']} to {ws}", "applying")
            await asyncio.to_thread(alternatives.apply_alternative, owner, exp["id"], win["alternative_id"])
            job["applied"] = True
            entry["applied"] = True
            log("measuring the real folder again against the baseline", "verifying")
            check = await asyncio.to_thread(loop_card.check, owner, card["id"], None, None, True, card.get("project"), runner)
            entry["verify"] = {k: check.get(k) for k in ("verdict", "delta", "delta_pct", "threshold")}
            log(f"after applying: {check.get('verdict')} ({(check.get('delta_pct') or 0):+.1f} %) against the old baseline; "
                "the card's baseline now is the new code")
            if not entry["kept_experiment"]:
                await asyncio.to_thread(alternatives.delete_experiment, owner, exp["id"])
                entry["experiment_deleted"] = True
        job["status"] = "cancelled" if stopped() else "done"
        job["phase"] = job["status"]
    except LoopError as exc:
        job["status"], job["error"] = "failed", exc.detail
    except Exception as exc:  # noqa: BLE001 - the job record must say what happened
        job["status"], job["error"] = "failed", f"{type(exc).__name__}: {exc}"[:500]
    for entry in job["history"]:
        if entry.get("experiment_id") and not entry.get("kept_experiment") and not entry.get("experiment_deleted"):
            await asyncio.to_thread(_delete_exp_quiet, owner, entry["experiment_id"])
            entry["experiment_deleted"] = True
    job["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    job["heartbeat"] = time.time()
    _save_job(owner, job)
    return job


def _write_files(root: str, files: Mapping[str, str]) -> None:
    for rel, body in files.items():
        target = Path(root, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8", newline="")


def _delete_exp_quiet(owner: str, exp_id: str) -> None:
    from src import alternatives
    try:
        alternatives.delete_experiment(owner, exp_id)
    except Exception:  # noqa: BLE001
        pass


def _safe_run(runner: loop_card.Runner, command: str, cwd: str, timeout_s: float) -> Tuple[int, str, float]:
    try:
        return runner(command, cwd, timeout_s)
    except LoopError as exc:
        return 1, exc.detail, 0.0


async def wait(job_id: str, timeout: float) -> bool:
    task = _TASKS.get(job_id)
    if task is None:
        return True
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout)
        return True
    except asyncio.TimeoutError:
        return False


# ---------------------------------------------------------------------------
# dispatcher shared by the chat tools, the routes and the MCP server
# ---------------------------------------------------------------------------
async def run_improve_action(owner: str, args: Mapping[str, Any], **kw) -> Dict[str, Any]:
    action = str(args.get("action") or "start").lower()
    try:
        if action == "start":
            job = start_improve(owner, args, **kw)
            if args.get("wait_s"):
                await wait(job["id"], float(args["wait_s"]))
                job = get_job(owner, job["id"])
            return job
        if action == "status":
            return get_job(owner, str(args.get("job") or args.get("job_id") or ""))
        if action == "list":
            return {"runs": list_jobs(owner, args.get("id") or args.get("card_id") or None)}
        if action == "cancel":
            return cancel_job(owner, str(args.get("job") or args.get("job_id") or ""))
        if action == "delete":
            return delete_job(owner, str(args.get("job") or args.get("job_id") or ""))
    except LoopError as exc:
        return {"error": exc.detail, "error_class": exc.error_class}
    return {"error": "action must be start, status, list, cancel or delete", "error_class": "loop.bad_args"}


def run_arms_action(owner: str, args: Mapping[str, Any], runner: Optional[loop_card.Runner] = None) -> Dict[str, Any]:
    try:
        return run_arms(owner, args, runner)
    except LoopError as exc:
        return {"error": exc.detail, "error_class": exc.error_class}


def _f(x: Any) -> str:
    return "-" if x is None else f"{x:.4g}"


def render_arms(data: Mapping[str, Any]) -> str:
    s = data.get("stats") or {}
    lines = [f"loop card {data.get('id')}: {len(s.get('arms', []))} arms against the control (median {_f(s.get('control_median'))}), "
             f"{data.get('rounds')} paired rounds, keep bar {_f(s.get('bar'))}"]
    if s.get("aa"):
        lines.append(f"A/A noise floor: {_f(s['aa'].get('noise_floor'))}")
    for a in s.get("arms", []):
        pct = f" ({a['delta_pct']:+.1f} %)" if a.get("delta_pct") is not None else ""
        lines.append(f"- {a['arm']}: {a['verdict']} -- median improvement {_f(a.get('median_delta'))}{pct}, wins "
                     f"{a.get('wins', 0)}/{a.get('pairs', 0)}, 95 % CI [{_f(a.get('ci_low'))}, {_f(a.get('ci_high'))}]; {a.get('why', '')}")
    lines += [f"warning: {w}" for w in s.get("warnings", [])]
    return "\n".join(lines)


def render_job(job: Mapping[str, Any]) -> str:
    if "runs" in job:
        return "\n".join(f"{r['id']}  {r['status']}  {r.get('goal')}  winner={((r.get('winner') or {}).get('arm')) or '-'}"
                         for r in job["runs"]) or "no improvement runs"
    if "deleted" in job or "cancel_requested" in job:
        return json.dumps(job)
    lines = [f"improvement run {job['id']} for card {job['card_id']}: {job['status']} ({job.get('phase')}), iteration "
             f"{job.get('iteration')}/{job.get('iterations')}"]
    for entry in job.get("history") or []:
        for a in (entry.get("stats") or {}).get("arms", []):
            hyp = next((x.get("hypothesis") for x in entry.get("arms", []) if x["name"] == a["arm"]), "")
            lines.append(f"- it{entry['iteration']} {a['arm']} {a['verdict']}: {hyp} -- {_f(a.get('median_delta'))} ({a.get('why', '')})")
        if entry.get("verify"):
            lines.append(f"  applied and re-measured: {entry['verify'].get('verdict')} {(entry['verify'].get('delta_pct') or 0):+.1f} %")
    if job.get("error"):
        lines.append(f"error: {job['error']}")
    if job.get("log"):
        lines.append(f"last: {job['log'][-1]['msg']}")
    return "\n".join(lines)
