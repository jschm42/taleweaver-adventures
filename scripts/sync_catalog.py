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
* **The transport, not the producer.** ``scripts/build_catalog.py`` generates
  ``catalog.json`` from the source tree; this script only records the fields the
  release actually proves (version, sha256, size_bytes, download_url,
  released_at, cover_url, cover_sha256).  Editorial fields (id, title, teaser,
  tags, license, ...) come from the sidecars and are copied through untouched.
* **New slugs land.** ``build_catalog.py --emit-entry`` derives a complete entry
  from the tagged tree, so the first release of a slug has no entry to copy and
  is appended in slug order instead of being blocked after the upload.
* **Idempotent.** When the only difference would be the timestamp, nothing is
  written and the script exits 0.  Re-running a release never produces an empty
  ``chore(catalog)`` commit.
* **Two publish methods.**
  ``--method push`` (used by the release workflow) commits ``catalog.json`` and
  pushes it straight to the default branch over SSH, authenticated by a
  write-enabled **deploy key**.  The repository ruleset lists ``DeployKey`` in
  ``bypass_actors`` with ``bypass_mode: always``, so the key is not subject to
  the ``pull_request`` rule, ``require_extra_approval_for_unattributed_changes``
  or the Copilot review rule — and the ruleset itself stays untouched.
  ``--method pr`` keeps the older path (short-lived ``catalog/*`` branch plus an
  auto-merged PR) as a fallback for setups without a deploy key.
* **Auditable.** No secrets are printed.  The API reads go through ``gh`` using
  the ambient ``GH_TOKEN``; the push goes through ``git`` using the ambient
  ``GIT_SSH_COMMAND`` (the workflow sets it up from a secret).

Usage
-----
    # apply by pushing with the deploy key (used by the workflow)
    scripts/sync_catalog.py --repo jschm42/taleweaver-adventures \\
        --slug kitchen-crisis --entry dist/catalog-entry.json --method push

    # apply through an auto-merged PR (fallback, no deploy key needed)
    scripts/sync_catalog.py --repo ... --entry ... --method pr

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
    doc: dict[str, Any], entry: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Merge ``entry`` into ``doc``.

    Returns ``(merged_document, changed)``.  ``changed`` is ``False`` when the
    merge would only rewrite volatile timestamps; in that case ``doc`` itself is
    returned untouched so the caller writes nothing.

    An unknown slug is *appended*, not rejected. Since ``build_catalog.py`` owns
    the document and ``--emit-entry`` derives a complete entry from the tagged
    source tree, "no entry yet" is the normal first-release case and refusing it
    only strands a release whose assets are already public.
    """
    slug = entry.get("slug")
    if not isinstance(slug, str) or not slug:
        raise SyncError("catalog entry has no slug")

    missing = [key for key in REQUIRED_SYNCED_KEYS if key not in entry]
    if missing:
        raise SyncError(f"catalog entry is missing required field(s): {', '.join(missing)}")

    merged = json.loads(json.dumps(doc))
    existing = find_entry(merged, slug)

    if existing is None:
        # First release of a new slug. The entry is complete - it is generated from
        # the tagged source tree by `build_catalog.py --emit-entry`, which only
        # succeeds when `<slug>/adventure.adv` and its sidecar exist - so there is
        # nothing to "add before releasing" and no reason to strand the release
        # after its assets are already public. Insert in slug order: `catalog.json`
        # is sorted by the generator, and appending at the end would make the very
        # next `--check` rewrite the whole file.
        for key in ("id", "title", "teaser"):
            if not entry.get(key):
                raise SyncError(
                    f"catalog entry for new slug {slug!r} has no {key!r}; it must be "
                    "generated from the source tree (build_catalog.py --emit-entry), "
                    "not hand-assembled"
                )
        merged["adventures"] = sorted(
            [*merged.get("adventures", []), json.loads(json.dumps(entry))],
            key=lambda item: item.get("slug", ""),
        )
        if _comparable(merged) == _comparable(doc):
            return doc, False
        merged.pop("generated_at", None)
        return merged, True

    target = existing
    for key in SYNCED_KEYS:
        if key in entry:
            target[key] = entry[key]
        else:
            # cover_* is absent when the adventure has no cover artwork at all.
            target.pop(key, None)

    if _comparable(merged) == _comparable(doc):
        return doc, False

    # `build_catalog.py` stopped writing this field: it only ever recorded the last
    # commit and made the document non-canonical. A transport that re-added it would
    # put the file back in drift on the next `--check`.
    merged.pop("generated_at", None)
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


# --- git plumbing (deploy-key push) -------------------------------------------


def _git(*args: str, cwd: str | None = None) -> str:
    """Run ``git`` and return stdout.

    ``stderr`` is never echoed wholesale: only its last line is used in the
    error message, so a failing SSH handshake cannot spill key material into the
    log.
    """
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise SyncError(f"git {args[0]} failed: {detail[-1] if detail else 'unknown error'}")
    return proc.stdout


def _git_identity() -> list[str]:
    """``-c`` overrides so the commit has a stable, honest author/committer."""
    name = os.environ.get("CATALOG_SYNC_NAME") or "taleweaver-catalog-sync"
    email = os.environ.get("CATALOG_SYNC_EMAIL") or "taleweaver-catalog-sync@users.noreply.github.com"
    return ["-c", f"user.name={name}", "-c", f"user.email={email}"]


def publish_via_git_push(
    repo: str,
    branch: str,
    entry: dict[str, Any],
    *,
    remote_url: str | None = None,
    attempts: int = 3,
) -> tuple[str | None, list[str]]:
    """Commit the merged ``catalog.json`` and push it to ``branch`` directly.

    Authenticated by whatever ``GIT_SSH_COMMAND`` points at — in the release
    workflow that is the repository's write-enabled deploy key, which the
    ruleset lists as a bypass actor, so the push is allowed without a PR.

    Returns ``(commit_message, changed_fields)``; ``(None, [])`` when the
    catalog already matches (another run won the race, nothing to push).

    The merge is recomputed from the freshly cloned branch on every attempt, so
    a concurrently advanced ``main`` is picked up rather than turned into a
    non-fast-forward rejection.  Raises :class:`SyncError` after ``attempts``.
    """
    remote = remote_url or os.environ.get("CATALOG_PUSH_URL") or f"git@github.com:{repo}.git"
    slug = str(entry.get("slug") or "")
    version = str(entry.get("version") or "")
    message = f"chore(catalog): {slug} {version}"
    attempts = max(1, attempts)
    last_error = "unknown error"

    for attempt in range(1, attempts + 1):
        workdir = tempfile.mkdtemp(prefix="catalog-sync.")
        clone_dir = os.path.join(workdir, "repo")
        try:
            _git("clone", "--quiet", "--depth", "1", "--branch", branch, remote, clone_dir)
            catalog_path = pathlib.Path(clone_dir) / "catalog.json"
            try:
                current = json.loads(catalog_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise SyncError(f"cannot read catalog.json from {remote} ({branch}): {exc}") from exc

            merged, changed = merge_entry(current, entry)
            if not changed:
                print(
                    f"catalog.json on {branch} already matches {slug} {version} "
                    f"(attempt {attempt}/{attempts}) - nothing to push"
                )
                return None, []
            changes = diff_summary(current, merged, slug)

            catalog_path.write_text(
                json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            _git("add", "catalog.json", cwd=clone_dir)
            _git(*_git_identity(), "commit", "-m", message, cwd=clone_dir)

            push = subprocess.run(
                ["git", "push", "--quiet", "origin", f"HEAD:{branch}"],
                cwd=clone_dir,
                capture_output=True,
                text=True,
            )
            if push.returncode != 0:
                # Almost always: main moved between clone and push. Retry with a
                # fresh clone instead of failing a release on a lost race.
                detail = (push.stderr or push.stdout).strip().splitlines()
                last_error = detail[-1] if detail else f"exit {push.returncode}"
                print(
                    f"::warning::catalog push rejected (attempt {attempt}/{attempts}): "
                    f"{last_error} - retrying on a fresh clone"
                )
                continue

            # Read back over the network: "push exited 0" is not evidence that
            # the default branch now carries this commit.
            local_sha = _git("rev-parse", "HEAD", cwd=clone_dir).strip()
            remote_sha = _git("ls-remote", remote, f"refs/heads/{branch}").split()[0]
            if remote_sha != local_sha:
                raise SyncError(
                    f"catalog commit {local_sha[:8]} was pushed but {branch} reports "
                    f"{remote_sha[:8]} - the push did not land"
                )
            print(f"pushed {local_sha[:8]} to {branch} via {remote.split('@')[-1]}")
            return message, changes
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    raise SyncError(
        f"git push of catalog.json to {branch} failed after {attempts} attempt(s): {last_error}"
    )


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
    parser.add_argument(
        "--method",
        choices=("pr", "push"),
        default="pr",
        help=(
            "how to publish: 'push' commits catalog.json and pushes it straight to the "
            "target branch over SSH (deploy key, ruleset bypass actor); 'pr' opens and "
            "auto-merges a catalog/* pull request (default, needs pull-requests: write)"
        ),
    )
    parser.add_argument(
        "--remote-url",
        default=os.environ.get("CATALOG_PUSH_URL"),
        help="git remote used by --method push (default: git@github.com:<repo>.git, or CATALOG_PUSH_URL)",
    )
    parser.add_argument(
        "--attempts",
        type=int,
        default=3,
        help="how often --method push retries on a lost race against the target branch (default: 3)",
    )
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

        if args.method == "push" and not args.dry_run:
            # The git path is self-contained: it merges against the branch it
            # just cloned, so the contents API is only needed for reporting.
            message, push_changes = publish_via_git_push(
                args.repo,
                args.branch,
                entry,
                remote_url=args.remote_url,
                attempts=args.attempts,
            )
            if message is None:
                # Another run already wrote the same values; not an error.
                _report(slug, version, False, [], False, target=f"{args.repo}@{args.branch}")
                return 0
            _report(slug, version, True, push_changes, False, target=f"{args.repo}@{args.branch}")
            print(f"catalog.json updated on {args.branch} by direct push ({message})")
            return 0

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
