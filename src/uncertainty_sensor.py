"""uncertainty_sensor.py -- decide what to observe next (pure, deterministic).

Given competing hypotheses (each with a prior weight), candidate observations
or tests (each with a cost and, per hypothesis, the outcome that hypothesis
predicts) and the results already observed, this module returns:

* the posterior over the hypotheses (Bayesian update);
* the candidate observations ranked by expected information gain per unit of
  cost (entropy reduction, in bits);
* the single recommended next observation, with a plain statement of which
  hypotheses it splits and which it leaves together;
* the pairs of hypotheses that no candidate can separate, and what kind of
  observation would be needed;
* a stop rule.

A prediction is one of:

* a discrete value (``"cracked"``, ``True``, ``3``): that hypothesis expects
  exactly that outcome;
* a numeric interval (``[0.4, 0.6]`` or ``{"interval": [0.4, 0.6]}`` or
  ``{"value": 0.5, "tolerance": 0.1}``): the outcome is expected anywhere in it
  with uniform likelihood;
* a probability distribution over outcomes (``{"ok": 0.7, "fail": 0.3}``, or
  ``{"distribution": {...}}``).

Every likelihood keeps a small noise floor (``noise``, default 2 %), so a
single surprising result never zeroes a hypothesis for good. No decorative
confidence score is produced: what is reported is the posterior, the entropy
and the test that links to each remaining doubt.

No I/O, no clock, no randomness: the same input always gives the same output.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_NOISE = 0.02
DEFAULT_STOP_THRESHOLD = 0.95
DEFAULT_MIN_GAIN_PER_COST = 0.001
LIVE_POSTERIOR = 0.01          # below this a hypothesis no longer needs separating
SEPARATED_TV = 0.9             # total-variation distance that counts as "tells them apart"
INDISTINGUISHABLE_TV = 0.05    # below this two hypotheses "agree" on a candidate
_OTHER = "<other>"
_EPS = 1e-12


# ── helpers ────────────────────────────────────────────────────────────────

def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


def _label(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip().lower()


def _entropy(ps) -> float:
    return -sum(p * math.log2(p) for p in ps if p > 0)


def _r(x: float, n: int = 6) -> float:
    return round(float(x), n)


def _fmt_num(x: float) -> str:
    return f"{x:.6g}"


# ── parsing ────────────────────────────────────────────────────────────────

def parse_hypotheses(raw: Any) -> List[Dict[str, Any]]:
    """Normalise hypotheses to [{id, prior (sums to 1), description}].

    Accepts a list of names, a list of {id|name, prior|weight, description},
    or a {name: prior} mapping. Hypotheses without a prior get the mean of the
    given priors (uniform when none is given)."""
    items: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        for k, v in raw.items():
            items.append({"id": str(k), "prior": v if _is_num(v) else None,
                          "description": "" if _is_num(v) else str(v)})
    elif isinstance(raw, (list, tuple)):
        for h in raw:
            if isinstance(h, dict):
                hid = h.get("id", h.get("name", h.get("label")))
                prior = h.get("prior", h.get("weight"))
                items.append({"id": "" if hid is None else str(hid).strip(), "prior": prior,
                              "description": str(h.get("description") or h.get("text") or "")})
            else:
                items.append({"id": str(h).strip(), "prior": None, "description": ""})
    else:
        raise ValueError("hypotheses must be a list (names or objects) or a {name: prior} mapping")
    if len(items) < 2:
        raise ValueError("at least two competing hypotheses are needed")
    seen = set()
    for it in items:
        if not it["id"]:
            raise ValueError("every hypothesis needs an id or name")
        if it["id"] in seen:
            raise ValueError(f"duplicate hypothesis id {it['id']!r}")
        seen.add(it["id"])
        p = it["prior"]
        if p is not None and p != "":
            if not _is_num(p) or float(p) < 0:
                raise ValueError(f"prior of {it['id']!r} must be a non-negative number")
            it["prior"] = float(p)
        else:
            it["prior"] = None
    given = [it["prior"] for it in items if it["prior"] is not None]
    fill = (sum(given) / len(given)) if given else 1.0
    for it in items:
        if it["prior"] is None:
            it["prior"] = fill
    total = sum(it["prior"] for it in items)
    if total <= 0:
        raise ValueError("the prior weights sum to zero")
    for it in items:
        it["prior"] = it["prior"] / total
    return items


def _interval_from(v: Any) -> Optional[Tuple[float, float]]:
    """A numeric interval if `v` spells one, else None."""
    if isinstance(v, (list, tuple)) and len(v) == 2 and all(_is_num(x) for x in v):
        a, b = float(v[0]), float(v[1])
        return (min(a, b), max(a, b))
    if isinstance(v, dict):
        for key in ("interval", "range"):
            if key in v:
                return _interval_from(v[key])
        if "min" in v and "max" in v and _is_num(v["min"]) and _is_num(v["max"]):
            a, b = float(v["min"]), float(v["max"])
            return (min(a, b), max(a, b))
        if "value" in v and _is_num(v["value"]):
            tol = None
            for key in ("tolerance", "pm", "sd", "plus_minus"):
                if key in v and _is_num(v[key]):
                    tol = abs(float(v[key]))
                    break
            if tol is not None:
                return (float(v["value"]) - tol, float(v["value"]) + tol)
    return None


def _distribution_from(v: Any) -> Optional[Dict[str, float]]:
    if isinstance(v, dict):
        for key in ("distribution", "outcomes", "probabilities"):
            if key in v and isinstance(v[key], dict):
                v = v[key]
                break
        else:
            if _interval_from(v) is not None or "value" in v:
                return None
        if v and all(_is_num(x) and float(x) >= 0 for x in v.values()):
            total = sum(float(x) for x in v.values())
            if total <= 0:
                raise ValueError("a probability distribution sums to zero")
            out: Dict[str, float] = {}
            for k, x in v.items():
                out[_label(k)] = out.get(_label(k), 0.0) + float(x) / total
            return out
    return None


def parse_candidates(raw: Any, hyp_ids: List[str]) -> List[Dict[str, Any]]:
    """Normalise candidates: kind 'discrete' or 'numeric', per-hypothesis
    predictions as {label: p} or (lo, hi), and the cost."""
    if isinstance(raw, dict):
        raw = [dict(v, id=k) if isinstance(v, dict) else {"id": k} for k, v in raw.items()]
    if not isinstance(raw, (list, tuple)) or not raw:
        raise ValueError("candidates must be a non-empty list of observations/tests")
    out: List[Dict[str, Any]] = []
    seen = set()
    hyp_set = set(hyp_ids)
    for c in raw:
        if not isinstance(c, dict):
            raise ValueError("each candidate must be an object with id, cost and predictions")
        cid = str(c.get("id", c.get("name", ""))).strip()
        if not cid:
            raise ValueError("every candidate needs an id or name")
        if cid in seen:
            raise ValueError(f"duplicate candidate id {cid!r}")
        seen.add(cid)
        unit = str(c.get("cost_unit") or "")
        cost: Any = c.get("cost")
        if cost is None and _is_num(c.get("cost_minutes")):
            cost, unit = c["cost_minutes"], unit or "min"
        if cost is None and _is_num(c.get("cost_money")):
            cost, unit = c["cost_money"], unit or "money"
        if cost is None:
            cost = 1.0
        if not _is_num(cost) or float(cost) < 0:
            raise ValueError(f"cost of {cid!r} must be a non-negative number")
        preds = c.get("predictions", c.get("predicts", c.get("outcomes")))
        if not isinstance(preds, dict) or not preds:
            raise ValueError(f"candidate {cid!r} needs predictions: {{hypothesis: predicted outcome}}")
        unknown = [k for k in preds if str(k) not in hyp_set]
        if unknown:
            raise ValueError(f"candidate {cid!r} predicts for unknown hypothesis {unknown[0]!r}")
        parsed: Dict[str, Any] = {}
        for k, v in preds.items():
            iv = _interval_from(v)
            dist = None if iv is not None else _distribution_from(v)
            parsed[str(k)] = ("interval", iv) if iv is not None else (("dist", dist) if dist is not None else ("scalar", v))
        numeric = any(p[0] == "interval" for p in parsed.values())
        if numeric and any(p[0] == "dist" for p in parsed.values()):
            raise ValueError(f"candidate {cid!r} mixes numeric intervals and discrete distributions")
        preds_n: Dict[str, Any] = {}
        for k, (kind, val) in parsed.items():
            if numeric:
                if kind == "scalar":
                    if not _is_num(val):
                        raise ValueError(f"candidate {cid!r}: prediction {val!r} of {k!r} is not numeric")
                    half = max(abs(float(val)) * 1e-6, 1e-9)
                    preds_n[k] = (float(val) - half, float(val) + half)
                else:
                    lo, hi = val
                    if hi - lo <= 0:
                        half = max(abs(lo) * 1e-6, 1e-9)
                        lo, hi = lo - half, hi + half
                    preds_n[k] = (lo, hi)
            else:
                preds_n[k] = val if kind == "dist" else {_label(val): 1.0}
        out.append({
            "id": cid, "description": str(c.get("description") or c.get("text") or ""),
            "cost": float(cost), "cost_unit": unit, "kind": "numeric" if numeric else "discrete",
            "predictions": preds_n, "repeatable": bool(c.get("repeatable")),
        })
    return out


def parse_observed(raw: Any, candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """[{candidate, result}] -> validated list. `result` is a label, a number,
    or a {interval: [a, b]} measurement range."""
    if raw in (None, "", []):
        return []
    if isinstance(raw, dict):
        raw = [{"candidate": k, "result": v} for k, v in raw.items()]
    if not isinstance(raw, (list, tuple)):
        raise ValueError("observed must be a list of {candidate, result}")
    by_id = {c["id"]: c for c in candidates}
    out = []
    for o in raw:
        if not isinstance(o, dict):
            raise ValueError("each observation must be {candidate, result}")
        cid = str(o.get("candidate", o.get("id", o.get("test", "")))).strip()
        if cid not in by_id:
            raise ValueError(f"observation for unknown candidate {cid!r}")
        if "result" not in o and "outcome" not in o and "value" not in o:
            raise ValueError(f"observation of {cid!r} has no result")
        res = o.get("result", o.get("outcome", o.get("value")))
        cand = by_id[cid]
        if cand["kind"] == "numeric":
            iv = _interval_from(res)
            if iv is not None:
                out.append({"candidate": cid, "result": {"interval": [iv[0], iv[1]]}, "_num": iv})
            elif _is_num(res):
                out.append({"candidate": cid, "result": float(res), "_num": (float(res), float(res))})
            else:
                raise ValueError(f"observation of numeric test {cid!r} must be a number or an interval")
        else:
            out.append({"candidate": cid, "result": _label(res), "_label": _label(res)})
    return out


# ── likelihood model ───────────────────────────────────────────────────────

def _label_space(cand: Dict[str, Any]) -> List[str]:
    labels = sorted({k for p in cand["predictions"].values() for k in p})
    return labels + [_OTHER]


def _breakpoints(cand: Dict[str, Any]) -> List[float]:
    return sorted({x for iv in cand["predictions"].values() for x in iv})


def _span(cand: Dict[str, Any]) -> float:
    bp = _breakpoints(cand)
    width = bp[-1] - bp[0]
    return 2.0 * width if width > 0 else 1.0


def _outcome_masses(cand: Dict[str, Any], hid: str, noise: float) -> Dict[Any, float]:
    """P(outcome bin | hypothesis) over the candidate's outcome partition."""
    if cand["kind"] == "discrete":
        space = _label_space(cand)
        pred = cand["predictions"].get(hid)
        n = len(space)
        if pred is None:
            return {o: 1.0 / n for o in space}
        return {o: (1 - noise) * pred.get(o, 0.0) + noise / n for o in space}
    bp = _breakpoints(cand)
    bins = [("below", None)] + [(i, (bp[i], bp[i + 1])) for i in range(len(bp) - 1)] + [("above", None)]
    n = len(bins)
    iv = cand["predictions"].get(hid)
    out: Dict[Any, float] = {}
    for key, seg in bins:
        if iv is None:
            out[key] = 1.0 / n
            continue
        mass = 0.0
        if seg is not None:
            overlap = max(0.0, min(seg[1], iv[1]) - max(seg[0], iv[0]))
            mass = overlap / (iv[1] - iv[0])
        out[key] = (1 - noise) * mass + noise / n
    return out


