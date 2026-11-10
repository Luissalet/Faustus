"""src/loop_card.py -- Loop Card: find the feedback loop before trying to improve something.

A card says: what we want to improve (goal), the command that measures it, how the
number is read from the command's output, which direction is better, and -- measured,
not guessed -- how noisy that number is. From N repeated runs it derives the mean,
median, standard deviation, min/max, coefficient of variation and the *improvement
threshold*: the larger of 2 x the standard deviation and 1 % of the mean. A later change
only counts as better when the metric moves past that threshold in the right direction.

`create` measures the baseline and stores the card. `check` measures again and says
`win`, `loss` or `noise` against the baseline, with the difference and its size against the
noise. Nothing here changes the project: the only side effect is running the measuring
command, which the caller chose.

Cards live under DATA_DIR/loop_cards/<owner>/<project>/<id>.json.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from src import constants

DEFAULT_REPEATS = 5
MAX_REPEATS = 50
MAX_TIMEOUT_S = 3600
DEFAULT_TIMEOUT_S = 300
MAX_HISTORY = 30
_OUT_CAP = 200_000
ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")

# runner(command, cwd, timeout_s) -> (exit_code, output, seconds)
Runner = Callable[[str, Optional[str], float], Tuple[int, str, float]]


class LoopError(Exception):
    def __init__(self, detail: str, error_class: str = "loop.bad_args"):
        super().__init__(detail)
        self.detail = detail
        self.error_class = error_class


# ---------------------------------------------------------------------------
# running and reading the metric
# ---------------------------------------------------------------------------
def default_runner(command: str, cwd: Optional[str], timeout_s: float) -> Tuple[int, str, float]:
    started = time.perf_counter()
    try:
        proc = subprocess.run(command, shell=True, cwd=cwd or None, capture_output=True, timeout=timeout_s,
                              stdin=subprocess.DEVNULL, env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
    except subprocess.TimeoutExpired:
        raise LoopError(f"the measuring command did not finish in {int(timeout_s)} s", "loop.timeout")
    except OSError as exc:
        raise LoopError(f"could not start the command: {exc}", "loop.cannot_run")
    seconds = time.perf_counter() - started
    out = (proc.stdout or b"").decode("utf-8", "replace") + (("\n" + proc.stderr.decode("utf-8", "replace")) if proc.stderr else "")
    return proc.returncode, out[-_OUT_CAP:], seconds


def _to_float(text: str) -> Optional[float]:
    s = str(text).strip().replace("−", "-")
    try:
        return float(s)
    except ValueError:
        pass
    cleaned = re.sub(r"[^0-9eE+.\-]", "", s.replace(",", ""))
    try:
        return float(cleaned) if cleaned and re.search(r"\d", cleaned) else None
    except ValueError:
        return None


def _json_path(data: Any, path: str) -> Any:
    cur = data
    for part in [p for p in re.split(r"\.|\[(\d+)\]", path.strip().lstrip("$.")) if p]:
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                raise LoopError(f"JSON path `{path}`: no item {part}", "loop.no_metric")
        elif isinstance(cur, dict):
            if part not in cur:
                raise LoopError(f"JSON path `{path}`: no key `{part}`", "loop.no_metric")
            cur = cur[part]
        else:
            raise LoopError(f"JSON path `{path}`: `{part}` is not reachable", "loop.no_metric")
    return cur


def _load_json_output(output: str) -> Any:
    text = output.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                return json.loads(line)
            except ValueError:
                continue
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            pass
    raise LoopError("the output is not JSON, so a JSON path cannot be read from it", "loop.no_metric")


def read_metric(extract: Mapping[str, Any], output: str, seconds: float) -> float:
    kind = extract.get("kind")
    if kind == "time":
        return float(seconds)
    if kind == "regex":
        pattern = str(extract.get("pattern") or "")
        try:
            rx = re.compile(pattern, re.MULTILINE)
        except re.error as exc:
            raise LoopError(f"the regex does not compile: {exc}")
        if rx.groups < 1:
            raise LoopError("the regex needs one capture group around the number, e.g. `took (\\d+\\.?\\d*) ms`")
        matches = list(rx.finditer(output))
        if not matches:
            raise LoopError(f"the regex `{pattern}` matched nothing in the output; last lines: {output.strip()[-300:]!r}", "loop.no_metric")
        value = _to_float(matches[-1].group(1))   # the last match: a final result is printed last
        if value is None:
            raise LoopError(f"the captured text {matches[-1].group(1)!r} is not a number", "loop.no_metric")
        return value
    if kind == "json":
        value = _json_path(_load_json_output(output), str(extract.get("path") or ""))
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise LoopError(f"the JSON value at `{extract.get('path')}` is not a number", "loop.no_metric")
        number = _to_float(str(value))
        if number is None:
            raise LoopError(f"the JSON value {value!r} is not a number", "loop.no_metric")
        return number
    raise LoopError("`extract` must be one of: a regex (one group), a JSON path, or time")


def normalise_extract(args: Mapping[str, Any]) -> Dict[str, Any]:
    ex = args.get("extract")
    if isinstance(ex, str) and ex.strip():
        text = ex.strip()
        if text.lower() in ("time", "tiempo", "duration", "duracion", "duración"):
            return {"kind": "time"}
        ex = {"kind": "json", "path": text[5:].strip()} if text.lower().startswith("json:") else \
             {"kind": "regex", "pattern": text[6:].strip() if text.lower().startswith("regex:") else text}
    if not isinstance(ex, Mapping):
        if args.get("regex"):
            ex = {"kind": "regex", "pattern": args["regex"]}
        elif args.get("json_path"):
            ex = {"kind": "json", "path": args["json_path"]}
        else:
            raise LoopError("`extract` is required: a regex with one group, a JSON path, or \"time\"")
    kind = str(ex.get("kind") or "").lower()
    if kind in ("regex", "re"):
        out = {"kind": "regex", "pattern": str(ex.get("pattern") or ex.get("regex") or "")}
    elif kind in ("json", "json_path"):
        out = {"kind": "json", "path": str(ex.get("path") or ex.get("json_path") or "")}
    elif kind in ("time", "tiempo", "duration"):
        out = {"kind": "time"}
    else:
        raise LoopError("`extract.kind` must be regex, json or time")
    if out["kind"] == "regex" and not out["pattern"]:
        raise LoopError("`extract.pattern` is empty")
    if out["kind"] == "json" and not out["path"]:
        raise LoopError("`extract.path` is empty")
    return out


def normalise_direction(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ("up", "higher", "increase", "max", "subir", "sube", "mas", "más", "mayor", "maximizar"):
        return "up"
    if text in ("down", "lower", "decrease", "min", "bajar", "baja", "menos", "menor", "minimizar", ""):
        return "down"
    raise LoopError("`direction` must be \"up\" (higher is better) or \"down\" (lower is better)")


# ---------------------------------------------------------------------------
# statistics
# ---------------------------------------------------------------------------
def summarize(values: List[float]) -> Dict[str, Any]:
    n = len(values)
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if n > 1 else 0.0
    return {"n": n, "mean": mean, "median": statistics.median(values), "stdev": sd, "min": min(values), "max": max(values),
            "cv": (sd / abs(mean)) if mean else None, "threshold": max(2 * sd, 0.01 * abs(mean))}


def verdict(base: Mapping[str, Any], new: Mapping[str, Any], direction: str) -> Dict[str, Any]:
    delta = new["mean"] - base["mean"]
    signed = delta if direction == "up" else -delta     # > 0 means better
    threshold = base["threshold"]
    if threshold > 0 and signed >= threshold:
        label = "win"
    elif threshold > 0 and signed <= -threshold:
        label = "loss"
    elif threshold == 0:
        label = "win" if signed > 0 else "loss" if signed < 0 else "noise"
    else:
        label = "noise"
    sd = base["stdev"]
    return {"verdict": label, "delta": delta, "delta_pct": (delta / abs(base["mean"]) * 100) if base["mean"] else None,
            "better_by": signed, "threshold": threshold,
            "noise_multiple": (abs(delta) / sd) if sd else None,
            "threshold_multiple": (abs(delta) / threshold) if threshold else None}


# ---------------------------------------------------------------------------
# storage
# ---------------------------------------------------------------------------
def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "")).strip("._")[:80] or "_"


def _dir(owner: str, project: str) -> Path:
    return Path(constants.DATA_DIR) / "loop_cards" / _safe(owner or "_") / _safe(project or "_")


def _path(owner: str, project: str, card_id: str) -> Path:
    if not ID_RE.match(card_id or ""):
        raise LoopError(f"invalid loop card id {card_id!r}", "loop.not_found")
    return _dir(owner, project) / f"{card_id}.json"


def _save(owner: str, card: Dict[str, Any]) -> None:
    p = _path(owner, card["project"], card["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _find(owner: str, card_id: str, project: Optional[str] = None) -> Dict[str, Any]:
    if not ID_RE.match(card_id or ""):
        raise LoopError(f"no such loop card: {card_id}", "loop.not_found")
    base = Path(constants.DATA_DIR) / "loop_cards" / _safe(owner or "_")
    candidates = [_dir(owner, project)] if project else (list(base.iterdir()) if base.is_dir() else [])
    for d in candidates:
        p = d / f"{card_id}.json"
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise LoopError(f"loop card {card_id} is unreadable", "loop.corrupt")
    raise LoopError(f"no such loop card: {card_id}", "loop.not_found")


def _row(card: Mapping[str, Any]) -> Dict[str, Any]:
    base = card.get("baseline") or {}
    last = (card.get("checks") or [None])[-1]
    return {"id": card["id"], "project": card.get("project"), "goal": card.get("goal"), "command": card.get("command"),
            "cwd": card.get("cwd"), "extract": card.get("extract"), "direction": card.get("direction"),
            "repeats": card.get("repeats"), "created": card.get("created"), "updated": card.get("updated"),
            "baseline": {k: base.get(k) for k in ("n", "mean", "median", "stdev", "min", "max", "cv", "threshold")},
            "noisy": bool(base.get("cv") is not None and base["cv"] > 0.10),
            "last_check": ({"at": last["at"], "verdict": last["verdict"], "delta": last["delta"], "delta_pct": last.get("delta_pct"),
                            "mean": last["summary"]["mean"], "noise_multiple": last.get("noise_multiple")} if last else None),
            "checks": len(card.get("checks") or [])}


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------
def _measure(command: str, cwd: Optional[str], extract: Mapping[str, Any], repeats: int, timeout_s: float,
             runner: Runner) -> Tuple[List[float], List[Dict[str, Any]]]:
    values: List[float] = []
    runs: List[Dict[str, Any]] = []
    for i in range(repeats):
        code, output, seconds = runner(command, cwd, timeout_s)
        if code != 0:
            raise LoopError(f"run {i + 1}/{repeats}: the command exited with code {code}; last output: {output.strip()[-400:]!r}",
                            "loop.command_failed")
        try:
            value = read_metric(extract, output, seconds)
        except LoopError as exc:
            raise LoopError(f"run {i + 1}/{repeats}: {exc.detail}", exc.error_class)
        values.append(value)
        runs.append({"value": value, "seconds": round(seconds, 4)})
    return values, runs


def _int(value: Any, default: int, lo: int, hi: int, label: str) -> int:
    if value in (None, ""):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise LoopError(f"`{label}` must be an integer")
    return max(lo, min(hi, n))


def _cwd(value: Any) -> Optional[str]:
    cwd = str(value or "").strip() or None
    if cwd and not os.path.isdir(cwd):
        raise LoopError(f"`cwd` is not a folder: {cwd}")
    return cwd


def create(owner: str, args: Mapping[str, Any], runner: Optional[Runner] = None) -> Dict[str, Any]:
    runner = runner or default_runner
    goal = str(args.get("goal") or "").strip()
    command = str(args.get("command") or "").strip()
    if not goal:
        raise LoopError("`goal` is required: what you want to improve")
    if not command:
        raise LoopError("`command` is required: the command that measures it")
    extract = normalise_extract(args)
    direction = normalise_direction(args.get("direction"))
    repeats = _int(args.get("repeats"), DEFAULT_REPEATS, 2, MAX_REPEATS, "repeats")
    timeout_s = float(_int(args.get("timeout_s"), DEFAULT_TIMEOUT_S, 1, MAX_TIMEOUT_S, "timeout_s"))
    cwd = _cwd(args.get("cwd"))
    project = str(args.get("project") or args.get("project_id") or "").strip() or "default"
    values, runs = _measure(command, cwd, extract, repeats, timeout_s, runner)
    base = summarize(values)
    base["values"] = values
    base["runs"] = runs
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    card = {"id": f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}", "project": project, "goal": goal[:400],
            "command": command, "cwd": cwd, "extract": extract, "direction": direction, "repeats": repeats,
            "timeout_s": timeout_s, "created": now, "updated": now, "baseline": base, "baseline_at": now, "checks": []}
    _save(owner, card)
    return {**card, "warnings": warnings_for(base)}


def warnings_for(base: Mapping[str, Any]) -> List[str]:
    out = []
    if base["n"] < 3:
        out.append("fewer than 3 runs: the noise estimate is unreliable")
    if base.get("cv") is not None and base["cv"] > 0.10:
        out.append(f"noisy metric (variation {base['cv'] * 100:.1f} % of the mean): only large changes will count; "
                   "make the measurement steadier or raise the repeats before trusting small wins")
    if base["stdev"] == 0 and base["n"] >= 3:
        out.append("the value never varied: the threshold falls back to 1 % of the mean")
    return out


def check(owner: str, card_id: str, command: Optional[str] = None, repeats: Any = None, rebase: bool = False,
          project: Optional[str] = None, runner: Optional[Runner] = None) -> Dict[str, Any]:
    runner = runner or default_runner
    card = _find(owner, card_id, project)
    cmd = (command or "").strip() or card["command"]
    n = _int(repeats, int(card.get("repeats") or DEFAULT_REPEATS), 2, MAX_REPEATS, "repeats")
    values, runs = _measure(cmd, card.get("cwd"), card["extract"], n, float(card.get("timeout_s") or DEFAULT_TIMEOUT_S), runner)
    new = summarize(values)
    result = verdict(card["baseline"], new, card["direction"])
    notes = []
    if new["stdev"] > 2 * max(card["baseline"]["stdev"], 1e-12) and new["n"] >= 3:
        notes.append("this measurement was noisier than the baseline; repeat it before believing the verdict")
    entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "command": cmd, "summary": {**new, "values": values}, "runs": runs,
             **result, "notes": notes}
    card.setdefault("checks", []).append(entry)
    card["checks"] = card["checks"][-MAX_HISTORY:]
    if rebase:
        card["baseline"] = {**new, "values": values, "runs": runs}
        card["baseline_at"] = entry["at"]
    card["updated"] = entry["at"]
    _save(owner, card)
    return {"id": card["id"], "goal": card["goal"], "direction": card["direction"], "baseline": {k: card["baseline"][k] for k in ("n", "mean", "median", "stdev", "cv", "threshold")} if not rebase else None,
            **entry, "rebased": bool(rebase)}


def list_cards(owner: str, project: Optional[str] = None) -> List[Dict[str, Any]]:
    base = Path(constants.DATA_DIR) / "loop_cards" / _safe(owner or "_")
    dirs = [_dir(owner, project)] if project else (list(base.iterdir()) if base.is_dir() else [])
    rows = []
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.glob("*.json"):
            try:
                rows.append(_row(json.loads(p.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
    rows.sort(key=lambda r: r["created"] or "", reverse=True)
    return rows


def get(owner: str, card_id: str, project: Optional[str] = None) -> Dict[str, Any]:
    return _find(owner, card_id, project)


def delete(owner: str, card_id: str, project: Optional[str] = None) -> Dict[str, Any]:
    card = _find(owner, card_id, project)
    _path(owner, card["project"], card_id).unlink(missing_ok=True)
    return {"deleted": card_id}


# ---------------------------------------------------------------------------
# dispatcher shared by the chat tools, the HTTP routes and the MCP server
# ---------------------------------------------------------------------------
def run(owner: str, args: Mapping[str, Any], runner: Optional[Runner] = None) -> Dict[str, Any]:
    action = str(args.get("action") or "").lower()
    try:
        if action == "create":
            return create(owner, args, runner)
        if action == "check":
            return check(owner, str(args.get("id") or args.get("card_id") or ""), args.get("command"), args.get("repeats"),
                         bool(args.get("rebase")), args.get("project") or None, runner)
        if action == "list":
            return {"cards": list_cards(owner, args.get("project") or None)}
        if action == "get":
            return get(owner, str(args.get("id") or args.get("card_id") or ""), args.get("project") or None)
        if action == "delete":
            return delete(owner, str(args.get("id") or args.get("card_id") or ""), args.get("project") or None)
    except LoopError as exc:
        return {"error": exc.detail, "error_class": exc.error_class}
    return {"error": "action must be create, check, list, get or delete", "error_class": "loop.bad_args"}


def _fmt(x: Any, digits: int = 4) -> str:
    return "-" if x is None else f"{x:.{digits}g}"


def render(data: Mapping[str, Any]) -> str:
    if "cards" in data:
        rows = data["cards"]
        return "\n".join(f"{r['id']}  {r['goal']}  mean={_fmt(r['baseline']['mean'])} noise(sd)={_fmt(r['baseline']['stdev'])} "
                         f"threshold={_fmt(r['baseline']['threshold'])}  last={(r['last_check'] or {}).get('verdict', 'not checked')}"
                         for r in rows) or "no loop cards yet"
    if "verdict" in data:
        pct = f" ({data['delta_pct']:+.1f} %)" if data.get("delta_pct") is not None else ""
        mult = f", {data['threshold_multiple']:.2f} x the threshold" if data.get("threshold_multiple") is not None else ""
        words = {"win": "BETTER than the baseline", "loss": "WORSE than the baseline", "noise": "within the noise"}[data["verdict"]]
        return (f"loop card {data['id']}: {words}. mean {_fmt(data['summary']['mean'])} vs baseline {_fmt(data['baseline']['mean'])}, difference {data['delta']:+.4g}{pct}"
                f"{mult}; threshold {_fmt(data['threshold'])}." + ("".join(f"\nnote: {n}" for n in data["notes"])))
    if "baseline" in data and "checks" in data:
        b = data["baseline"]
        text = (f"loop card {data['id']} for: {data['goal']}\ncommand: {data['command']}\nbetter = {'higher' if data['direction'] == 'up' else 'lower'}; "
                f"{b['n']} runs: mean {_fmt(b['mean'])}, median {_fmt(b['median'])}, sd {_fmt(b['stdev'])}, min {_fmt(b['min'])}, max {_fmt(b['max'])}, "
                f"variation {_fmt((b['cv'] or 0) * 100, 3)} %\nimprovement threshold: {_fmt(b['threshold'])} (max of 2 x sd and 1 % of the mean)")
        return text + "".join(f"\nwarning: {w}" for w in data.get("warnings", []))
    if "deleted" in data:
        return f"deleted loop card {data['deleted']}"
    return json.dumps(data, ensure_ascii=False, default=str)
