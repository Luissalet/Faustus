"""How is this feature of a website built? Answers with evidence, not guesses.

Four entry points, all read-only towards Faustus (the browser is a fresh
headless profile with no logins; nothing is stored but screenshots under
``DATA_DIR/web_explain``):

* :func:`explain_feature` -- load a page, perform the actions that trigger the
  feature, and return numbered evidence split into ``observed`` (seen in the
  network log, the DOM, the executed code or the original sources),
  ``inferred`` (a conclusion that names the evidence it rests on) and
  ``unknown`` (what the browser cannot show). The model writes the answer from
  that, citing ``E`` ids.
* :func:`observe_execution` -- the raw observation behind it: requests with
  initiators, DOM mutations, functions that ran during the actions mapped to
  original modules, console and errors, script and package inventory.
* :func:`analyze_bundle` -- one script (or the scripts of one page) without a
  browser: its source map, first-party modules, packages with versions,
  license banners and an optional search in the original sources.
* :func:`trace_location` -- a generated ``script:line:column`` (from a stack
  trace or a request initiator) mapped back to the original file with a
  snippet.

Source maps are what turn minified code into module names; when a site does not
publish them, the report says so under ``unknown`` instead of inventing names.
"""

from __future__ import annotations

import re
import urllib.request
from urllib.parse import urljoin, urlparse

from .capture import WebExplainError, check_url, observe
from .sourcemap import (SourceMap, banner_versions, data_url_text, load_map_text, map_url_from_code,
                        package_of)

__all__ = ["WebExplainError", "explain_feature", "observe_execution", "analyze_bundle", "trace_location"]

MAX_SCRIPT_BYTES = 15_000_000
MAX_MAP_BYTES = 40_000_000
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0 Safari/537.36"

_STOP = set("""the and for with that this from what which when where how does done made built work works feature page site
web website button into have uses using used about there their them they then than will would could should como hace hecho
esta este esto esta una uno unos unas para por con que qué cómo cuando donde dónde funciona funcion función pagina página
sitio boton botón desde entre sobre hacer hecha hechas está están tiene tienen usa usan come comes happen happens
get gets got here into show shows viene vienen pasa ocurre""".split())
_GENERIC = re.compile(r"(?:^|/)(?:utils?|helpers?|i18n|locales?|constants?|polyfills?|vendor|lib/shared)(?:/|\.|$)", re.I)


def _terms(question: str, actions: list[dict] | None) -> list[str]:
    words = re.findall(r"[A-Za-zÀ-ÿ_$][\wÀ-ÿ$-]{3,}", question or "")
    out: list[str] = []
    for w in words:
        lw = w.lower()
        if lw in _STOP or lw in (t.lower() for t in out):
            continue
        out.append(w)
    for a in actions or []:
        t = str(a.get("target") or "") if isinstance(a, dict) else str(a)
        t = re.sub(r"^(text|css|role)=", "", t).strip("#. ")
        if 3 <= len(t) <= 40 and t.lower() not in (x.lower() for x in out):
            out.append(t)
    return out[:8]


def observe_execution(url: str, actions: list[dict] | None = None, wait_ms: int = 1500) -> dict:
    report, _ = observe(url, actions, wait_ms=wait_ms)
    return report