def _likelihood(cand: Dict[str, Any], hid: str, obs: Dict[str, Any], noise: float) -> Tuple[float, float]:
    """(likelihood of the observed result under `hid`, the noise-only floor)."""
    pred = cand["predictions"].get(hid)
    if cand["kind"] == "discrete":
        k = len(_label_space(cand))
        label = obs["_label"] if obs["_label"] in cand_labels(cand) else _OTHER
        floor = noise / k
        if pred is None:
            return 1.0 / k, floor
        return (1 - noise) * pred.get(label, 0.0) + floor, floor
    a, b = obs["_num"]
    span = _span(cand)
    if b - a <= 0:  # a point reading: density
        floor = noise / span
        if pred is None:
            return 1.0 / span, floor
        inside = 1.0 / (pred[1] - pred[0]) if pred[0] <= a <= pred[1] else 0.0
        return (1 - noise) * inside + floor, floor
    floor = noise * (b - a) / span  # a range reading: probability mass of the range
    if pred is None:
        return min(1.0, (b - a) / span), floor
    overlap = max(0.0, min(b, pred[1]) - max(a, pred[0]))
    return (1 - noise) * overlap / (pred[1] - pred[0]) + floor, floor


def cand_labels(cand: Dict[str, Any]) -> set:
    return {k for p in cand["predictions"].values() for k in p}


