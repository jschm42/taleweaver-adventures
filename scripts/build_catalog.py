#!/usr/bin/env python3
"""Build `catalog.json` from the adventure source tree, or verify it with `--check`.

Division of labour, and the reason this script exists:

* **Curated** copy lives in `<slug>/adventure.meta.json` and is hand-written:
  `id`, `description`, `tags`, `content_warnings`, `min_app_version`,
  `cover_asset`, `status`, `save_compat`. The generator never invents it.
* **Derived** values are computed on every run and must *not* appear in the
  sidecar: `sha256`, `size_bytes`, `cover_sha256`, `cover_url`, `download_url`.
  If they were hand-maintained, every re-encoded cover would force a text
  commit, and `--check` would fail for a change nobody made on purpose.
* **Blueprint** values come from `adventure.adv`: `title`, `teaser`,
  `version`, `format_version`, `author`, `license`, `language`.

`--check` is the PR gate: it fails when the committed `catalog.json` no longer
describes the source tree, so drift becomes a build error *before* a tag is
cut instead of a silent lie after it.

Usage:
    python3 scripts/build_catalog.py            # write catalog.json
    python3 scripts/build_catalog.py --check    # verify, exit 1 on drift
    python3 scripts/build_catalog.py --entry dist/catalog-entry.json
                                                # pre-upload gate: a built release
                                                # entry against the source tree
    python3 scripts/build_catalog.py --json     # machine-readable report
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import os
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO_ROOT / "catalog.json"
PACKER = REPO_ROOT / "scripts" / "pack_adventure.py"

CATALOG_SCHEMA = 2
GENERATOR = "scripts/build_catalog.py"
REPOSITORY = "jschm42/taleweaver-adventures"
RELEASE_BASE = f"https://github.com/{REPOSITORY}/releases/download"

# Keys that are results, never input. Seeing one in a sidecar means someone is
# hand-maintaining a hash, which is exactly the drift this script removes.
DERIVED_KEYS = frozenset(
    {"sha256", "size_bytes", "cover_sha256", "cover_source_sha256", "cover_url",
     "download_url", "released_at"}
)

REQUIRED_META_KEYS = ("id", "description", "tags")
# CONTENT.md: "wo du bist -> was schiefgeht -> was es kostet", 240-700 characters.
# A description shorter than the lower bound is a teaser with a newline.
MIN_DESCRIPTION_CHARS = 240
MAX_DESCRIPTION_CHARS = 700
MIN_TEASER_CHARS = 74

STATUS_VALUES = frozenset({"released", "unreleased-complete", "unreleased-qa", "abandoned"})


class CatalogError(Exception):
    """A source tree that cannot produce a valid catalogue entry."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path, what: str) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CatalogError(f"{path.relative_to(REPO_ROOT)}: {what} is missing")
    except json.JSONDecodeError as exc:
        raise CatalogError(f"{path.relative_to(REPO_ROOT)}: invalid JSON ({exc})") from exc


def dumps(document: dict) -> str:
    """The one serialisation of `catalog.json`, so `--check` can compare bytes.

    Field-level diffs are what a human reads, but a file that differs only in
    formatting or key order is still a file the next generator run will rewrite.
    """
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CatalogError(message)