def explain_feature(url: str, question: str, actions: list[dict] | None = None, wait_ms: int = 1500) -> dict:
    """Observe the feature and return evidence (observed / inferred / unknown) for answering ``question``."""
    if not (question or "").strip():
        raise WebExplainError("say what you want to know about the page (question)")
    report, internals = observe(url, actions, wait_ms=wait_ms)
    maps: dict[str, SourceMap] = internals["maps"]
    ev: list[dict] = []

    def add(status: str, claim: str, basis: str, rests_on: list[str] | None = None, **extra) -> str:
        eid = f"E{len(ev) + 1}"
        item = {"id": eid, "status": status, "claim": claim, "basis": basis}
        if rests_on:
            item["rests_on"] = rests_on
        item.update({k: v for k, v in extra.items() if v})
        ev.append(item)
        return eid

    fp = report.get("fingerprint") or {}
    lib_ids = []
    for lib in fp.get("libs") or []:
        version = f" {lib['version']}" if lib.get("version") else ""
        lib_ids.append(add("observed", f"The page runs {lib['name']}{version}.", f"in the live page: {lib['how']}"))
    pk = (report.get("scripts") or {}).get("packages") or []
    if pk:
        shown = ", ".join(p["name"] + (f"@{p['version']}" if p.get("version") else "") for p in pk[:20])
        add("observed", f"Packages bundled in its scripts: {shown}" + (" ..." if len(pk) > 20 else "") + ".",
            "module paths listed in the published source maps")

    did = report.get("actions") or []
    if did:
        add("observed", "Actions performed: " + "; ".join(did) + ".", "headless browser, fresh profile")
    req_ids: list[str] = []
    assets: dict[str, list[dict]] = {}
    for r in (report.get("requests") or {}).get("during_actions") or []:
        if r.get("type") in ("Image", "Font", "Stylesheet", "Media", "Manifest"):
            assets.setdefault(r["type"], []).append(r)
            continue
        claim = f"{r['id']}: {r['method']} {r['url']} -> {r.get('status') or r.get('failed') or 'no response'}"
        if r.get("mime"):
            claim += f" ({r['mime']})"
        ini = r.get("first_party_caller") or r.get("initiator") or {}
        where = None
        if ini.get("original"):
            o = ini["original"]
            where = f"{o['source']}:{o['line']}" + (f" ({o['name']})" if o.get("name") else "")
        elif ini.get("script"):
            where = f"{ini['script']}:{ini.get('line')}" + (f" ({ini['function']})" if ini.get("function") else "")
        if where:
            claim += f", issued from {where}"
        req_ids.append(add("observed", claim + ".", "network log of the action with the initiator stack",
                           body=r.get("body"), response=r.get("response")))
    for kind, rows in assets.items():
        hosts = sorted({urlparse(r["url"]).netloc for r in rows})
        ids = ", ".join(r["id"] for r in rows[:3]) + (f" ... {rows[-1]['id']}" if len(rows) > 3 else "")
        req_ids.append(add("observed", f"{ids}: {len(rows)} {kind.lower()} request(s) from {', '.join(hosts[:3])}.",
                           "network log of the action (assets grouped)"))
    mut = report.get("mutations")
    mut_id = None
    if mut and (mut.get("added") or mut.get("removed") or mut.get("attrs") or mut.get("text")):
        mut_id = add("observed", f"The DOM changed: {mut['added']} nodes added, {mut['removed']} removed, "
                     f"{mut['attrs']} attribute and {mut['text']} text changes.", "MutationObserver during the action",
                     samples=(mut.get("samples") or [])[:12])
    nav = report.get("navigated_to")
    nav_id = add("observed", f"The action navigated to {nav}.", "page URL before and after") if nav else None

    ran = report.get("ran") or {}
    code_ids: list[str] = []
    terms = _terms(question, actions)
    tl = [t.lower() for t in terms]

    def _specific(f: dict) -> tuple:
        path = f["original"]["source"].lower()
        hit = any(t in path or t in (f.get("function") or "").lower() for t in tl)
        return (not hit, bool(_GENERIC.search(path)), -f["calls"])

    own = sorted((f for f in ran.get("first_party") or [] if f.get("original")), key=_specific)
    ran_modules = {f["original"]["source"] for f in own}
    if own:
        listed = "; ".join(f"{f['function']} in {f['original']['source']}:{f['original']['line']} (x{f['calls']})" for f in own[:12])
        code_ids.append(add("observed", f"Site code that ran during the action: {listed}.",
                            "precise JS coverage of the action, mapped through source maps"))
    libs_ran = ran.get("packages") or []
    if libs_ran:
        code_ids.append(add("observed", "Library code that ran during the action: " +
                            ", ".join(f"{p['name']} ({p['functions']} functions)" for p in libs_ran[:10]) + ".",
                            "precise JS coverage mapped to node_modules paths"))
    unm = [f for f in ran.get("first_party") or [] if not f.get("original")]
    if unm:
        listed = "; ".join(f"{f['function']} at {f['generated']['script']}:{f['generated']['line']}" for f in unm[:10])
        code_ids.append(add("observed", f"Code without source map that ran during the action: {listed}.",
                            "precise JS coverage (minified names)"))

    # The question's own words in the original sources and in the traffic.
    hit_ids: list[str] = []
    reported: set[tuple[str, int]] = set()
    for term in terms:
        hits = []
        for m in maps.values():
            hits.extend(h for h in m.search(term, limit=25, first_party_only=True) if (h["source"], h["line"]) not in reported)
        # Modules that ran during the action first; import/export plumbing already sorted last by search().
        hits.sort(key=lambda h: h["source"] not in ran_modules)
        hits = hits[:3]
        reported.update((h["source"], h["line"]) for h in hits)
        if hits:
            where = "; ".join(f"{h['source']}:{h['line']} `{h['text'][:90]}`" for h in hits[:3])
            hit_ids.append(add("observed", f"'{term}' appears in the site's original code: {where}.",
                               "search in sourcesContent of the published source maps"))
        reqs = [r["id"] for r in (report.get("requests") or {}).get("during_actions") or [] if term.lower() in r["url"].lower()]
        if reqs:
            hit_ids.append(add("observed", f"'{term}' appears in the URL of {', '.join(reqs)}.", "network log"))

    # Inferences, each naming what it rests on.
    api = [e for e in ev if e["claim"].startswith("R") and re.search(r"-> 2\d\d", e["claim"])]
    if did:
        if api:
            data_calls = [e for e in api if re.search(r"json|graphql|protobuf|text/plain|event-stream", e["claim"], re.I)]
            if data_calls:
                add("inferred", "The feature asks a server endpoint for data and renders the answer in the browser.",
                    "data responses during the action" + (" plus DOM changes" if mut_id else ""),
                    [e["id"] for e in data_calls[:4]] + ([mut_id] if mut_id else []))
            else:
                add("inferred", "The action loads resources from the server (no data API call identified).",
                    "requests during the action", [e["id"] for e in api[:4]])
        elif nav_id:
            add("inferred", "The feature is a page navigation: the server renders the result page.",
                "URL changed and no API call", [nav_id])
        elif mut_id:
            add("inferred", "The feature runs entirely in the browser: no network traffic during the action, only DOM changes.",
                "empty network log plus DOM mutations", [mut_id] + code_ids[:1])
        if own:
            spec = _specific(own[0])
            top = own[0]["original"]["source"]
            why = "its path or function names match the question" if not spec[0] else (
                "most-called non-utility site code during the action" if not spec[1] else "most-called site code during the action")
            add("inferred", f"The behaviour is most likely implemented in the site's own module {top} ({why}).",
                "coverage ranking of site code", code_ids[:1])
        if any(r.get("type") == "WebSocket" for r in (report.get("requests") or {}).get("during_actions") or []):
            add("inferred", "The feature uses a live socket connection for updates.", "WebSocket created during the action", req_ids)
    for name in ("Next.js", "Nuxt", "Remix", "Astro", "SvelteKit"):
        if any(lib["name"] == name for lib in fp.get("libs") or []):
            add("inferred", f"Pages are rendered by the {name} framework (server rendering plus client hydration is its default).",
                "framework marker in the page", lib_ids)
            break

    unknown: list[str] = []
    if report.get("blocked"):
        unknown.append("Blocked: " + report["blocked"] + ". Nothing behind it was observed; open the page yourself or "
                       "use analyze_web_bundle on its script URLs.")
    unknown.append("What the server does behind each endpoint (database, models, business rules) is not visible from the browser; only requests and responses are.")
    sc = report.get("scripts") or {}
    if not sc.get("with_source_map"):
        unknown.append("The site publishes no source maps: original file and function names are unknown; only minified code positions are available.")
    elif sc.get("without_source_map"):
        unknown.append(f"{sc['without_source_map']} scripts have no source map; their code is reported by minified position only.")
    if not did:
        unknown.append("No actions were given, so the code that handles the feature was not isolated; pass actions (e.g. click the button) to watch it run.")
    elif not req_ids and not mut_id and not nav_id:
        unknown.append("The actions produced no network traffic, DOM change or navigation; the feature may need a login, a different target or more wait time.")
    if any(a.startswith("FAILED") for a in did):
        unknown.append("An action failed (see actions); later steps were not observed.")

    return {
        "url": report["url"], "final_url": report.get("final_url"), "title": report.get("title"),
        "question": question, "terms_searched": terms,
        "evidence": ev, "unknown": unknown,
        "screenshots": report.get("screenshots"),
        "errors": report.get("errors"), "notes": report.get("notes"),
        "how_to_answer": "Answer the question from the evidence only; cite E ids; keep inferred claims marked as such and "
                         "state the unknowns. Use trace_web_source_location for any generated position you need explained.",
    }


