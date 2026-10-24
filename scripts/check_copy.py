#!/usr/bin/env python3
"""Check the copy that ships: the ten adventure.meta.json sidecars in this repository.

Read-only, no network, no secret. Two modes:

  (default)      whatever copy is present must be valid. Empty copy passes, because
                 today it is empty and a gate that is red by design is a gate nobody
                 reads before the release that matters.
  --require-copy empty copy is an error. This is the mode for the re-cut: it turns
                 "the descriptions are in" from a memory into an exit code.

What it measures: length 240-700 characters, every tag from the vocabulary, no axis
with more than two values, every content warning from the vocabulary, and that the
sidecar's id matches the adventure manifest - so a description cannot be attached to
the wrong adventure.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
VOCAB_PATH = REPO_ROOT / "scripts" / "copy-vocabulary.json"

MIN_LEN = 240
MAX_LEN = 700
MAX_PER_AXIS = 2


def load_vocabulary() -> tuple[dict[str, list[str]], set[str], dict[str, str]]:
    document = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
    tags: dict[str, list[str]] = document["tags"]
    axis_of = {value: axis for axis, values in tags.items() for value in values}
    return tags, set(axis_of), axis_of


def check_one(slug: str, meta: dict, axis_of: dict[str, str], warnings: set[str],
              require_copy: bool) -> list[str]:
    problems: list[str] = []
    description = meta.get("description") or ""

    if not description.strip():
        if require_copy:
            problems.append(f"{slug}: description is empty")
    else:
        length = len(description.strip())
        if length < MIN_LEN:
            problems.append(f"{slug}: description is {length} characters, minimum {MIN_LEN}")
        elif length > MAX_LEN:
            problems.append(f"{slug}: description is {length} characters, maximum {MAX_LEN}")
        if description != description.strip():
            problems.append(f"{slug}: description has leading or trailing whitespace")

    tags = meta.get("tags") or []
    unknown = [tag for tag in tags if tag not in axis_of]
    if unknown:
        problems.append(f"{slug}: tags outside the vocabulary: {', '.join(unknown)}")
    per_axis: dict[str, int] = {}
    for tag in tags:
        axis = axis_of.get(tag)
        if axis:
            per_axis[axis] = per_axis.get(axis, 0) + 1
    over = {axis: count for axis, count in per_axis.items() if count > MAX_PER_AXIS}
    for axis, count in sorted(over.items()):
        problems.append(f"{slug}: {count} tags on axis '{axis}', at most {MAX_PER_AXIS}")

    unknown_warnings = [w for w in (meta.get("content_warnings") or []) if w not in warnings]
    if unknown_warnings:
        problems.append(
            f"{slug}: content warnings outside the vocabulary: {', '.join(unknown_warnings)}"
        )

    manifest = REPO_ROOT / slug / "adventure.adv"
    if manifest.exists():
        declared = json.loads(manifest.read_text(encoding="utf-8"))["adventure"].get("origin_id")
        if declared and meta.get("id") and declared != meta["id"]:
            problems.append(
                f"{slug}: sidecar id {meta['id']} does not match adventure.origin_id {declared}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-copy", action="store_true",
                        help="treat an empty description as an error (re-cut mode)")
    args = parser.parse_args(argv)

    _, _, axis_of = load_vocabulary()
    vocabulary = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))

    sidecars = sorted(REPO_ROOT.glob("*/adventure.meta.json"))
    if not sidecars:
        print("no adventure.meta.json sidecars found - wrong working directory?",
              file=sys.stderr)
        return 2

    problems: list[str] = []
    filled = 0
    for path in sidecars:
        slug = path.parent.name
        meta = json.loads(path.read_text(encoding="utf-8"))
        if (meta.get("description") or "").strip():
            filled += 1
        problems.extend(check_one(slug, meta, axis_of, set(vocabulary["content_warnings"]),
                                 args.require_copy))

    if problems:
        print(f"copy check failed ({len(problems)} problem(s) across {len(sidecars)} "
              f"sidecars):", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    mode = "require-copy" if args.require_copy else "present-only"
    print(f"copy ok ({mode}): {filled}/{len(sidecars)} descriptions present, "
          f"{MIN_LEN}-{MAX_LEN} characters, vocabulary and axes clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
