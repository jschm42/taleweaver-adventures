#!/usr/bin/env python3
"""Record the published release bytes for one adventure and regenerate `catalog.json`.

This script does not own the catalogue. `scripts/build_catalog.py` is its only
producer; this is the *transport* that runs at release time, after an asset has
been uploaded:

1. **Prove** the bytes on the release are the bytes being recorded (`gh` read-back
   of the asset digest and size, `--no-verify` to skip).
2. **Record** them into `catalog.lock.json` via `build_catalog.py --record`.
3. **Regenerate** `catalog.json` from the lock plus the source tree.
4. **Land** both files on the default branch through an auto-merged PR, because
   `main` is protected by a ruleset that requires a pull request.

Why the values come from the release and not from the build
-----------------------------------------------------------
The catalogue is what clients verify a download against, so it may only state
hashes that are actually downloadable. Reading the digest back from the release
API *after* the upload is the only step that proves that: the built bundle and
the stored asset are usually identical, but "usually" is not a property a
verification gate can rely on.

Usage
-----
    # apply (verify, record, regenerate, PR + auto-merge) - used by the release job
    scripts/sync_catalog.py --repo jschm42/taleweaver-adventures \\
        --branch main --slug kitchen-crisis --entry dist/catalog-entry.json

    # plan only: verify what would be recorded, print the delta, write nothing
    scripts/sync_catalog.py ... --dry-run

    # offline (tests, manual repair): write lock + catalogue in the working tree
    scripts/sync_catalog.py --entry dist/catalog-entry.json --local

Exit codes
----------
0  recorded (and merged, when a repo was given)
1  invalid input, or the release does not carry the bytes the entry claims
3  branch + PR were created but the merge did not complete (needs a human)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import build_catalog
from build_catalog import CatalogError

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK_NAME = build_catalog.LOCK_PATH.name
CATALOG_NAME = build_catalog.CATALOG_PATH.name


class SyncError(Exception):
    """The recording or landing step could not be completed."""


# --- external tools -------------------------------------------------------------


def _run(cmd: list[str], *, cwd: Path | None = None, quiet: bool = True) -> str:
    proc = subprocess.run(cmd, cwd=str(cwd or REPO_ROOT), capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise SyncError(f"{cmd[0]} {' '.join(cmd[1:3])} failed: {detail[0] if detail else 'unknown error'}")
    return proc.stdout if not quiet else proc.stdout.strip()


def _gh(args: list[str]) -> str:
    binary = shutil.which("gh")
    if not binary:
        raise SyncError("gh CLI not found on PATH")
    return _run([binary, *args])


def _git(*args: str) -> str:
    if not shutil.which("git"):
        raise SyncError("git not found on PATH")
    return _run(["git", *args])


def published_asset(repo: str, tag: str, asset: str) -> dict[str, Any] | None:
    """Return `{sha256, size}` for a release asset, or None if it is not there."""
    raw = _gh(
        [
            "api",
            f"repos/{repo}/releases/tags/{tag}",
            "--jq",
            '[.assets[] | {name, digest, size}]',
        ]
    )
    assets = json.loads(raw or "[]")
    match = next((item for item in assets if item.get("name") == asset), None)
    if match is None:
        return None
    return {
        "sha256": (match.get("digest") or "").removeprefix("sha256:"),
        "size": match.get("size"),
        "name": match.get("name"),
    }


def verify_published(repo: str, entry: dict[str, Any], slug: str, tag: str) -> list[str]:
    """Every digest the catalogue is about to advertise must exist on the release.

    This is the step that closes the gap the build cannot close: a generator can
    hash a file, only the release can say what a client will actually download.
    """
    problems: list[str] = []
    asset = f"{slug}-{entry['version']}.adz"
    published = published_asset(repo, tag, asset)
    if published is None:
        return [f"{tag} has no asset {asset} - nothing was published, so nothing may be recorded"]
    if published["sha256"] != entry["sha256"]:
        problems.append(
            f"{asset}: release carries sha256 {published['sha256'][:12]}… "
            f"({published['size']} B) but the entry says {entry['sha256'][:12]}… "
            f"({entry['size_bytes']} B)"
        )
    if published["size"] != entry["size_bytes"]:
        problems.append(
            f"{asset}: release carries {published['size']} bytes, entry says {entry['size_bytes']}"
        )

    cover_url = entry.get("cover_url") or ""
    if cover_url:
        cover_name = cover_url.rsplit("/", 1)[-1]
        cover = published_asset(repo, tag, cover_name)
        if cover is None:
            problems.append(f"{tag} has no cover asset {cover_name} that the entry advertises")
        elif cover["sha256"] != entry.get("cover_sha256"):
            problems.append(
                f"{cover_name}: release carries sha256 {cover['sha256'][:12]}… but the entry "
                f"says {str(entry.get('cover_sha256'))[:12]}…"
            )
    return problems


# --- landing ----------------------------------------------------------------------


def _pr_body(slug: str, version: str, tag: str, asset: str, delta: list[str]) -> str:
    lines = [
        f"Automated catalog record for `{slug}` v{version}.",
        "",
        f"- release: `{tag}`, asset `{asset}`",
        "- sha256/size were read back from the release API, not from the build",
        f"- `{LOCK_NAME}` records the published bytes; `{CATALOG_NAME}` is regenerated "
        f"from that lock plus the source tree by `scripts/build_catalog.py`",
    ]
    if delta:
        lines += ["", "Changes:", "", *[f"- {item}" for item in delta]]
    lines += ["", "Merged automatically by the release workflow."]
    return "\n".join(lines)


def land(repo: str, branch: str, *, slug: str, version: str, tag: str,
         asset: str, delta: list[str], run_id: str) -> str:
    """Commit lock + catalogue on a short-lived branch, open a PR, merge it."""
    pr_branch = f"catalog/{slug}-{version}-{run_id}"
    message = f"chore(catalog): record published bytes for {slug} v{version}"

    _git("checkout", "-B", pr_branch)
    _git("add", LOCK_NAME, CATALOG_NAME)
    # Nothing to land is not an error: re-running a release must not fail on an
    # already-correct catalogue.
    if _run(["git", "diff", "--cached", "--name-only"]) == "":
        print("nothing to land: the catalogue already records these bytes")
        return ""
    _git("commit", "-m", message, "-m",
         "Regenerated by scripts/build_catalog.py from catalog.lock.json.")
    _git("push", "origin", f"HEAD:refs/heads/{pr_branch}")

    pr_url = _gh(
        ["pr", "create", "--repo", repo, "--base", branch, "--head", pr_branch,
         "--title", message, "--body", _pr_body(slug, version, tag, asset, delta)]
    ).strip().splitlines()[-1].strip()

    merge_error = None
    for strategy in ("--merge", "--squash", "--rebase"):
        try:
            _gh(["pr", "merge", "--repo", repo, pr_branch, strategy, "--delete-branch"])
            merge_error = None
            break
        except SyncError as exc:
            merge_error = str(exc)

    # Read back: a created PR is not evidence that the branch moved.
    landed_sha = _gh(["api", f"repos/{repo}/contents/{CATALOG_NAME}?ref={branch}", "--jq", ".sha"]).strip()
    committed_sha = _run(["git", "hash-object", CATALOG_NAME])
    if merge_error or not landed_sha:
        raise SyncError(
            f"catalog PR was created ({pr_url or 'no url'}) but did not land on {branch}"
            + (f": {merge_error}" if merge_error else "")
        )
    print(f"landed {pr_url} on {branch} ({CATALOG_NAME} blob {landed_sha[:12]}…)")
    if landed_sha != committed_sha:
        print(
            f"notice: {branch} now carries a different {CATALOG_NAME} blob "
            f"({landed_sha[:12]}… vs local {committed_sha[:12]}…) - "
            f"someone else merged a catalogue change meanwhile",
            file=sys.stderr,
        )
    return pr_url


# --- CLI --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_catalog.py",
        description="Record published release bytes into catalog.lock.json and regenerate catalog.json.",
    )
    parser.add_argument("--entry", type=Path, required=True, help="catalog-entry.json from the build")
    parser.add_argument("--slug", help="adventure slug (defaults to the entry's slug)")
    parser.add_argument("--repo", help="owner/repo to verify against and land the PR in")
    parser.add_argument("--branch", default="main", help="target branch (default: main)")
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"),
                        help="run id for the branch name")
    parser.add_argument("--dry-run", action="store_true", help="verify and print the delta; write nothing")
    parser.add_argument("--local", action="store_true",
                        help="offline: record into the working tree, no verification, no PR")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip the release read-back (only for a release being repaired by hand)")
    parser.add_argument("--no-merge", action="store_true",
                        help="open the PR but leave it for a human")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.repo and not args.local:
        print("error: give --repo (verify + land) or --local (offline write)", file=sys.stderr)
        return 1

    try:
        entry, slug, version = build_catalog._validated_entry(args.entry)
        if args.slug and args.slug != slug:
            raise CatalogError(f"entry names slug {slug!r} but --slug says {args.slug!r}")
        tag = build_catalog._entry_tag(slug, version, entry)
        asset = f"{slug}-{version}.adz"

        if args.repo and not args.no_verify:
            problems = verify_published(args.repo, entry, slug, tag)
            if problems:
                print(f"refusing to record: {tag} does not carry these bytes", file=sys.stderr)
                for problem in problems:
                    print(f"  {problem}", file=sys.stderr)
                return 1
            print(f"verified {asset} and its cover against the release API ({tag})")

        if args.dry_run:
            lock = build_catalog.load_lock()
            current, _, _ = build_catalog.build_catalog(lock)
            record = build_catalog.compose_record(entry, slug, version, tag)
            would = json.loads(json.dumps(lock))
            would["releases"][slug] = record
            would["releases"] = dict(sorted(would["releases"].items()))
            after, _, _ = build_catalog.build_catalog(would)
            delta = build_catalog.diff_catalog(current, after)
            print(f"dry run: would record {slug} v{version} ({entry['sha256'][:12]}…, "
                  f"{entry['size_bytes']} B)")
            for item in delta:
                print(f"  would change: {item}")
            if not delta:
                print("  nothing would change")
            return 0

        before = build_catalog.load_lock()
        before_catalog, _, _ = build_catalog.build_catalog(before)
        lock = build_catalog.record_entry(args.entry)
        generated, warnings, _ = build_catalog.build_catalog(lock)
        build_catalog.CATALOG_PATH.write_text(build_catalog.dumps(generated), encoding="utf-8")
        print(f"wrote {CATALOG_NAME}: {len(generated['adventures'])} adventures")
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)

        delta = build_catalog.diff_catalog(before_catalog, generated)
        if args.local:
            for item in delta:
                print(f"  changed: {item}")
            return 0

        if args.no_merge:
            print("--no-merge: files written, PR will not be opened by this run", file=sys.stderr)
        url = land(args.repo, args.branch, slug=slug, version=version, tag=tag, asset=asset,
                   delta=delta, run_id=args.run_id) if not args.no_merge else ""
        if not url:
            return 0
        return 0
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