# -- static bundle analysis -----------------------------------------------------------------------------------------
def _get(url: str, limit: int) -> tuple[bytes, dict]:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=25) as resp:  # noqa: S310 - http(s) checked by the caller
        data = resp.read(limit + 1)
        headers = {k.lower(): v for k, v in resp.headers.items()}
    if len(data) > limit:
        raise WebExplainError(f"{url} is larger than {limit // 1_000_000} MB")
    return data, headers


def _load_map(code: str, headers: dict, script_url: str) -> tuple[SourceMap | None, str | None, str | None]:
    """``(map, map_url, problem)`` for a script."""
    ref = headers.get("sourcemap") or headers.get("x-sourcemap")
    map_url = urljoin(script_url, ref) if ref else map_url_from_code(code, script_url)
    if not map_url:
        return None, None, "no sourceMappingURL comment or SourceMap header"
    try:
        inline = data_url_text(map_url)
        if inline is not None:
            return SourceMap(load_map_text(inline), "inline"), "inline", None
        check_url(map_url)
        data, _ = _get(map_url, MAX_MAP_BYTES)
        return SourceMap(load_map_text(data.decode("utf-8", "replace")), map_url), map_url, None
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        return None, map_url, f"map not readable: {str(exc).splitlines()[0][:160]}"


