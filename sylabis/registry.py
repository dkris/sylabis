"""
Registry client (Primetime plan, Phase 1 WS3c) — `sy paths`.

The registry is a static index, crates.io-sparse-index style: one
paths.json in the sylabis-registry repo, served raw over GitHub Pages —
no API server. This module is the CLIENT side: fetch the index (cached
under $SYLABIS_HOME with a daily TTL and ETag revalidation), search it,
and resolve a listing to journey.attach() at the listing's PINNED
commit SHA. The registry is metadata over existing attach plumbing.

Reciprocity is STATUS, not access (the give-to-get decision): cloning
from the registry is free and anonymous forever; publishing unlocks the
public journey page, the verified badge, attribution (derived_from),
and ranking weight. There is NO hard gate anywhere in this module.

NO telemetry: fetching the index sends nothing but the GET itself —
no identifiers, no usage pings — and a file:// or local-path index
(offline tests, mirrors) never touches the network at all.
"""
import json
import os
import time
from pathlib import Path

import yaml

from . import journey
from .errors import RegistryError

# The public index. Override with $SYLABIS_REGISTRY_URL (or the url=
# parameter): any http(s) URL, a file:// URL, or a plain local path.
DEFAULT_REGISTRY_URL = ("https://sylabis-registry.github.io/"
                        "sylabis-registry/paths.json")

SCHEMA_VERSION = 1
CACHE_FILE = ".registry-cache.json"
CACHE_TTL = 24 * 60 * 60  # one day, in seconds
FETCH_TIMEOUT = 12.0

# Repo naming convention (Homebrew-tap style) and author-scoped listing
# ids — documented in docs/registry/schema.md.
REPO_PREFIX = "sylabis-path-"


def registry_url(override: str | None = None) -> str:
    return (override or os.environ.get("SYLABIS_REGISTRY_URL")
            or DEFAULT_REGISTRY_URL)


# ------------------------------------------------------------------ fetching

def _default_fetch(url: str, etag: str | None) -> tuple[str | None, str | None]:
    """Fetch the index text. Returns (text, etag); text None means the
    server said 304 Not Modified for `etag`. file:// URLs and plain
    local paths read straight from disk (no network, no ETag)."""
    if url.startswith("file://"):
        path = Path(url[len("file://"):])
    elif not url.startswith(("http://", "https://")):
        path = Path(url).expanduser()
    else:
        path = None
    if path is not None:
        try:
            return path.read_text(), None
        except OSError as e:
            raise RegistryError(f"registry index unreadable: {url}: {e}")

    import httpx  # runtime dep; imported here so offline paths never need it
    headers = {"If-None-Match": etag} if etag else {}
    try:
        r = httpx.get(url, headers=headers, timeout=FETCH_TIMEOUT,
                      follow_redirects=True)
    except httpx.HTTPError as e:
        raise RegistryError(
            f"registry fetch failed: {url}: {type(e).__name__}")
    if r.status_code == 304:
        return None, etag
    if r.status_code != 200:
        raise RegistryError(
            f"registry fetch failed: {url}: HTTP {r.status_code}")
    return r.text, r.headers.get("etag")


