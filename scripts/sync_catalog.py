#!/usr/bin/env python3
"""Keep the distribution repository's ``catalog.json`` in sync with a release.

The release workflow builds a ``.adz`` bundle, publishes it as a release asset and
knows the exact ``sha256``/``size_bytes`` of that asset.  ``catalog.json`` is the
document clients verify downloads against, so it has to carry those exact values
or verification fails.

This script merges one already-built entry (``catalog-entry.json``, produced by
the release job) into ``catalog.json`` on the distribution repository's default
branch.

Design notes
------------
* **Merge, never regenerate.** Only the fields the release actually proves are
  overwritten (version, sha256, size_bytes, download_url, released_at, cover_url,
  cover_sha256).  Editorial fields (id, title, teaser, tags, license, ...) are
  owned by humans and are copied through untouched.
* **Idempotent.** When the only difference would be the timestamp, nothing is
  written and the script exits 0.  Re-running a release never produces an empty
  ``chore(catalog)`` commit.
* **Ruleset-friendly.** ``main`` requires a pull request, so the change lands as
  an auto-merged PR from a short-lived ``catalog/*`` branch instead of a direct
  push.  This is deliberately *not* punching a bypass hole into the branch
  protection.
* **Auditable.** No secrets are printed.  Every API call goes through ``gh``,
  which uses the ambient ``GH_TOKEN``.

Usage
-----
    # apply (fetch main, merge, PR + auto-merge) — used by the workflow
    scripts/sync_catalog.py --repo jschm42/taleweaver-adventures \\
        --slug kitchen-crisis --entry dist/catalog-entry.json

    # plan only: fetch, merge, print.  Zero writes.
    scripts/sync_catalog.py ... --dry-run

    # offline: merge into a local file (tests, manual repair)
    scripts/sync_catalog.py --catalog catalog.json --entry dist/catalog-entry.json
    scripts/sync_catalog.py --catalog catalog.json --entry ... --dry-run

Exit codes
----------
0  catalog is up to date (no change) or the change landed
1  invalid input, broken JSON, or an API failure
3  branch + PR were created but the merge did not complete (needs a human)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import Any

#: Fields the release build proves and therefore may overwrite.
SYNCED_KEYS = (
    "version",
    "sha256",
    "size_bytes",
    "download_url",
    "released_at",
    "cover_url",
    "cover_sha256",
)

#: Fields that only exist when the adventure actually ships cover artwork.
#: The release job drops both of them for cover-less adventures, so the merge
#: must accept an entry without them (and remove stale values from the catalog).
OPTIONAL_SYNCED_KEYS = ("cover_url", "cover_sha256")

#: Fields every catalog entry has to carry for a client to verify a download.
REQUIRED_SYNCED_KEYS = tuple(key for key in SYNCED_KEYS if key not in OPTIONAL_SYNCED_KEYS)

#: Entry-level fields that change on every run and must not count as a diff.
VOLATILE_ENTRY_KEYS = ("released_at",)

#: Document-level fields with the same property.
VOLATILE_DOC_KEYS = ("generated_at",)


class SyncError(Exception):
    """User-facing failure (exit code 1)."""


# --- pure merging -------------------------------------------------------------


def _comparable(doc: dict[str, Any]) -> dict[str, Any]:
    """Deep-ish copy of ``doc`` with volatile fields dropped, for equality tests."""
    clone = json.loads(json.dumps(doc))
    for key in VOLATILE_DOC_KEYS:
        clone.pop(key, None)
    for item in clone.get("adventures", []):
        for key in VOLATILE_ENTRY_KEYS:
            item.pop(key, None)
    return clone


def find_entry(doc: dict[str, Any], slug: str) -> dict[str, Any] | None:
    for item in doc.get("adventures", []):
        if item.get("slug") == slug:
            return item
    return None


def merge_entry(
    doc: dict[str, Any], entry: dict[str, Any], *, now: str | None = None
) -> tuple[dict[str, Any], bool]:
    """Merge ``entry`` into ``doc``.

    Returns ``(merged_document, changed)``.  ``changed`` is ``False`` when the
    merge would only rewrite volatile timestamps; in that case ``doc`` itself is
    returned untouched so the caller writes nothing.
    """
    slug = entry.get("slug")
    if not isinstance(slug, str) or not slug:
        raise SyncError("catalog entry has no slug")

    existing = find_entry(doc, slug)
    if existing is None:
        raise SyncError(
            f"catalog.json has no entry for slug {slug!r}; "
            "add the editorial entry (id/title/teaser/...) before releasing"
        )

    merged = json.loads(json.dumps(doc))
    target = find_entry(merged, slug)
    assert target is not None  # guaranteed by the lookup above

    missing = [key for key in REQUIRED_SYNCED_KEYS if key not in entry]
    if missing:
        raise SyncError(f"catalog entry is missing required field(s): {', '.join(missing)}")

    for key in SYNCED_KEYS:
        if key in entry:
            target[key] = entry[key]
        else:
            # cover_* is absent when the adventure has no cover artwork at all.
            target.pop(key, None)

    if _comparable(merged) == _comparable(doc):
        return doc, False

    merged["generated_at"] = now or entry.get("released_at") or doc.get("generated_at")
    return merged, True


def diff_summary(before: dict[str, Any], after: dict[str, Any], slug: str) -> list[str]:
    """Human-readable list of the fields that actually change."""
    old = find_entry(before, slug) or {}
    new = find_entry(after, slug) or {}
    lines = []
    for key in SYNCED_KEYS:
        if key not in new and key not in old:
            continue
        if old.get(key) != new.get(key):
            lines.append(f"{key}: {old.get(key)!r} -> {new.get(key)!r}")
    return lines


# --- gh plumbing --------------------------------------------------------------


def _gh_bin() -> str:
    binary = shutil.which("gh")
    if not binary:
        raise SyncError("gh CLI not found on PATH")
    return binary


def gh(*args: str, payload: dict[str, Any] | None = None) -> str:
    """Run ``gh api …`` (or any ``gh`` subcommand) and return stdout.

    ``payload`` is passed via ``--input`` so large bodies never hit ARG_MAX and
    nothing is echoed into the log.
    """
    if args and args[0] not in ("api", "pr", "release") and any(
        item.startswith("repos/") for item in args
    ):
        # `gh api` is implicit for REST paths; keep call sites readable.
        args = ("api", *args)
    cmd = [_gh_bin(), *args]
    tmp_name = None
    try:
        if payload is not None:
            fd, tmp_name = tempfile.mkstemp(prefix="gh-payload.", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            cmd += ["--input", tmp_name]
        proc = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        if tmp_name and os.path.exists(tmp_name):
            os.unlink(tmp_name)

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise SyncError(f"gh {' '.join(args[:3])} failed: {detail[0] if detail else 'unknown error'}")
    return proc.stdout


def fetch_catalog(repo: str, branch: str) -> tuple[dict[str, Any], str, str]:
    """Return ``(document, blob_sha, raw_text)`` for catalog.json on ``branch``."""
    raw = gh(f"repos/{repo}/contents/catalog.json?ref={branch}")
    meta = json.loads(raw)
    try:
        text = base64.b64decode(meta["content"]).decode("utf-8")
    except (KeyError, ValueError) as exc:  # pragma: no cover - defensive
        raise SyncError(f"cannot decode catalog.json from {repo}@{branch}") from exc
    return json.loads(text), meta["sha"], text


def publish_via_pull_request(
    repo: str,
    branch: str,
    blob_sha: str,
    document: dict[str, Any],
    *,
    slug: str,
    version: str,
    run_id: str,
) -> str:
    """Commit ``document`` on a short-lived branch and auto-merge the PR.

    Returns the PR URL.  Raises :class:`SyncError` on failure.
    """
    base_sha = json.loads(gh(f"repos/{repo}/git/ref/heads/{branch}"))["object"]["sha"]
    pr_branch = f"catalog/{slug}-{version}-{run_id}".replace(" ", "-")

    gh(
        "-X",
        "POST",
        f"repos/{repo}/git/refs",
        payload={"ref": f"refs/heads/{pr_branch}", "sha": base_sha},
    )

    content = base64.b64encode(
        (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    ).decode("ascii")
    message = f"chore(catalog): {slug} {version}"

    gh(
        "-X",
        "PUT",
        f"repos/{repo}/contents/catalog.json",
        payload={
            "message": message,
            "content": content,
            "sha": blob_sha,
            "branch": pr_branch,
        },
    )

    result = gh(
        "pr", "create",
        "--repo", repo,
        "--base", branch,
        "--head", pr_branch,
        "--title", message,
        "--body",
        "\n".join(
            [
                f"Automated catalog sync for `{slug}` {version}.",
                "",
                f"- source of truth: the published release asset "
                f"(`{slug}-{version}.adz`)",
                "- generated by `.github/workflows/release-adventure.yml`; "
                "the sha256/size_bytes here are the values the release job verified",
                "",
                "Merged automatically by the release workflow.",
            ]
        ),
    )
    pr_url = result.strip().splitlines()[-1].strip() if result.strip() else ""

    merge_error = None
    try:
        gh("pr", "merge", "--repo", repo, pr_branch, "--merge", "--delete-branch")
    except SyncError as exc:  # merge blocked by policy/permissions
        merge_error = str(exc)

    # Read back: the PR body is not evidence that main changed.
    _, new_sha, _ = fetch_catalog(repo, branch)
    landed = new_sha != blob_sha

    if merge_error or not landed:
        raise SyncError(
            f"catalog PR was created ({pr_url}) but did not land on {branch}"
            + (f": {merge_error}" if merge_error else "")
        )
    return pr_url


# --- CLI ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_catalog.py",
        description="Merge a release entry into the distribution repository's catalog.json.",
    )
    parser.add_argument("--entry", type=pathlib.Path, required=True, help="catalog-entry.json from the build")
    parser.add_argument("--slug", help="adventure slug (defaults to the entry's slug)")
    parser.add_argument("--repo", help="owner/repo holding catalog.json")
    parser.add_argument("--branch", default="main", help="target branch (default: main)")
    parser.add_argument("--catalog", type=pathlib.Path, help="offline mode: local catalog.json to merge into")
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", "local"), help="run id for the branch name")
    parser.add_argument("--dry-run", action="store_true", help="merge and print only; never write")
    parser.add_argument("--print", dest="print_document", action="store_true", help="print the merged document")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        entry = json.loads(args.entry.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"error: cannot read entry {args.entry}: {exc}", file=sys.stderr)
        return 1

    slug = args.slug or entry.get("slug")
    if not slug:
        print("error: no slug given (--slug) and none in the entry", file=sys.stderr)
        return 1
    entry.setdefault("slug", slug)
    version = str(entry.get("version", ""))

    try:
        if args.catalog is not None:
            # Offline mode: pure file merge, no network at all.
            try:
                current = json.loads(args.catalog.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                print(f"error: cannot read catalog {args.catalog}: {exc}", file=sys.stderr)
                return 1
            merged, changed = merge_entry(current, entry)
            changes = diff_summary(current, merged, slug) if changed else []
            if changed and not args.dry_run:
                args.catalog.write_text(
                    json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
                )
            _report(slug, version, changed, changes, args.dry_run, target=str(args.catalog))
            if args.print_document:
                print(json.dumps(merged, indent=2, ensure_ascii=False))
            return 0

        if not args.repo:
            print("error: --repo is required unless --catalog is given", file=sys.stderr)
            return 1

        current, blob_sha, _ = fetch_catalog(args.repo, args.branch)
        merged, changed = merge_entry(current, entry)
        changes = diff_summary(current, merged, slug) if changed else []

        if not changed:
            _report(slug, version, False, [], args.dry_run, target=f"{args.repo}@{args.branch}")
            return 0

        if args.dry_run:
            _report(slug, version, True, changes, True, target=f"{args.repo}@{args.branch}")
            if args.print_document:
                print(json.dumps(merged, indent=2, ensure_ascii=False))
            return 0

        pr_url = publish_via_pull_request(
            args.repo,
            args.branch,
            blob_sha,
            merged,
            slug=slug,
            version=version,
            run_id=args.run_id,
        )
        _report(slug, version, True, changes, False, target=f"{args.repo}@{args.branch}")
        print(f"catalog.json updated on {args.branch} via {pr_url}")
        return 0

    except SyncError as exc:
        message = str(exc)
        print(f"error: {message}", file=sys.stderr)
        if "did not land" in message and "PR was created" in message:
            return 3
        return 1


def _report(
    slug: str,
    version: str,
    changed: bool,
    changes: list[str],
    dry_run: bool,
    *,
    target: str,
) -> None:
    if not changed:
        print(f"catalog.json ({target}) already matches {slug} {version} - nothing to do")
        return
    verb = "would update" if dry_run else "updated"
    print(f"{verb} catalog.json ({target}) for {slug} {version}:")
    for line in changes:
        print(f"  {line}")


if __name__ == "__main__":
    sys.exit(main())