def _bundle_report(script_url: str, search: str | None) -> dict:
    data, headers = _get(script_url, MAX_SCRIPT_BYTES)
    code = data.decode("utf-8", "replace")
    smap, map_url, problem = _load_map(code, headers, script_url)
    out: dict = {"script": script_url, "bytes": len(data), "source_map": map_url if smap else None}
    if problem:
        out["source_map_problem"] = problem
    banners = banner_versions(code)
    if banners:
        out["license_banners"] = banners
    if smap:
        own = smap.first_party()
        out["sources_total"] = len(smap.sources)
        out["first_party_modules"] = own[:80]
        out["first_party_total"] = len(own)
        out["packages"] = smap.packages()[:60]
        out["has_original_text"] = any(c for c in smap.contents)
    if search:
        if smap and any(c for c in smap.contents):
            out["search"] = {"term": search, "where": "original sources", "hits": smap.search(search, limit=12)}
        else:
            hits = []
            for m in re.finditer(re.escape(search), code, re.I):
                line = code.count("\n", 0, m.start()) + 1
                col = m.start() - (code.rfind("\n", 0, m.start()) + 1)
                hits.append({"line": line, "column": col, "context": code[max(0, m.start() - 100): m.end() + 100]})
                if len(hits) >= 8:
                    break
            out["search"] = {"term": search, "where": "minified bundle", "hits": hits}
    return out


_SCRIPT_SRC = re.compile(r"<script\b[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.I)
_MODULE_PRELOAD = re.compile(r"<link\b[^>]*rel\s*=\s*[\"']modulepreload[\"'][^>]*href\s*=\s*[\"']([^\"']+)[\"']", re.I)


def analyze_bundle(url: str, search: str | None = None, max_scripts: int = 6) -> dict:
    """A script URL, or a page URL whose scripts are analysed (up to ``max_scripts``)."""
    url = check_url(url)
    data, headers = _get(url, MAX_SCRIPT_BYTES)
    ctype = headers.get("content-type", "")
    if "html" not in ctype and not data[:200].lstrip().lower().startswith((b"<!doctype", b"<html")):
        return _bundle_report(url, search)
    html = data.decode("utf-8", "replace")
    srcs = list(dict.fromkeys(urljoin(url, s) for s in _SCRIPT_SRC.findall(html) + _MODULE_PRELOAD.findall(html)))
    srcs = [s for s in srcs if s.startswith(("http://", "https://"))]
    reports = []
    for s in srcs[: max(1, min(int(max_scripts), 12))]:
        try:
            reports.append(_bundle_report(s, search))
        except Exception as exc:  # noqa: BLE001
            reports.append({"script": s, "error": str(exc).splitlines()[0][:200]})
    pk: dict[str, str | None] = {}
    for r in reports:
        for p in r.get("packages") or []:
            if p["name"] not in pk or (p.get("version") and not pk[p["name"]]):
                pk[p["name"]] = p.get("version")
    return {"page": url, "scripts_found": len(srcs), "analysed": reports,
            "packages": [{"name": k, "version": v} for k, v in sorted(pk.items())],
            "note": "Only scripts referenced in the HTML; code loaded later (lazy chunks) shows up in observe_web_execution."}


def trace_location(script_url: str, line: int, column: int = 0, context: int = 4) -> dict:
    """Map a generated ``line`` (1-based) / ``column`` (0-based) of a script to its original source."""
    script_url = check_url(script_url)
    data, headers = _get(script_url, MAX_SCRIPT_BYTES)
    code = data.decode("utf-8", "replace")
    smap, map_url, problem = _load_map(code, headers, script_url)
    out: dict = {"script": script_url, "line": int(line), "column": int(column)}
    if smap:
        hit = smap.lookup(int(line), int(column))
        if hit:
            pkg = package_of(hit["source"])
            out["original"] = {"source": hit["source"], "line": hit["line"], "column": hit["column"], "name": hit["name"],
                               "package": pkg[0] if pkg else None, "package_version": pkg[1] if pkg else None}
            snip = smap.snippet(hit["index"], hit["line"], max(0, min(int(context), 20)))
            if snip:
                out["snippet"] = snip
            else:
                out["note"] = "the map has no sourcesContent: file and line are known, the text is not"
            return out
        out["note"] = "the source map has no mapping at that position"
    else:
        out["note"] = problem or "no source map"
    rows = code.splitlines()
    if 1 <= int(line) <= len(rows):
        row = rows[int(line) - 1]
        c = max(0, int(column))
        out["generated_context"] = row[max(0, c - 160): c + 160]
    out["source_map"] = map_url
    return out