# ── inference ──────────────────────────────────────────────────────────────

def posterior(hyps: List[Dict[str, Any]], cands: List[Dict[str, Any]], observed: List[Dict[str, Any]],
              noise: float) -> Tuple[Dict[str, float], List[str]]:
    by_id = {c["id"]: c for c in cands}
    logp = {h["id"]: math.log(max(h["prior"], _EPS)) if h["prior"] > 0 else -math.inf for h in hyps}
    warnings: List[str] = []
    for o in observed:
        cand = by_id[o["candidate"]]
        liks = {}
        floor = 0.0
        for h in hyps:
            lik, floor = _likelihood(cand, h["id"], o, noise)
            liks[h["id"]] = lik
            logp[h["id"]] += math.log(max(lik, _EPS))
        if max(liks.values()) <= floor * (1 + 1e-9):
            warnings.append(
                f"result of {o['candidate']!r} was not predicted by any hypothesis (only the noise floor explains it): "
                "a hypothesis is probably missing")
    m = max(logp.values())
    w = {k: (math.exp(v - m) if v > -math.inf else 0.0) for k, v in logp.items()}
    z = sum(w.values())
    return {k: v / z for k, v in w.items()}, warnings


def _outcome_table(cand, hyps, post, noise):
    """{hid: {outcome: p}} and the marginal P(outcome)."""
    table = {h["id"]: _outcome_masses(cand, h["id"], noise) for h in hyps}
    outcomes = list(next(iter(table.values())))
    marg = {o: sum(post[h["id"]] * table[h["id"]][o] for h in hyps) for o in outcomes}
    return table, marg


