"""
Source locator verification. Dead URLs silently corrupt the knowledge
bundle, so every http/arxiv/doi locator gets an existence check at harvest
time; 'search:' locators (model was unsure) are flagged, never trusted.
Verification never blocks compilation — flag and continue.

arXiv checks go through the batched export.arxiv.org API: the /abs pages
carry a 15s crawl-delay for bots, while one API query covers every arXiv
source in a compile. DOIs are confirmed by the doi.org redirect status
alone — following to the publisher invites bot-blocks that say nothing
about the DOI itself.
"""
import os
import re
import time
import xml.etree.ElementTree as ET

import httpx

TIMEOUT = 12.0
POLITE_DELAY = 0.5  # seconds between per-URL checks
ARXIV_API = "https://export.arxiv.org/api/query"
_ATOM = "{http://www.w3.org/2005/Atom}"

_ARXIV = re.compile(r"^(?:arxiv:\s*)?(\d{4}\.\d{4,5})(v\d+)?$", re.I)
_DOI = re.compile(r"^(?:doi:\s*|https?://(?:dx\.)?doi\.org/)(10\.\S+)$", re.I)


def _user_agent() -> str:
    ua = "sylabis/0.1 source-verifier"
    mailto = os.environ.get("SYLABIS_CONTACT_MAILTO")
    if mailto:  # Crossref/arXiv polite-pool etiquette
        ua += f" (mailto:{mailto})"
    return ua


def resolve_locator(locator: str) -> tuple[str | None, str]:
    """Map a harvest locator to (check_target, kind).
    kind: 'arxiv' (target = bare id) | 'doi' | 'http' (target = URL)
        | 'search' | 'opaque' (target = None)."""
    loc = (locator or "").strip()
    if loc.lower().startswith("search:"):
        return None, "search"
    m = _ARXIV.match(loc)
    if m:
        return m.group(1) + (m.group(2) or ""), "arxiv"
    m = _DOI.match(loc)
    if m:
        return f"https://doi.org/{m.group(1)}", "doi"
    if loc.startswith(("http://", "https://")):
        return loc, "http"
    return None, "opaque"


def _check_arxiv_batch(client: httpx.Client,
                       id_map: dict[str, list[dict]]) -> None:
    """One batched API query for every arXiv source in the compile.
    A valid ID comes back as an entry with a real title; bad IDs come back
    as 'Error' entries or not at all."""
    try:
        r = client.get(ARXIV_API, params={"id_list": ",".join(id_map),
                                          "max_results": str(len(id_map))},
                       follow_redirects=True)
        r.raise_for_status()
        root = ET.fromstring(r.text)
    except (httpx.HTTPError, ET.ParseError) as e:
        for srcs in id_map.values():
            for s in srcs:
                s["verified"] = False
                s["verification"] = f"network_error:{type(e).__name__}"
        return
    found = set()
    for entry in root.iter(f"{_ATOM}entry"):
        eid = entry.findtext(f"{_ATOM}id") or ""
        title = (entry.findtext(f"{_ATOM}title") or "").strip()
        if not title or title.lower() == "error":
            continue
        for aid in id_map:
            base = aid.split("v")[0]
            if re.search(rf"/abs/{re.escape(base)}(v\d+)?$", eid):
                found.add(aid)
    for aid, srcs in id_map.items():
        ok = aid in found
        for s in srcs:
            s["verified"] = ok
            s["verification"] = ("arxiv_api_found" if ok
                                 else "arxiv_api_not_found")


def _check_url(client: httpx.Client, url: str, kind: str) -> tuple[bool, str]:
    follow = kind != "doi"
    try:
        r = client.head(url, follow_redirects=follow)
        if r.status_code in (403, 405) and follow:
            r = client.get(url, follow_redirects=True)
    except httpx.HTTPError as e:
        return False, f"network_error:{type(e).__name__}"
    if kind == "doi":
        if r.status_code == 200 or 300 <= r.status_code < 400:
            return True, "doi_registered"
        return False, f"http_{r.status_code}"
    if r.status_code < 400:
        return True, f"http_{r.status_code}"
    return False, f"http_{r.status_code}"


def verify_sources(sources: list[dict], enabled: bool = True) -> dict:
    """Annotate each source in place:
      verified:      True | False | None (None = not checkable / skipped)
      verification:  short reason string
    Returns a count summary for logging."""
    summary = {"verified": 0, "unverified": 0, "flagged_search": 0,
               "skipped": 0}
    arxiv_batch: dict[str, list[dict]] = {}
    url_checks: list[tuple[dict, str, str]] = []

    for s in sources:
        target, kind = resolve_locator(s.get("locator", ""))
        s["locator_kind"] = kind
        if kind == "search":
            s["verified"] = False
            s["verification"] = "search_locator_model_unsure"
            summary["flagged_search"] += 1
        elif kind == "opaque" or not enabled:
            s["verified"] = None
            s["verification"] = ("locator_not_checkable" if kind == "opaque"
                                 else "check_skipped")
            summary["skipped"] += 1
        elif kind == "arxiv":
            arxiv_batch.setdefault(target, []).append(s)
        else:
            url_checks.append((s, target, kind))

    if arxiv_batch or url_checks:
        with httpx.Client(timeout=TIMEOUT,
                          headers={"User-Agent": _user_agent()}) as client:
            if arxiv_batch:
                _check_arxiv_batch(client, arxiv_batch)
            for s, url, kind in url_checks:
                ok, reason = _check_url(client, url, kind)
                s["verified"] = ok
                s["verification"] = reason
                time.sleep(POLITE_DELAY)
        for group in (list(arxiv_batch.values()), [[s] for s, _, _ in url_checks]):
            for srcs in group:
                for s in srcs:
                    summary["verified" if s["verified"] else "unverified"] += 1
    return summary
