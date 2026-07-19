#!/usr/bin/env python3
"""
Registry CI: verify changed paths.json entries (sylabis WS3c).

TEMPLATE NOTE: this script ships inside the main sylabis repo under
registry/ as scaffolding for the separate sylabis-registry repository.
It only runs there (see .github/workflows/verify-listing.yml).

For every entry that is new or changed relative to --base:
  1. validate the entry shape (mirrors schema.json; stdlib only),
  2. clone git_url and check out the PINNED commit_sha — a repo whose
     history no longer contains it fails here,
  3. run the sylabis quality gates: compiler.self_test (structural +
     OKF conformance), okf.hash_problems (tamper-evident hashes),
     publish.pii_problems (no learner data), publish.scan_secrets,
     and verify.verify_sources over the bundle's source locators,
  4. on green, write the `verified` badge fields into paths.json
     (verified_at, sylabis_version, checks_passed) for the workflow to
     commit back to the PR branch.

Exit code 0 only when every changed entry verifies.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REQUIRED = ("id", "name", "git_url", "commit_sha", "topic",
             "est_hours", "license", "author")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]*/[a-z0-9][a-z0-9-]*$")
_SHA = re.compile(r"^[0-9a-f]{40}$")


def entry_problems(entry: dict) -> list[str]:
    problems = [f"missing required field: {f}" for f in _REQUIRED
                if not entry.get(f) and entry.get(f) != 0]
    if entry.get("id") and not _ID.match(entry["id"]):
        problems.append("id must be author-scoped: <author-slug>/<path-slug>")
    if entry.get("commit_sha") and not _SHA.match(entry["commit_sha"]):
        problems.append("commit_sha must be a full 40-char lowercase sha")
    if "verified" in entry:
        problems.append("the verified block is CI-written; remove it "
                        "from the PR")
    return problems


def changed_entries(index: dict, base_text: str | None) -> list[dict]:
    if not base_text:
        return list(index["paths"])
    base = {e.get("id"): e for e in json.loads(base_text).get("paths", [])}
    return [e for e in index["paths"] if base.get(e.get("id")) != e]


def clone_pinned(git_url: str, sha: str, dest: Path) -> list[str]:
    proc = subprocess.run(["git", "clone", "--quiet", git_url, str(dest)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        return [f"clone failed: {proc.stderr.strip()}"]
    have = subprocess.run(
        ["git", "-C", str(dest), "cat-file", "-e", f"{sha}^{{commit}}"],
        capture_output=True, text=True)
    if have.returncode != 0:
        return [f"sha mismatch: repo does not contain pinned commit {sha}"]
    co = subprocess.run(["git", "-C", str(dest), "checkout", "--quiet",
                         "--detach", sha], capture_output=True, text=True)
    if co.returncode != 0:
        return [f"checkout of pinned commit failed: {co.stderr.strip()}"]
    # The checks run on the pinned TREE: drop .git so the clone's own
    # metadata cannot trip the dotfile/secret scans.
    shutil.rmtree(dest / ".git")
    return []


def bundle_problems(bundle: Path) -> tuple[list[str], list[str]]:
    """(problems, checks_passed). Imports sylabis here so entry-shape
    errors report even when the install step failed."""
    import yaml

    from sylabis import okf
    from sylabis.compiler import self_test
    from sylabis.publish import pii_problems, scan_secrets
    from sylabis.verify import verify_sources

    problems: list[str] = []
    passed: list[str] = []
    for name, check in (
            ("self_test", lambda: self_test(bundle)),
            ("okf_conformance", lambda: okf.conformance_problems(bundle)),
            ("okf_hashes", lambda: okf.hash_problems(bundle)),
            ("pii_scan", lambda: pii_problems(bundle)),
            ("secret_scan", lambda: scan_secrets(bundle))):
        found = check()
        if found:
            problems += [f"{name}: {p}" for p in found]
        else:
            passed.append(name)

    sources_yaml = bundle / "grader" / "sources.yaml"
    sources = (yaml.safe_load(sources_yaml.read_text()) or {}).get(
        "sources", []) if sources_yaml.exists() else []
    verify_sources(sources)  # annotates in place; never raises
    dead = [s for s in sources
            if s.get("verified") is False
            and s.get("locator_kind") in ("arxiv", "doi", "http")]
    if dead:
        problems += [f"source_locators: {s.get('id')} "
                     f"({s.get('verification')})" for s in dead]
    else:
        passed.append("source_locators")
    return problems, passed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", default="paths.json")
    ap.add_argument("--base", default=None,
                    help="git ref whose paths.json is the baseline "
                         "(e.g. origin/main); omit to verify every entry")
    ap.add_argument("--write", action="store_true",
                    help="write verified badge fields back into --index")
    args = ap.parse_args()

    index_path = Path(args.index)
    index = json.loads(index_path.read_text())
    base_text = None
    if args.base:
        show = subprocess.run(
            ["git", "show", f"{args.base}:{args.index}"],
            capture_output=True, text=True)
        base_text = show.stdout if show.returncode == 0 else None

    failures = 0
    for entry in changed_entries(index, base_text):
        eid = entry.get("id", "<no id>")
        print(f"== {eid}")
        problems = entry_problems(entry)
        checks: list[str] = []
        if not problems:
            with tempfile.TemporaryDirectory() as td:
                bundle = Path(td) / "bundle"
                problems = clone_pinned(entry["git_url"],
                                        entry["commit_sha"], bundle)
                if not problems:
                    checks = ["pinned_sha"]
                    more, passed = bundle_problems(bundle)
                    problems, checks = more, checks + passed
        if problems:
            failures += 1
            for p in problems:
                print(f"   FAIL {p}")
            continue
        import sylabis
        entry["verified"] = {
            "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sylabis_version": sylabis.__version__,
            "checks_passed": checks,
        }
        print(f"   ok — verified ({', '.join(checks)})")

    if args.write and not failures:
        index_path.write_text(json.dumps(index, indent=2) + "\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