def expected_gain(cand, hyps, post, noise) -> float:
    table, marg = _outcome_table(cand, hyps, post, noise)
    h0 = _entropy(post.values())
    after = 0.0
    for o, po in marg.items():
        if po <= 0:
            continue
        cond = [post[h["id"]] * table[h["id"]][o] / po for h in hyps]
        after += po * _entropy(cond)
    return max(0.0, h0 - after)


def _tv(cand, a, b, noise) -> float:
    pa, pb = _outcome_masses(cand, a, noise), _outcome_masses(cand, b, noise)
    return 0.5 * sum(abs(pa[o] - pb[o]) for o in pa)


def _fmt_pred(cand, hid) -> str:
    pred = cand["predictions"].get(hid)
    if pred is None:
        return "no prediction"
    if cand["kind"] == "numeric":
        lo, hi = pred
        if hi - lo < 1e-5 * max(1.0, abs(lo)):
            return _fmt_num((lo + hi) / 2)
        return f"[{_fmt_num(lo)}, {_fmt_num(hi)}]"
    items = sorted(pred.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(items) == 1 or items[0][1] >= 0.99:
        return repr(items[0][0])
    return " / ".join(f"{k!r} ({round(p * 100)}%)" for k, p in items)


def _names(ids, desc) -> str:
    return " and ".join(f"{i}" for i in ids) if len(ids) <= 2 else ", ".join(ids[:-1]) + " and " + ids[-1]


def _explain(cand, live, post, noise, h0, gain, full, desc_of) -> Dict[str, Any]:
    groups: Dict[str, List[str]] = {}
    for h in live:
        groups.setdefault(_fmt_pred(cand, h), []).append(h)
    separated, together = [], []
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            (separated if _tv(cand, a, b, noise) >= SEPARATED_TV else together).append([a, b])
    parts = [f"{_names(ids, desc_of)} predict{'s' if len(ids) == 1 else ''} {pred}" for pred, ids in groups.items()]
    what = cand["description"] or cand["id"]
    text = f"{what}: " + "; ".join(parts) + "."
    if full:
        text += f" It tells all {len(live)} live hypotheses apart in one step."
    elif together:
        text += (" It does not separate " + "; ".join(f"{a} from {b}" for a, b in together[:4])
                 + (f" (and {len(together) - 4} more pairs)" if len(together) > 4 else "") + ".")
    text += f" Expected uncertainty removed: {gain:.2f} of {h0:.2f} bits."
    return {"text": text, "predictions": {h: _fmt_pred(cand, h) for h in live},
            "splits": [{"predicted_outcome": p, "hypotheses": ids} for p, ids in groups.items()],
            "separated_pairs": separated, "left_together_pairs": together}


# ── the sensor ─────────────────────────────────────────────────────────────

def next_observation(hypotheses: Any, candidates: Any, observed: Any = None, *,
                     noise: float = DEFAULT_NOISE, stop_threshold: float = DEFAULT_STOP_THRESHOLD,
                     min_gain_per_cost: float = DEFAULT_MIN_GAIN_PER_COST) -> Dict[str, Any]:
    """Posterior, ranking, recommendation, unseparable pairs and stop rule."""
    if not _is_num(noise) or not 0 <= float(noise) < 0.5:
        raise ValueError("noise must be between 0 and 0.5")
    if not _is_num(stop_threshold) or not 0 < float(stop_threshold) <= 1:
        raise ValueError("stop_threshold must be in (0, 1]")
    if not _is_num(min_gain_per_cost) or float(min_gain_per_cost) < 0:
        raise ValueError("min_gain_per_cost must be >= 0")
    noise, stop_threshold, min_gain_per_cost = float(noise), float(stop_threshold), float(min_gain_per_cost)

    hyps = parse_hypotheses(hypotheses)
    cands = parse_candidates(candidates, [h["id"] for h in hyps])
    obs = parse_observed(observed, cands)
    desc_of = {h["id"]: h["description"] for h in hyps}
    post, warnings = posterior(hyps, cands, obs, noise)
    for c in cands:
        missing = [h["id"] for h in hyps if h["id"] not in c["predictions"]]
        if missing:
            warnings.append(f"{c['id']!r} has no prediction for {', '.join(missing)}: treated as uninformative for them")
    units = {c["cost_unit"] for c in cands}
    if len(units) > 1:
        warnings.append("candidates use different cost units; gain per cost compares raw numbers")

    ordered = sorted(hyps, key=lambda h: (-post[h["id"]], h["id"]))
    live = [h["id"] for h in ordered if post[h["id"]] >= LIVE_POSTERIOR]
    h0 = _entropy(post.values())
    top = ordered[0]["id"]

    used = {o["candidate"] for o in obs}
    avail = [c for c in cands if c["id"] not in used or c["repeatable"]]
    live_h = [h for h in hyps if h["id"] in live]

    ranking = []
    for c in avail:
        gain = expected_gain(c, hyps, post, noise)
        full = all(_tv(c, a, b, noise) >= SEPARATED_TV for i, a in enumerate(live) for b in live[i + 1:])
        cost = max(c["cost"], 1e-9)
        ranking.append({
            "candidate": c["id"], "description": c["description"], "cost": c["cost"], "cost_unit": c["cost_unit"],
            "expected_gain_bits": _r(gain), "gain_per_cost": _r(gain / cost),
            "separates_all_live": bool(full and len(live) > 1),
        })
    ranking.sort(key=lambda r: (-r["gain_per_cost"], r["cost"], r["candidate"]))

    # pairs of live hypotheses and the test that separates each (each doubt links to its test)
    doubts, unseparable = [], []
    for i, a in enumerate(live):
        for b in live[i + 1:]:
            best = None
            for c in avail:
                tv = _tv(c, a, b, noise)
                key = (-tv, c["cost"], c["id"])
                if best is None or key < best[0]:
                    best = (key, c, tv)
            entry: Dict[str, Any] = {"between": [a, b], "posterior": [_r(post[a]), _r(post[b])]}
            if best is not None and best[2] >= INDISTINGUISHABLE_TV:
                c = best[1]
                entry["discriminating_test"] = c["id"]
                entry["how"] = f"{c['id']}: {a} predicts {_fmt_pred(c, a)}, {b} predicts {_fmt_pred(c, b)}"
                entry["separation"] = _r(best[2], 3)
            else:
                entry["discriminating_test"] = None
                hint = (f"No remaining test separates {a} from {b}: they predict the same outcome on every candidate. "
                        f"Needed: an observation on which {a}" + (f" ({desc_of[a]})" if desc_of[a] else "")
                        + f" and {b}" + (f" ({desc_of[b]})" if desc_of[b] else "")
                        + " predict different outcomes, i.e. one that probes exactly what differs between them.")
                entry["needed_observation"] = hint
                unseparable.append({"between": [a, b], "needed_observation": hint})
            doubts.append(entry)
    doubts.sort(key=lambda d: -(d["posterior"][0] * d["posterior"][1]))

    # stop rule
    if post[top] >= stop_threshold:
        stop = {"stop": True, "reason": "posterior_threshold",
                "detail": f"{top} has posterior {post[top]:.3f} >= {stop_threshold}"}
    elif not avail:
        stop = {"stop": True, "reason": "no_candidates_left", "detail": "every candidate observation has been used"}
    elif ranking[0]["gain_per_cost"] < min_gain_per_cost:
        stop = {"stop": True, "reason": "gain_per_cost_below_minimum",
                "detail": f"best remaining gain per cost {ranking[0]['gain_per_cost']} < {min_gain_per_cost}"}
    else:
        stop = {"stop": False, "reason": "", "detail": ""}
    stop["top_hypothesis"] = top
    stop["top_posterior"] = _r(post[top])
    stop["unresolved"] = len(live) > 1

    # recommendation: the cheapest test that separates every live hypothesis, otherwise best gain per cost
    recommended = None
    if not stop["stop"] and ranking:
        full = [r for r in ranking if r["separates_all_live"]]
        if full:
            pick = min(full, key=lambda r: (r["cost"], -r["expected_gain_bits"], r["candidate"]))
            mode = "cheapest_test_that_separates_all_live_hypotheses"
        else:
            pick = ranking[0]
            mode = "best_expected_gain_per_cost"
        cand = next(c for c in avail if c["id"] == pick["candidate"])
        why = _explain(cand, live, post, noise, h0, pick["expected_gain_bits"], pick["separates_all_live"], desc_of)
        recommended = {**pick, "mode": mode, "why": why["text"], "predictions": why["predictions"],
                       "splits": why["splits"], "separated_pairs": why["separated_pairs"],
                       "left_together_pairs": why["left_together_pairs"]}

    return {
        "hypotheses": [{"id": h["id"], "prior": _r(h["prior"]), "posterior": _r(post[h["id"]]),
                        "description": h["description"]} for h in ordered],
        "uncertainty_bits": _r(h0),
        "observations_used": [{"candidate": o["candidate"], "result": o["result"]} for o in obs],
        "ranking": ranking,
        "recommended": recommended,
        "doubts": doubts,
        "cannot_separate": bool(unseparable),
        "unseparable_pairs": unseparable,
        "stop": stop,
        "warnings": warnings,
        "settings": {"noise": noise, "stop_threshold": stop_threshold, "min_gain_per_cost": min_gain_per_cost},
    }


def render(result: Dict[str, Any]) -> str:
    """A short plain-text rendering of `next_observation`'s result."""
    lines = ["Posterior: " + ", ".join(f"{h['id']} {h['posterior']:.3f}" for h in result["hypotheses"])
             + f" (uncertainty {result['uncertainty_bits']:.2f} bits)"]
    st = result["stop"]
    if st["stop"]:
        lines.append(f"STOP ({st['reason']}): {st['detail']}. Leading hypothesis: {st['top_hypothesis']}.")
    rec = result["recommended"]
    if rec:
        lines.append(f"Next: {rec['candidate']} (cost {rec['cost']} {rec['cost_unit']}".rstrip() + f", gain {rec['expected_gain_bits']} bits). {rec['why']}")
    for d in result["doubts"]:
        a, b = d["between"]
        lines.append(f"Doubt {a} vs {b}: " + (d["how"] if d.get("discriminating_test") else d["needed_observation"]))
    for w in result["warnings"]:
        lines.append("Warning: " + w)
    return "\n".join(lines)