def _pack(slug: str, into: Path) -> tuple[str, int]:
    """Pack `<slug>/` exactly the way the release workflow does."""
    bundle = into / f"{slug}.adz"
    result = subprocess.run(
        [sys.executable, str(PACKER), "pack", str(REPO_ROOT / slug), "-o", str(bundle)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise CatalogError(f"{slug}: packer failed ({detail[-1] if detail else 'no output'})")
    return _sha256(bundle), bundle.stat().st_size


def _cover_candidates(source_dir: Path) -> list[Path]:
    return sorted(source_dir.glob("assets/*_cover_*"))


def _read_meta(source_dir: Path) -> dict:
    slug = source_dir.name
    path = source_dir / "adventure.meta.json"
    meta = _load_json(path, "metadata sidecar")

    smuggled = sorted(DERIVED_KEYS & set(meta))
    _require(
        not smuggled,
        f"{slug}/adventure.meta.json: derived field(s) {smuggled} do not belong in a sidecar "
        f"- they are computed by {GENERATOR}",
    )
    missing = [key for key in REQUIRED_META_KEYS if key not in meta]
    _require(not missing, f"{slug}/adventure.meta.json: missing required key(s) {missing}")

    _require(
        isinstance(meta["description"], str),
        f"{slug}/adventure.meta.json: description must be a string",
    )
    _require(
        isinstance(meta["tags"], list) and all(isinstance(t, str) for t in meta["tags"]),
        f"{slug}/adventure.meta.json: tags must be a list of strings",
    )
    status = meta.get("status", "released")
    _require(
        status in STATUS_VALUES,
        f"{slug}/adventure.meta.json: status '{status}' is not one of {sorted(STATUS_VALUES)}",
    )
    _require(
        isinstance(meta.get("content_warnings", []), list),
        f"{slug}/adventure.meta.json: content_warnings must be a list",
    )
    return meta


def _copy_problems(slug: str, meta: dict) -> list[str]:
    """Curated-copy gaps. Structural validity is a hard error; missing copy is `--strict-copy`.

    Deliberately not fatal by default: the only way to make this green is for a human to
    write the copy. Letting the generator fill it in would produce a catalogue that is
    consistent and says nothing - the exact failure mode this split is meant to prevent.
    """
    problems = []
    description = (meta.get("description") or "").strip()
    if len(description) < MIN_DESCRIPTION_CHARS:
        problems.append(
            f"{slug}: description has {len(description)} characters, "
            f"at least {MIN_DESCRIPTION_CHARS} expected"
        )
    elif len(description) > MAX_DESCRIPTION_CHARS:
        problems.append(
            f"{slug}: description has {len(description)} characters, "
            f"at most {MAX_DESCRIPTION_CHARS} expected"
        )
    if not meta.get("tags"):
        problems.append(f"{slug}: no tags")
    return problems


def _image_size(path: Path) -> tuple[int, int] | None:
    """Pixel dimensions from the file header, without a decoder.

    Covers are shipped as-is, so the declared aspect ratio is the only thing that
    tells the client how to frame them. A declaration that nobody checks is a
    declaration that drifts: this reads PNG IHDR and the JPEG SOF marker, which is
    enough to catch a wrong number without pulling in Pillow.
    """
    with path.open("rb") as handle:
        head = handle.read(26)
        if head[:8] == b"\x89PNG\r\n\x1a\n":
            return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")
        if head[:2] != b"\xff\xd8":
            return None
        handle.seek(2)
        while True:
            marker = handle.read(2)
            if len(marker) < 2 or marker[0] != 0xFF:
                return None
            while marker[1] == 0xFF:
                following = handle.read(1)
                if not following:
                    return None
                marker = bytes((0xFF, following[0]))
            code = marker[1]
            if code in (0xD8, 0xD9) or 0xD0 <= code <= 0xD7:
                continue
            raw_length = handle.read(2)
            if len(raw_length) < 2:
                return None
            length = int.from_bytes(raw_length, "big")
            if code in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                frame = handle.read(5)
                if len(frame) < 5:
                    return None
                return int.from_bytes(frame[3:5], "big"), int.from_bytes(frame[1:3], "big")
            handle.seek(length - 2, 1)


def _parse_aspect(value: object) -> float | None:
    """`cover_aspect` is curated, so accept the two forms a human writes: "3:2" or 1.5."""
    if isinstance(value, (int, float)) and value > 0:
        return float(value)
    if isinstance(value, str) and ":" in value:
        left, _, right = value.partition(":")
        try:
            numerator, denominator = float(left), float(right)
        except ValueError:
            return None
        if numerator > 0 and denominator > 0:
            return numerator / denominator
    return None


def _check_cover_aspect(slug: str, path: Path, meta: dict) -> str | None:
    """Validate `cover_aspect` against the real pixels. Absent field, no check."""
    if meta.get("cover_aspect") is None:
        return None
    declared = _parse_aspect(meta["cover_aspect"])
    _require(
        declared is not None,
        f"{slug}/adventure.meta.json: cover_aspect must be 'W:H' or a positive number, "
        f"got {meta['cover_aspect']!r}",
    )
    size = _image_size(path)
    _require(
        size is not None,
        f"{slug}: cannot read the pixel size of {path.name} - cover_aspect cannot be verified",
    )
    width, height = size
    actual = width / height
    _require(
        abs(actual - declared) <= 0.01,
        f"{slug}: cover_aspect says {meta['cover_aspect']!r} ({declared:.3f}) but "
        f"{path.name} is {width}x{height} ({actual:.3f}) - the client would frame it wrong",
    )
    return f"{width}x{height}"


def _resolve_cover(source_dir: Path, meta: dict) -> tuple[Path | None, list[str]]:
    """Return the declared cover and any unreferenced cover candidates."""
    slug = source_dir.name
    candidates = _cover_candidates(source_dir)
    declared = meta.get("cover_asset")

    if declared:
        _require(
            bool(declared) and not declared.startswith("/") and ".." not in declared,
            f"{slug}/adventure.meta.json: cover_asset must be a relative path inside {slug}/",
        )
        path = source_dir / declared
        _require(
            path.is_file(),
            f"{slug}/adventure.meta.json: cover_asset '{declared}' does not exist",
        )
    elif candidates:
        _require(
            len(candidates) == 1,
            f"{slug}: {len(candidates)} cover candidates and no 'cover_asset' in "
            f"adventure.meta.json - declare which one is the cover instead of guessing",
        )
        path = candidates[0]
    else:
        path = None

    if path is not None:
        _check_cover_aspect(slug, path, meta)

    unreferenced = []
    if path is not None:
        unreferenced = [
            candidate.relative_to(source_dir).as_posix()
            for candidate in candidates
            if candidate != path
        ]
    return path, unreferenced


def _blueprint_fields(source_dir: Path) -> dict:
    slug = source_dir.name
    blueprint = _load_json(source_dir / "adventure.adv", "blueprint")
    adventure = blueprint.get("adventure") or {}
    _require(isinstance(adventure, dict), f"{slug}/adventure.adv: 'adventure' must be an object")

    title = adventure.get("title")
    version = adventure.get("version")
    teaser = adventure.get("teaser") or ""
    _require(isinstance(title, str) and title.strip(), f"{slug}/adventure.adv: missing title")
    _require(isinstance(version, str) and version.strip(), f"{slug}/adventure.adv: missing version")
    _require(
        len(teaser.strip()) >= MIN_TEASER_CHARS,
        f"{slug}/adventure.adv: teaser must be at least {MIN_TEASER_CHARS} characters",
    )
    return {
        "origin_id": adventure.get("origin_id") or "",
        "title": title,
        "teaser": teaser,
        "version": version,
        "format_version": blueprint.get("version"),
        "author": adventure.get("creator") or adventure.get("author") or "",
        "license": adventure.get("license") or "",
        "language": adventure.get("language") or "",
        "image_url": adventure.get("image_url") or "",
    }


def build_entry(slug: str, staging: Path) -> tuple[dict, list[str], list[str]]:
    """Build one catalogue entry. Returns the entry, non-fatal warnings, copy problems."""
    source_dir = REPO_ROOT / slug
    warnings: list[str] = []

    meta = _read_meta(source_dir)
    copy_problems = _copy_problems(slug, meta)
    blueprint = _blueprint_fields(source_dir)

    # Save-keying integrity. The client keys saves on the UUID, so a differing id
    # between bundle manifest and catalogue entry is a save break that no hash check
    # will ever find. The manifest carries it as `adventure.origin_id`.
    _require(
        bool(blueprint["origin_id"]),
        f"{slug}/adventure.adv: adventure.origin_id is missing - the bundle would ship "
        f"without the save-key UUID that adventure.meta.json declares",
    )
    _require(
        blueprint["origin_id"] == meta["id"],
        f"{slug}: id mismatch between bundle and catalogue: adventure.origin_id="
        f"{blueprint['origin_id']} vs adventure.meta.json:id={meta['id']} "
        f"(saves key on the UUID; this is a save break)",
    )
    cover, unreferenced = _resolve_cover(source_dir, meta)

    if unreferenced:
        raise CatalogError(
            f"{slug}: {len(unreferenced)} unreferenced cover candidate(s) in assets/: "
            f"{unreferenced} - delete them or point cover_asset at them"
        )
    if cover is not None and blueprint["image_url"]:
        declared_in_blueprint = (source_dir / blueprint["image_url"]).resolve()
        if declared_in_blueprint != cover.resolve():
            raise CatalogError(
                f"{slug}: adventure.image_url names '{blueprint['image_url']}' but "
                f"adventure.meta.json:cover_asset names "
                f"'{cover.relative_to(source_dir).as_posix()}' - the bundle and the catalogue "
                f"would ship different covers"
            )

    sha256, size_bytes = _pack(slug, staging)
    version = blueprint["version"]
    tag = f"adv/{slug}/v{version}"

    entry: dict = {
        "id": meta["id"],
        "slug": slug,
        "source_dir": slug,
        "title": blueprint["title"],
        "teaser": blueprint["teaser"],
        "description": meta["description"],
        "version": version,
        "format_version": blueprint["format_version"],
        "size_bytes": size_bytes,
        "sha256": sha256,
        "download_url": f"{RELEASE_BASE}/{tag}/{slug}-{version}.adz",
        "tags": meta["tags"],
        "content_warnings": meta.get("content_warnings", []),
        "status": meta.get("status", "released"),
        "author": blueprint["author"],
        "license": blueprint["license"],
        "language": blueprint["language"],
        "cover_url": "",
        "cover_sha256": "",
    }
    if meta.get("min_app_version"):
        entry["min_app_version"] = meta["min_app_version"]
    if meta.get("save_compat"):
        entry["save_compat"] = meta["save_compat"]
    if cover is not None:
        # Two hashes with two jobs. `cover_source_sha256` hashes the bundled
        # original and proves the delivered artwork was derived from this asset.
        # `cover_sha256` hashes exactly the bytes `cover_url` resolves to, which
        # is what a client verifies after downloading. While the release ships the
        # bundled file unchanged both are equal; the moment a derivative
        # (cover.webp) is published, only `cover_sha256` follows it and the
        # provenance claim stays checkable.
        entry["cover_source_sha256"] = _sha256(cover)
        entry["cover_url"] = f"{RELEASE_BASE}/{tag}/cover{cover.suffix.lower()}"
        entry["cover_sha256"] = _sha256(cover)
    if blueprint["image_url"] and blueprint["image_url"] != (
        cover.relative_to(source_dir).as_posix() if cover else ""
    ):
        warnings.append(
            f"{slug}: adventure.image_url is empty or dangling relative to cover_asset"
        )
    return entry, warnings, copy_problems


def build_catalog() -> tuple[dict, list[str], list[str]]:
    slugs = sorted(
        path.parent.name
        for path in REPO_ROOT.glob("*/adventure.adv")
        if (path.parent / "adventure.meta.json").is_file()
    )
    _require(bool(slugs), "no adventure source directories with a metadata sidecar found")

    entries: list[dict] = []
    warnings: list[str] = []
    copy_problems: list[str] = []
    with tempfile.TemporaryDirectory() as staging_dir:
        staging = Path(staging_dir)
        for slug in slugs:
            entry, entry_warnings, entry_copy_problems = build_entry(slug, staging)
            entries.append(entry)
            warnings.extend(entry_warnings)
            copy_problems.extend(entry_copy_problems)

    released = [entry for entry in entries if entry["status"] == "released"]
    catalog = {
        "schema": CATALOG_SCHEMA,
        "generated_at": _build_timestamp(),
        "generator": GENERATOR,
        "repo_layout": "one source directory per adventure at the repository root",
        "adventures": entries,
    }
    warnings.append(
        f"{len(released)} released, {len(entries) - len(released)} not released "
        f"({', '.join(sorted({e['status'] for e in entries if e['status'] != 'released'})) or 'none'})"
    )
    return catalog, warnings, copy_problems


def _build_timestamp() -> str:
    """Deterministic timestamp. A field that records "when this was generated" but
    lives in the repository only ever records the last human commit, so derive it:
    SOURCE_DATE_EPOCH when the caller pins it, otherwise the commit date."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch and epoch.isdigit():
        moment = datetime.datetime.fromtimestamp(int(epoch), datetime.timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    result = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "log", "-1", "--format=%cI"],
        capture_output=True, text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        moment = datetime.datetime.fromisoformat(result.stdout.strip())
        return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return "1970-01-01T00:00:00Z"


def check_release(slug_entry: dict) -> list[str]:
    """Read back the published release and compare it with what the catalogue claims.

    `--check` only guards the source tree, so it stays green while `cover_url` points
    at a name the release does not carry - a 404 that no local check can see. This is
    the only check that talks to the published artefact, and it is read-only.
    """
    slug = slug_entry["slug"]
    version = slug_entry["version"]
    tag = f"adv/{slug}/v{version}"
    result = subprocess.run(
        ["gh", "api", f"repos/{REPOSITORY}/releases/tags/{tag}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        return [f"{slug}: cannot read release {tag}: {detail[0] if detail else 'gh api failed'}"]

    assets = json.loads(result.stdout).get("assets", [])
    names = [asset["name"] for asset in assets]
    problems = []

    bundles = [asset for asset in assets if asset["name"].endswith(".adz")]
    expected_bundle = f"{slug}-{version}.adz"
    if len(bundles) != 1:
        problems.append(
            f"{slug}: {len(bundles)} .adz assets in {tag} ({', '.join(b['name'] for b in bundles)}) "
            f"- a client cannot tell which one the catalogue means"
        )
    elif bundles[0]["name"] != expected_bundle:
        problems.append(
            f"{slug}: release carries {bundles[0]['name']!r} but the catalogue links "
            f"{expected_bundle!r}"
        )
    else:
        bundle = bundles[0]
        if bundle["size"] != slug_entry["size_bytes"]:
            problems.append(
                f"{slug}: {expected_bundle} is {bundle['size']} B in the release but "
                f"{slug_entry['size_bytes']} B in the catalogue"
            )
        digest = (bundle.get("digest") or "").removeprefix("sha256:")
        if digest and digest != slug_entry["sha256"]:
            problems.append(
                f"{slug}: {expected_bundle} digest {digest[:12]}... is not the catalogue "
                f"sha256 {slug_entry['sha256'][:12]}..."
            )

    cover_url = slug_entry.get("cover_url")
    if cover_url:
        expected_cover = cover_url.rsplit("/", 1)[-1]
        covers = [name for name in names if name.startswith("cover")]
        if expected_cover not in covers:
            problems.append(
                f"{slug}: cover_url points at {expected_cover!r} but {tag} carries "
                f"{covers or 'no cover asset'} - every client following that URL gets a 404"
            )
        elif len(covers) > 1:
            problems.append(f"{slug}: {len(covers)} cover assets in {tag}: {covers}")
        else:
            asset = next(a for a in assets if a["name"] == expected_cover)
            digest = (asset.get("digest") or "").removeprefix("sha256:")
            if digest and digest != slug_entry.get("cover_sha256"):
                problems.append(
                    f"{slug}: {expected_cover} digest {digest[:12]}... is not the catalogue "
                    f"cover_sha256 {(slug_entry.get('cover_sha256') or '')[:12]}..."
                )
    return problems


def diff_catalog(committed: dict, generated: dict) -> list[str]:
    """Field-level diff, narrative fields first so copy loss is impossible to miss."""
    order = ("title", "teaser", "description", "tags", "content_warnings", "status")
    problems: list[str] = []

    if committed.get("schema") != generated.get("schema"):
        problems.append(f"schema: {committed.get('schema')} -> {generated.get('schema')}")

    old = {entry["slug"]: entry for entry in committed.get("adventures", [])}
    new = {entry["slug"]: entry for entry in generated.get("adventures", [])}
    for slug in sorted(set(new) - set(old)):
        problems.append(f"{slug}: missing from catalog.json entirely")
    for slug in sorted(set(old) - set(new)):
        problems.append(f"{slug}: in catalog.json but has no source directory with a sidecar")

    for slug in sorted(set(old) & set(new)):
        before, after = old[slug], new[slug]
        keys = [key for key in order if before.get(key) != after.get(key)]
        keys += [
            key
            for key in sorted(set(before) | set(after))
            if key not in order and before.get(key) != after.get(key)
        ]
        for key in keys:
            problems.append(f"{slug}.{key}: {before.get(key)!r} -> {after.get(key)!r}")
    return problems


# Fields a published release entry must agree with the source tree on. Everything
# else in `catalog-entry.json` is derived from the same sources, so comparing it
# is only meaningful for these: they are what a verifying client keys on.
ENTRY_FIELDS = ("id", "slug", "version", "sha256", "size_bytes", "cover_sha256",
                "cover_source_sha256", "download_url", "cover_url")


def verify_entry(entry_path: Path, generated: dict) -> list[str]:
    """Compare a built `catalog-entry.json` against the source tree.

    Run before anything is uploaded: every mismatch here is a defect that no
    post-upload read-back can repair, because the wrong bytes are already public
    by then. `id` is the save key, `sha256` is the download truth.
    """
    entry = _load_json(entry_path, "release entry")
    slug = entry.get("slug") or ""
    if not slug:
        return [f"{entry_path}: release entry carries no slug, cannot match it to a source directory"]
    generated_entry = next(
        (a for a in generated["adventures"] if a["slug"] == slug), None
    )
    if generated_entry is None:
        return [f"{entry_path}: slug '{slug}' has no source directory in this tree"]

    problems = [
        f"{slug}.{field}: release entry says {entry.get(field)!r} but the source tree "
        f"produces {generated_entry.get(field)!r}"
        for field in ENTRY_FIELDS
        if entry.get(field) != generated_entry.get(field)
    ]

    # The identity chain the client actually relies on: the UUID a save is keyed
    # on must survive bundle -> release entry unchanged.
    origin_id = (REPO_ROOT / slug / "adventure.adv")
    if origin_id.is_file():
        manifest = json.loads(origin_id.read_text(encoding="utf-8")).get("adventure", {})
        if manifest.get("origin_id") != entry.get("id"):
            problems.append(
                f"{slug}: id chain broken: adventure.origin_id="
                f"{manifest.get('origin_id')!r} vs release entry id={entry.get('id')!r} "
                f"(saves key on this UUID)"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="verify catalog.json instead of writing it")
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    parser.add_argument(
        "--strict-copy",
        action="store_true",
        help="also fail when curated copy is missing (description length, tags)",
    )
    parser.add_argument(
        "--release-check",
        action="store_true",
        help="read back every released tag and compare it with the generated catalogue",
    )
    parser.add_argument(
        "--entry",
        metavar="PATH",
        help="verify a built catalog-entry.json against the source tree (pre-upload gate)",
    )
    args = parser.parse_args(argv)

    try:
        generated, warnings, copy_problems = build_catalog()
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

    if args.release_check:
        released = [a for a in generated["adventures"] if a["status"] == "released"]
        release_problems = []
        for adventure in released:
            release_problems.extend(check_release(adventure))
        if release_problems:
            print(
                f"published releases do not match the catalogue ({len(release_problems)} problem(s)):",
                file=sys.stderr,
            )
            for problem in release_problems:
                print(f"  {problem}", file=sys.stderr)
            return 2
        print(f"all {len(released)} published releases match the catalogue")
        return 0

    if args.entry:
        entry_problems = verify_entry(Path(args.entry), generated)
        if entry_problems:
            print(
                f"release entry does not match the source tree ({len(entry_problems)} problem(s)):",
                file=sys.stderr,
            )
            for problem in entry_problems:
                print(f"  {problem}", file=sys.stderr)
            print("refusing to continue: these bytes must not be published", file=sys.stderr)
            return 2
        print(f"{args.entry} matches the source tree ({len(ENTRY_FIELDS)} fields + id chain)")
        return 0

    if not args.check:
        CATALOG_PATH.write_text(dumps(generated), encoding="utf-8")
        print(
            f"wrote {CATALOG_PATH.name}: {len(generated['adventures'])} adventures, "
            f"generated_at={generated['generated_at']}"
        )
        return 0

    committed = _load_json(CATALOG_PATH, "catalogue")
    problems = diff_catalog(committed, generated)
    if not problems and CATALOG_PATH.read_text(encoding="utf-8") != dumps(committed):
        problems.append(
            "catalog.json differs from the canonical serialisation (formatting or key order); "
            "run the generator and commit the result"
        )
    copy_errors = copy_problems if args.strict_copy else []

    if args.json:
        print(json.dumps(
            {"ok": not problems and not copy_errors, "problems": problems,
             "copy_problems": copy_problems, "strict_copy": args.strict_copy},
            indent=2,
        ))
        return 1 if (problems or copy_errors) else 0

    # Copy findings first: a dropped description must be impossible to miss behind
    # a wall of hash diffs. `--strict-copy` promotes them to errors, otherwise they
    # are reported as gaps that only a human can close.
    if copy_problems:
        severity = "error" if args.strict_copy else "gap"
        print(f"curated copy: {len(copy_problems)} {severity}(s)", file=sys.stderr)
        for problem in copy_problems:
            print(f"  {problem}", file=sys.stderr)

    if problems:
        print(
            f"{CATALOG_PATH.name} does not match the source tree ({len(problems)} difference(s)):",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        print("run: python3 scripts/build_catalog.py", file=sys.stderr)
    elif not copy_errors:
        print(f"{CATALOG_PATH.name} is up to date ({len(generated['adventures'])} adventures)")

    return 1 if (problems or copy_errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())
