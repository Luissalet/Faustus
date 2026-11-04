"""Conservative source authority: what kind of site a cited URL is, and whether it is an official one.

A research report cites pages of very different weight: a ministry, a university or a vendor's own
documentation against a forum thread, a blog or a shop. This module classifies the HOST of a URL once, with
no network access, so the sources of a report can say so.

The rule that matters is conservatism: ``official`` is True only on a strong signal in the host itself (a
government, military, university or intergovernmental domain, or a ``docs.`` / ``developer.`` subdomain).
Everything else, including a well-known site that merely looks serious, is ``official: False``. A
classification that never says "official" by mistake is worth more than one that catches every case.

Pure stdlib; nothing here raises, a bad URL gives ``unknown``.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import urlsplit

SOURCE_TYPES = ("gov", "edu", "intergovernmental", "docs", "paper", "code", "wiki", "qa", "forum", "blog",
                "shop", "social", "video", "unknown")

# Public-suffix style second levels that mean "government" / "academia" in a country code domain.
_GOV_LABELS = ("gov", "gob", "gouv", "go", "govt", "gv")
_EDU_LABELS = ("edu", "ac")
_INTERGOV = ("europa.eu", "who.int", "un.org", "worldbank.org", "imf.org", "oecd.org", "wto.org", "nato.int",
             "iso.org", "ietf.org", "w3.org", "rfc-editor.org", "unesco.org", "bis.org", "ecb.europa.eu")
# Official bodies whose domain has no structural signal (Spain's gob.es ones are caught by the host
# pattern already). A short curated list, matched as the domain itself or a subdomain, never as a substring.
_GOV_LISTED = ("boe.es", "ine.es", "bde.es", "cnmv.es", "agenciatributaria.es", "aemet.es", "seg-social.es",
               "congreso.es", "senado.es", "poderjudicial.es", "tribunalconstitucional.es")
_PAPER = ("arxiv.org", "doi.org", "pubmed.ncbi.nlm.nih.gov", "semanticscholar.org", "openreview.net",
          "biorxiv.org", "medrxiv.org", "aclanthology.org", "jmlr.org")
_CODE = ("github.com", "gitlab.com", "bitbucket.org", "huggingface.co", "pypi.org", "npmjs.com", "crates.io",
         "codeberg.org", "sourcehut.org", "git.sr.ht")
_WIKI = ("wikipedia.org", "wikimedia.org", "wiktionary.org", "wikidata.org", "fandom.com", "wikihow.com")
_QA = ("stackoverflow.com", "stackexchange.com", "superuser.com", "serverfault.com", "askubuntu.com", "quora.com")
_FORUM = ("reddit.com", "news.ycombinator.com", "lobste.rs", "discourse.org", "forum.", "forums.", "community.",
          "bbs.")
_BLOG = ("medium.com", "substack.com", "dev.to", "blogspot.com", "wordpress.com", "hashnode.dev", "ghost.io",
         "tumblr.com", "blog.", "pages.dev", "github.io")
_SHOP = ("amazon.", "ebay.", "aliexpress.", "etsy.com", "walmart.com", "pccomponentes.com", "shop.", "store.",
         "shopify.com", "temu.com")
_SOCIAL = ("x.com", "twitter.com", "facebook.com", "instagram.com", "linkedin.com", "tiktok.com", "threads.net",
           "mastodon.social", "bsky.app")
_USER_CONTENT = ("docs.google.com", "drive.google.com", "sites.google.com", "notion.site", "notion.so",
                 "gitbook.io", "netlify.app", "vercel.app", "herokuapp.com", "web.app", "firebaseapp.com",
                 "glitch.me", "repl.co", "onrender.com", "fly.dev", "workers.dev")
_VIDEO = ("youtube.com", "youtu.be", "vimeo.com", "twitch.tv", "dailymotion.com")


def _host(url: Any) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    try:
        host = urlsplit(raw).hostname or ""
    except ValueError:
        return ""
    return host.lower().rstrip(".")


def _is_or_under(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def _matches(host: str, patterns: Iterable[str]) -> str:
    for p in patterns:
        if p.endswith("."):  # a leading label such as "forum." or a brand stem such as "amazon."
            if host.startswith(p) or ("." + p) in host:
                return p
        elif _is_or_under(host, p):
            return p
    return ""


def classify_source(url: Any) -> Dict[str, Any]:
    """``{"source_type", "official", "signal", "host"}`` for a URL, from its host alone."""
    host = _host(url)
    if not host or "." not in host:
        return {"source_type": "unknown", "official": False, "signal": "", "host": host}
    labels = host.split(".")
    tld, second = labels[-1], labels[-2] if len(labels) > 1 else ""

    def out(kind: str, official: bool, signal: str) -> Dict[str, Any]:
        return {"source_type": kind, "official": official, "signal": signal, "host": host}

    # Strong, structural signals first: they are the only way to be "official" by domain.
    if tld in ("gov", "mil"):
        return out("gov", True, "." + tld)
    if len(labels) >= 3 and second in _GOV_LABELS and len(tld) == 2:
        return out("gov", True, f".{second}.{tld}")
    if tld == "edu":
        return out("edu", True, ".edu")
    if len(labels) >= 3 and second in _EDU_LABELS and len(tld) == 2:
        return out("edu", True, f".{second}.{tld}")
    hit = _matches(host, _INTERGOV)
    if hit:
        return out("intergovernmental", True, hit)
    hit = _matches(host, _GOV_LISTED)
    if hit:
        return out("gov", True, hit)
    # Documentation: a docs./developer. subdomain is the vendor's own, so it is official.
    # ...unless the host is a platform that hands anyone a subdomain or a document (docs.google.com is a
    # shared document, docs.someone.github.io a project page): there the prefix proves nothing.
    open_platform = bool(_matches(host, _USER_CONTENT + _CODE + _WIKI + _QA + _SOCIAL + _VIDEO + _BLOG))
    if labels[0] in ("docs", "developer", "developers") and len(labels) >= 3 and not open_platform:
        return out("docs", True, labels[0] + ".")
    if _is_or_under(host, "readthedocs.io") or _is_or_under(host, "readthedocs.org"):
        return out("docs", False, "readthedocs")
    hit = _matches(host, _PAPER)
    if hit:
        return out("paper", False, hit)
    # Open platforms host anyone's content: classified, never official.
    for kind, patterns in (("code", _CODE), ("wiki", _WIKI), ("qa", _QA), ("social", _SOCIAL),
                           ("video", _VIDEO), ("forum", _FORUM), ("blog", _BLOG), ("shop", _SHOP)):
        hit = _matches(host, patterns)
        if hit:
            return out(kind, False, hit)
    return out("unknown", False, "")


def annotate(sources: Iterable[Any]) -> List[Dict[str, Any]]:
    """Each source (a dict with ``url`` or a bare URL) with its classification, in order."""
    result: List[Dict[str, Any]] = []
    for s in sources or []:
        url = s.get("url") if isinstance(s, dict) else s
        info = classify_source(url)
        item = dict(s) if isinstance(s, dict) else {"url": str(s)}
        item.update({"source_type": info["source_type"], "official": info["official"]})
        result.append(item)
    return result


def summarise(sources: Iterable[Any]) -> Dict[str, Any]:
    """How a set of cited sources splits: count per type and how many are official."""
    counts: Dict[str, int] = {}
    official = 0
    total = 0
    for s in sources or []:
        url = s.get("url") if isinstance(s, dict) else s
        info = classify_source(url)
        total += 1
        counts[info["source_type"]] = counts.get(info["source_type"], 0) + 1
        official += 1 if info["official"] else 0
    return {"total": total, "official": official, "by_type": counts}


def stamp(entry: Dict[str, Any]) -> Tuple[str, bool]:
    """Set ``source_type`` / ``official`` on a source dict in place unless it already carries a real type."""
    info = classify_source(entry.get("url"))
    existing = str(entry.get("source_type") or "").strip()
    if not existing or existing == "unknown":
        entry["source_type"] = info["source_type"]
    entry["official"] = bool(info["official"])
    return entry["source_type"], entry["official"]


def stamp_missing(sources: Any) -> Any:
    """Saved reports from before the classification carry no ``official`` flag: classify those sources by
    host when they are served (cheap, no network, the saved file is left alone). Sources that already carry
    the flag, and anything that is not a list of dicts, are returned untouched."""
    if not isinstance(sources, list):
        return sources
    for entry in sources:
        if isinstance(entry, dict) and "official" not in entry:
            stamp(entry)
    return sources