def _parse_index(text: str, url: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise RegistryError(f"registry index at {url} is not valid JSON: {e}")
    if not isinstance(data, dict) or not isinstance(data.get("paths"), list):
        raise RegistryError(f"registry index at {url} is malformed: "
                            "expected an object with a 'paths' list")
    if data.get("schema") != SCHEMA_VERSION:
        raise RegistryError(
            f"registry index at {url} uses schema "
            f"{data.get('schema')!r}; this sylabis understands "
            f"schema {SCHEMA_VERSION} — upgrade sylabis")
    if not all(isinstance(e, dict) for e in data["paths"]):
        raise RegistryError(f"registry index at {url} is malformed: "
                            "every paths entry must be an object")
    return data


def _read_cache(cache_path: Path) -> dict | None:
    try:
        data = json.loads(cache_path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def fetch_index(home_dir: Path | str, url: str | None = None, *,
                now=time.time, fetcher=None, force: bool = False) -> dict:
    """The parsed index, from the on-disk cache when fresh (daily TTL),
    otherwise fetched — with ETag revalidation for http(s) indexes, so a
    304 refreshes the TTL without re-downloading. `now` and `fetcher`
    are injectable for offline tests. Raises RegistryError on fetch or
    parse failure."""
    url = registry_url(url)
    home_dir = Path(home_dir)
    cache_path = home_dir / CACHE_FILE
    cached = _read_cache(cache_path)
    if cached is not None and cached.get("url") != url:
        cached = None  # a different index; never serve its entries
    ts = now()
    if cached is not None and not force:
        if 0 <= ts - float(cached.get("fetched_at", 0)) < CACHE_TTL:
            return cached["index"]

    etag = cached.get("etag") if cached else None
    text, new_etag = (fetcher or _default_fetch)(url, etag)
    if text is None:  # 304 — the cached index is still current
        if cached is None:
            raise RegistryError(f"registry fetch failed: {url}: "
                                "not-modified response with no cached index")
        index = cached["index"]
    else:
        index = _parse_index(text, url)
    try:
        home_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(
            {"schema": 1, "url": url, "etag": new_etag,
             "fetched_at": ts, "index": index}, indent=2))
    except OSError:
        pass  # an unwritable home only costs a re-fetch next time
    return index


# ----------------------------------------------------------- search and get

def search(home_dir: Path | str, query: str, url: str | None = None,
           **fetch_kw) -> list[dict]:
    """Listings whose id, name, topic, or assumed-knowledge tags match
    `query` (case-insensitive substring). An empty query lists all."""
    index = fetch_index(home_dir, url, **fetch_kw)
    q = (query or "").strip().lower()
    hits = []
    for e in index["paths"]:
        hay = [str(e.get("id", "")), str(e.get("name", "")),
               str(e.get("topic", ""))]
        hay += [str(t) for t in (e.get("assumed_knowledge") or [])]
        if not q or any(q in h.lower() for h in hay):
            hits.append(e)
    return hits


def get(home_dir: Path | str, path_id: str, url: str | None = None,
        **fetch_kw) -> Path:
    """Resolve a listing id to journey.attach() of its git_url at the
    PINNED commit_sha. RegistryError for a missing/unpinned listing;
    AttachError('sha mismatch...') when the repo no longer contains the
    pinned commit (post-listing tampering)."""
    index = fetch_index(home_dir, url, **fetch_kw)
    entry = next((e for e in index["paths"] if e.get("id") == path_id), None)
    if entry is None:
        near = [e.get("id") for e in search(home_dir, path_id, url, **fetch_kw)]
        hint = f" (did you mean: {', '.join(near[:3])}?)" if near else ""
        raise RegistryError(f"no path {path_id!r} in the registry index{hint}")
    git_url = str(entry.get("git_url") or "").strip()
    if not git_url:
        raise RegistryError(f"listing {path_id!r} has no git_url")
    sha = str(entry.get("commit_sha") or "").strip()
    if not sha:
        raise RegistryError(
            f"listing {path_id!r} has no pinned commit_sha — refusing to "
            "attach unpinned registry content")
    return journey.attach(home_dir, git_url, pin_sha=sha)


# ------------------------------------------------- author-side listing entry

def listing_entry(template_dir: Path | str, git_url: str = "",
                  commit_sha: str = "",
                  derived_from: str | None = None) -> dict:
    """A ready-to-paste paths.json entry for a published template
    (`sy publish --list`). Computing the pinned commit_sha is the
    author's step — it exists only after they push the template — so it
    ships empty unless provided. The verified badge fields are NEVER
    author-set; registry CI writes them on a green listing PR."""
    template_dir = Path(template_dir)
    cy = template_dir / "course.yaml"
    if not cy.exists():
        raise RegistryError(f"{template_dir} is not a published template "
                            "(no course.yaml at its root)")
    manifest = yaml.safe_load(cy.read_text()) or {}
    meta = manifest.get("meta") or {}
    pub = manifest.get("publish") or {}
    title = str(meta.get("title") or template_dir.name)
    slug = journey.slugify(title)
    author = str(pub.get("author") or "").strip()
    handle = journey.slugify(author) if author else "<your-github-handle>"
    entry = {
        "id": f"{handle}/{slug}",
        "name": title,
        "git_url": git_url or f"https://github.com/{handle}/"
                              f"{REPO_PREFIX}{slug}",
        "commit_sha": commit_sha,
        "topic": title,
        "est_hours": sum(m.get("estimated_hours") or 0
                         for m in manifest.get("milestones", [])),
        "license": str(pub.get("license") or ""),
        "assumed_knowledge": [str(t) for t in
                              (manifest.get("assumed_knowledge") or [])],
        "author": author or "<your-github-handle>",
    }
    if derived_from:
        entry["derived_from"] = str(derived_from)
    return entry
