#!/usr/bin/env python3
"""Generate `catalog.json` from three inputs, or verify it with `--check`.

Where the catalogue's data comes from
-------------------------------------
`catalog.json` is a *generated* file with exactly one producer: this script. It
is a function of three inputs, and every field belongs to exactly one of them.

* **Blueprint** — `<slug>/adventure.adv`: `title`, `teaser`, `version`,
  `format_version`, `author`, `license`, `language`, `origin_id`.
* **Curated copy** — `<slug>/adventure.meta.json`, hand-written: `id`,
  `description`, `tags`, `content_warnings`, `status`, `cover_asset`,
  `min_app_version`, `save_compat`. The generator never invents it.
* **Published bytes** — `catalog.lock.json`: `sha256`, `size_bytes`, published
  cover asset and digest, `released_at`. Written only by the release job after
  an upload, never by hand.

Why a lock file and not "just pack the tree"
--------------------------------------------
`sha256`/`size_bytes` in the catalogue describe the **release asset a client
downloads**, not the current working tree. Those are different bytes for every
adventure released before the deterministic packer existed: `kitchen-crisis`
ships 8_413_245 B (`0386532e…`) while the current packer produces
8_413_062 B (`54ab18fe…`). A generator that packs the tree would therefore
replace true hashes with hashes no client can download — the catalogue would
start lying at the moment it stopped being hand-maintained.

So the published values live in `catalog.lock.json`, are read back from the
release API when they are recorded, and `--check` needs neither a packer nor a
network nor a release token. It is a pure text comparison against the committed
file, which is what makes it usable in an offline PR gate.

Two cover hashes, two jobs
--------------------------
`cover_source_sha256` hashes the cover *as bundled in this repository* and is
derived here. `cover_sha256` hashes exactly the bytes `cover_url` resolves to
and comes from the lock. They differ today for all ten adventures, because the
legacy publisher re-encoded every `.jpg` into a `cover.png`: one field cannot
answer both "what does my client download" and "which source file is that".

Usage
-----
    python3 scripts/build_catalog.py             # write catalog.json
    python3 scripts/build_catalog.py --check     # verify; offline, exit 1 on drift
    python3 scripts/build_catalog.py --entry dist/catalog-entry.json
                                                 # pre-upload gate
    python3 scripts/build_catalog.py --record dist/catalog-entry.json
                                                 # record published bytes into the lock
    python3 scripts/build_catalog.py --check --json

Exit codes
----------
0  catalogue is up to date / written
1  `--check` found drift
2  the inputs cannot produce a valid catalogue (bad sidecar, missing lock entry, ...)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = REPO_ROOT / "catalog.json"
LOCK_PATH = REPO_ROOT / "catalog.lock.json"

CATALOG_SCHEMA = 2
LOCK_SCHEMA = 1
GENERATOR = "scripts/build_catalog.py"
LOCK_GENERATOR = "scripts/sync_catalog.py"
REPOSITORY = "jschm42/taleweaver-adventures"
RELEASE_BASE = f"https://github.com/{REPOSITORY}/releases/download"

#: Values that are results, never input. Seeing one of them in a sidecar means
#: someone is hand-maintaining a hash, which is the drift this split removes.
DERIVED_KEYS = frozenset(
    {
        "sha256",
        "size_bytes",
        "cover_sha256",
        "cover_source_sha256",
        "cover_url",
        "download_url",
        "released_at",
    }
)

REQUIRED_META_KEYS = ("id", "description", "tags")
# CONTENT.md: "wo du bist -> was schiefgeht -> was es kostet", 240-700 Zeichen.
# A description shorter than the lower bound is a teaser with a newline.
MIN_DESCRIPTION_CHARS = 240
MAX_DESCRIPTION_CHARS = 700
MIN_TEASER_CHARS = 74

STATUS_VALUES = frozenset({"released", "unreleased-complete", "unreleased-qa", "abandoned"})

LOCK_KEYS = frozenset(
    {"version", "tag", "asset", "sha256", "size_bytes", "cover_asset", "cover_sha256", "released_at"}
)

SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SEMVER_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
TAG_RE = re.compile(r"^adv/(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)/v(?P<version>[0-9]+\.[0-9]+\.[0-9]+)$")


class CatalogError(Exception):
    """Inputs that cannot produce a valid catalogue entry."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CatalogError(f"{path.name}: {what} is missing") from None
    except json.JSONDecodeError as exc:
        raise CatalogError(f"{path.name}: invalid JSON ({exc})") from exc


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CatalogError(message)


def dumps(document: Any) -> str:
    """The one canonical serialisation. `--check` compares exactly this text.

    No wall clock, no ordering by dictionary insertion at the call site: a
    generated artefact that changes on every run cannot be verified by a diff.
    """
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# --- published-bytes lock -----------------------------------------------------


def load_lock() -> dict[str, Any]:
    """Read and validate `catalog.lock.json`.

    Every entry has to be internally consistent, because a lock that disagrees
    with itself is worse than no lock: the catalogue would inherit the error.
    """
    lock = _load_json(LOCK_PATH, "published-bytes lock")
    _require(isinstance(lock, dict), "catalog.lock.json: top level must be an object")
    _require(
        lock.get("schema") == LOCK_SCHEMA,
        f"catalog.lock.json: schema {lock.get('schema')!r} is not {LOCK_SCHEMA}",
    )
    releases = lock.get("releases")
    _require(isinstance(releases, dict), "catalog.lock.json: 'releases' must be an object")

    for slug, record in releases.items():
        where = f"catalog.lock.json: {slug}"
        _require(SLUG_RE.fullmatch(slug) is not None, f"{where}: key is not a valid slug")
        _require(isinstance(record, dict), f"{where}: entry must be an object")
        unknown = sorted(set(record) - LOCK_KEYS)
        _require(not unknown, f"{where}: unknown key(s) {unknown}")
        missing = sorted({"version", "tag", "asset", "sha256", "size_bytes"} - set(record))
        _require(not missing, f"{where}: missing key(s) {missing}")

        version = record["version"]
        _require(
            isinstance(version, str) and SEMVER_RE.fullmatch(version) is not None,
            f"{where}: version {version!r} is not MAJOR.MINOR.PATCH",
        )
        tag = record["tag"]
        match = TAG_RE.match(tag) if isinstance(tag, str) else None
        _require(match is not None, f"{where}: tag {tag!r} does not match adv/<slug>/v<version>")
        assert match is not None  # narrowing for readers and type checkers
        _require(match.group("slug") == slug, f"{where}: tag {tag!r} names another slug")
        _require(
            match.group("version") == version,
            f"{where}: tag {tag!r} and version {version!r} disagree",
        )
        _require(
            record["asset"] == f"{slug}-{version}.adz",
            f"{where}: asset {record['asset']!r} is not {slug}-{version}.adz",
        )
        _require(
            isinstance(record["sha256"], str) and SHA256_RE.fullmatch(record["sha256"]) is not None,
            f"{where}: sha256 must be 64 lowercase hex characters",
        )
        _require(
            isinstance(record["size_bytes"], int) and record["size_bytes"] > 0,
            f"{where}: size_bytes must be a positive integer",
        )
        if record.get("cover_asset"):
            _require(
                isinstance(record["cover_asset"], str)
                and "/" not in record["cover_asset"]
                and ".." not in record["cover_asset"],
                f"{where}: cover_asset must be a bare release asset name",
            )
            cover_sha = record.get("cover_sha256")
            _require(
                isinstance(cover_sha, str) and SHA256_RE.fullmatch(cover_sha) is not None,
                f"{where}: cover_asset is set but cover_sha256 is missing or malformed",
            )
    return lock


# --- source-tree inputs -------------------------------------------------------


def _cover_candidates(source_dir: Path) -> list[Path]:
    return sorted(source_dir.glob("assets/*_cover_*"))


def _read_meta(source_dir: Path) -> dict[str, Any]:
    slug = source_dir.name
    meta = _load_json(source_dir / "adventure.meta.json", "metadata sidecar")
    _require(isinstance(meta, dict), f"{slug}/adventure.meta.json: top level must be an object")

    smuggled = sorted(DERIVED_KEYS & set(meta))
    _require(
        not smuggled,
        f"{slug}/adventure.meta.json: derived field(s) {smuggled} do not belong in a sidecar "
        f"- they are computed by {GENERATOR} or read from catalog.lock.json",
    )
    missing = [key for key in REQUIRED_META_KEYS if key not in meta]
    _require(not missing, f"{slug}/adventure.meta.json: missing required key(s) {missing}")

    _require(
        isinstance(meta["id"], str) and meta["id"].strip(),
        f"{slug}/adventure.meta.json: id must be a non-empty string",
    )
    _require(
        isinstance(meta["description"], str),
        f"{slug}/adventure.meta.json: description must be a string (empty is allowed, "
        f"missing is not)",
    )
    _require(
        isinstance(meta["tags"], list) and all(isinstance(t, str) for t in meta["tags"]),
        f"{slug}/adventure.meta.json: tags must be a list of strings",
    )
    status = meta.get("status", "released")
    _require(
        status in STATUS_VALUES,
        f"{slug}/adventure.meta.json: status {status!r} is not one of {sorted(STATUS_VALUES)}",
    )
    _require(
        isinstance(meta.get("content_warnings", []), list),
        f"{slug}/adventure.meta.json: content_warnings must be a list",
    )
    return meta


def _copy_problems(slug: str, meta: dict[str, Any]) -> list[str]:
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
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
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


def _check_cover_aspect(slug: str, path: Path, meta: dict[str, Any]) -> None:
    """Validate `cover_aspect` against the real pixels. Absent field, no check."""
    if meta.get("cover_aspect") is None:
        return
    declared = _parse_aspect(meta["cover_aspect"])
    _require(
        declared is not None,
        f"{slug}/adventure.meta.json: cover_aspect must be 'W:H' or a positive number, "
        f"got {meta['cover_aspect']!r}",
    )
    assert declared is not None  # narrowing for readers and type checkers
    size = _image_size(path)
    _require(
        size is not None,
        f"{slug}: cannot read the pixel size of {path.name} - cover_aspect cannot be verified",
    )
    assert size is not None
    width, height = size
    actual = width / height
    _require(
        abs(actual - declared) <= 0.01,
        f"{slug}: cover_aspect says {meta['cover_aspect']!r} ({declared:.3f}) but "
        f"{path.name} is {width}x{height} ({actual:.3f}) - the client would frame it wrong",
    )


def _resolve_cover(source_dir: Path, meta: dict[str, Any]) -> tuple[Path | None, list[str]]:
    """Return the declared cover and any unreferenced cover candidates."""
    slug = source_dir.name
    candidates = _cover_candidates(source_dir)
    declared = meta.get("cover_asset")

    if declared:
        _require(
            isinstance(declared, str) and not declared.startswith("/") and ".." not in declared,
            f"{slug}/adventure.meta.json: cover_asset must be a relative path inside {slug}/",
        )
        path = source_dir / declared
        _require(
            path.is_file(),
            f"{slug}/adventure.meta.json: cover_asset {declared!r} does not exist",
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


def _blueprint_fields(source_dir: Path) -> dict[str, Any]:
    slug = source_dir.name
    blueprint = _load_json(source_dir / "adventure.adv", "blueprint")
    _require(isinstance(blueprint, dict), f"{slug}/adventure.adv: top level must be an object")
    adventure = blueprint.get("adventure") or {}
    _require(isinstance(adventure, dict), f"{slug}/adventure.adv: 'adventure' must be an object")

    title = adventure.get("title")
    version = adventure.get("version")
    teaser = adventure.get("teaser") or ""
    _require(isinstance(title, str) and title.strip(), f"{slug}/adventure.adv: missing title")
    _require(isinstance(version, str) and version.strip(), f"{slug}/adventure.adv: missing version")
    _require(
        SEMVER_RE.fullmatch(version) is not None,
        f"{slug}/adventure.adv: version {version!r} is not MAJOR.MINOR.PATCH",
    )
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


# --- one entry -----------------------------------------------------------------


def build_entry(slug: str, lock: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """Build one catalogue entry. Returns the entry, non-fatal warnings, copy problems."""
    source_dir = REPO_ROOT / slug
    warnings: list[str] = []

    meta = _read_meta(source_dir)
    copy_problems = _copy_problems(slug, meta)
    blueprint = _blueprint_fields(source_dir)
    status = meta.get("status", "released")

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
                f"{slug}: adventure.image_url names {blueprint['image_url']!r} but "
                f"adventure.meta.json:cover_asset names "
                f"{cover.relative_to(source_dir).as_posix()!r} - the bundle and the catalogue "
                f"would ship different covers"
            )

    version = blueprint["version"]
    entry: dict[str, Any] = {
        "id": meta["id"],
        "slug": slug,
        "source_dir": slug,
        "title": blueprint["title"],
        "teaser": blueprint["teaser"],
        "description": meta["description"],
        "version": version,
        "format_version": blueprint["format_version"],
        "status": status,
        "tags": meta["tags"],
        "content_warnings": meta.get("content_warnings", []),
        "author": blueprint["author"],
        "license": blueprint["license"],
        "language": blueprint["language"],
    }
    if meta.get("min_app_version"):
        entry["min_app_version"] = meta["min_app_version"]
    if meta.get("save_compat"):
        entry["save_compat"] = meta["save_compat"]

    # Provenance of the artwork in this tree. Derived, never published: a release
    # may ship a re-encoded derivative whose digest differs (all ten legacy
    # releases do), and this field must not follow it.
    if cover is not None:
        entry["cover_source_sha256"] = _sha256(cover)
    if meta.get("cover_aspect") is not None:
        entry["cover_aspect"] = meta["cover_aspect"]

    if status != "released":
        # Nothing was published, so there is no download truth to state. Inventing
        # one would hand every client a hash that fails verification.
        entry["download_url"] = ""
        return entry, warnings, copy_problems

    record = lock["releases"].get(slug)
    _require(
        record is not None,
        f"{slug}: status is 'released' but catalog.lock.json has no entry - only the "
        f"release job writes published bytes, so this slug has never been published "
        f"from this repository",
    )
    _require(
        record["version"] == version,
        f"{slug}: the tree says v{version} but the published asset is v{record['version']} "
        f"({record['tag']}) - release the new version, or correct the version in adventure.adv",
    )

    entry["size_bytes"] = record["size_bytes"]
    entry["sha256"] = record["sha256"]
    entry["download_url"] = f"{RELEASE_BASE}/{record['tag']}/{record['asset']}"
    if record.get("released_at"):
        entry["released_at"] = record["released_at"]

    if record.get("cover_asset"):
        entry["cover_url"] = f"{RELEASE_BASE}/{record['tag']}/{record['cover_asset']}"
        entry["cover_sha256"] = record["cover_sha256"]
        if cover is None:
            warnings.append(
                f"{slug}: {record['tag']} published {record['cover_asset']} but the source "
                f"tree has no cover - the next release will ship none"
            )
    elif cover is not None:
        warnings.append(
            f"{slug}: the source tree has a cover that {record['tag']} never published - "
            f"the catalogue carries no cover_url until the next release"
        )

    if blueprint["image_url"] and cover is not None:
        if blueprint["image_url"] != cover.relative_to(source_dir).as_posix():
            warnings.append(f"{slug}: adventure.image_url does not name the resolved cover")
    return entry, warnings, copy_problems


def build_catalog(lock: dict[str, Any] | None = None) -> tuple[dict[str, Any], list[str], list[str]]:
    lock = load_lock() if lock is None else lock
    slugs = sorted(
        path.parent.name
        for path in REPO_ROOT.glob("*/adventure.adv")
        if (path.parent / "adventure.meta.json").is_file()
    )
    _require(bool(slugs), "no adventure source directories with a metadata sidecar found")

    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    copy_problems: list[str] = []
    for slug in slugs:
        entry, entry_warnings, entry_copy_problems = build_entry(slug, lock)
        entries.append(entry)
        warnings.extend(entry_warnings)
        copy_problems.extend(entry_copy_problems)

    for slug in sorted(set(lock["releases"]) - set(slugs)):
        warnings.append(
            f"{slug}: catalog.lock.json records a published release but the tree has no "
            f"source directory for it - the catalogue would silently drop a downloadable "
            f"adventure"
        )

    released = [entry for entry in entries if entry["status"] == "released"]
    catalog = {
        "schema": CATALOG_SCHEMA,
        "generator": GENERATOR,
        "repo_layout": "one source directory per adventure at the repository root",
        "adventures": entries,
    }
    warnings.append(
        f"{len(released)} released, {len(entries) - len(released)} not released "
        f"({', '.join(sorted({e['status'] for e in entries if e['status'] != 'released'})) or 'none'})"
    )
    return catalog, warnings, copy_problems


# --- drift report ---------------------------------------------------------------


def diff_catalog(committed: dict[str, Any], generated: dict[str, Any]) -> list[str]:
    """Field-level diff, narrative fields first so copy loss is impossible to miss."""
    order = ("title", "teaser", "description", "tags", "content_warnings", "status", "version")
    problems: list[str] = []

    for key in ("schema", "generator"):
        if committed.get(key) != generated.get(key):
            problems.append(f"{key}: {committed.get(key)!r} -> {generated.get(key)!r}")

    old = {entry["slug"]: entry for entry in committed.get("adventures", [])}
    new = {entry["slug"]: entry for entry in generated.get("adventures", [])}
    for slug in sorted(set(new) - set(old)):
        problems.append(f"{slug}: missing from catalog.json entirely")
    for slug in sorted(set(old) - set(new)):
        problems.append(f"{slug}: in catalog.json but has no source directory with a sidecar")

    for slug in sorted(set(old) & set(new)):
        before, after = old[slug], new[slug]
        keys = [key for key in order if key in before or key in after]
        keys += [key for key in sorted(set(before) | set(after)) if key not in order]
        for key in keys:
            if before.get(key) != after.get(key):
                problems.append(f"{slug}.{key}: {before.get(key)!r} -> {after.get(key)!r}")
    return problems


# --- release entry gate and lock recording ---------------------------------------


def _validated_entry(entry_path: Path) -> tuple[dict[str, Any], str, str]:
    """Load a release entry and check the fields without which it is not a record."""
    entry = _load_json(entry_path, "release entry")
    _require(isinstance(entry, dict), f"{entry_path.name}: release entry must be an object")
    slug = entry.get("slug") or ""
    _require(
        isinstance(slug, str) and SLUG_RE.fullmatch(slug) is not None,
        f"{entry_path.name}: release entry carries no valid slug",
    )
    version = entry.get("version") or ""
    _require(
        isinstance(version, str) and SEMVER_RE.fullmatch(version) is not None,
        f"{slug}: release entry version {version!r} is not MAJOR.MINOR.PATCH",
    )
    sha256 = entry.get("sha256") or ""
    _require(
        isinstance(sha256, str) and SHA256_RE.fullmatch(sha256) is not None,
        f"{slug}: release entry sha256 must be the 64 hex characters of the uploaded asset",
    )
    _require(
        isinstance(entry.get("size_bytes"), int) and entry["size_bytes"] > 0,
        f"{slug}: release entry size_bytes must be a positive integer",
    )
    return entry, slug, version


def _entry_tag(slug: str, version: str, entry: dict[str, Any]) -> str:
    """Resolve the tag a release entry describes, and insist it is self-consistent."""
    expected = f"adv/{slug}/v{version}"
    download_url = entry.get("download_url") or ""
    prefix = f"{RELEASE_BASE}/"
    _require(
        download_url.startswith(prefix),
        f"{slug}: entry download_url {download_url!r} does not start with {prefix}",
    )
    parts = download_url[len(prefix):].split("/")
    _require(
        len(parts) >= 4,
        f"{slug}: entry download_url {download_url!r} is not "
        f"{prefix}adv/<slug>/v<version>/<asset>",
    )
    tag = "/".join(parts[:3])
    _require(
        tag == expected,
        f"{slug}: entry download_url names tag {tag!r} but the tree version is "
        f"{version} ({expected})",
    )
    return tag


def verify_entry(entry_path: Path, generated: dict[str, Any]) -> list[str]:
    """Compare a built `catalog-entry.json` against the source tree, before upload.

    What this gate *can* prove: identity (`id` is the save key), version, cover
    provenance, and that the catalogue knows the slug. What it cannot prove is
    `sha256`/`size_bytes` - those describe the bytes this very build produced,
    and they only become true once they are uploaded. Recording them into the
    lock is the next step, and reading them back from the release API is the
    step after that.
    """
    entry, slug, version = _validated_entry(entry_path)
    generated_entry = next((a for a in generated["adventures"] if a["slug"] == slug), None)
    if generated_entry is None:
        return [f"{entry_path.name}: slug {slug!r} has no source directory in this tree"]

    problems: list[str] = []
    for field in ("id", "slug", "version", "status", "title"):
        if entry.get(field) != generated_entry.get(field):
            problems.append(
                f"{slug}.{field}: release entry says {entry.get(field)!r} but the source tree "
                f"produces {generated_entry.get(field)!r}"
            )

    # The identity chain the client relies on: the UUID a save is keyed on must
    # survive bundle -> release entry unchanged.
    blueprint_path = REPO_ROOT / slug / "adventure.adv"
    if blueprint_path.is_file():
        manifest = json.loads(blueprint_path.read_text(encoding="utf-8")).get("adventure", {})
        if manifest.get("origin_id") != entry.get("id"):
            problems.append(
                f"{slug}: id chain broken: adventure.origin_id="
                f"{manifest.get('origin_id')!r} vs release entry id={entry.get('id')!r} "
                f"(saves key on this UUID)"
            )

    source_sha = generated_entry.get("cover_source_sha256")
    entry_source_sha = entry.get("cover_source_sha256")
    if entry_source_sha is not None and entry_source_sha != source_sha:
        problems.append(
            f"{slug}.cover_source_sha256: the release would ship artwork that is not the "
            f"cover in this tree ({entry_source_sha!r} != {source_sha!r})"
        )
    try:
        _entry_tag(slug, version, entry)
    except CatalogError as exc:
        problems.append(str(exc))
    return problems


def compose_record(entry: dict[str, Any], slug: str, version: str, tag: str) -> dict[str, Any]:
    """Turn a release entry into the lock record it justifies. Pure, no writes.

    Shared by `record_entry` and by the release job's `--dry-run`, so a plan can
    never disagree with the run it plans.
    """
    _require(
        (REPO_ROOT / slug / "adventure.adv").is_file(),
        f"{slug}: no source directory in this tree - the lock may only describe "
        f"adventures this repository ships",
    )

    source_dir = REPO_ROOT / slug
    meta = _read_meta(source_dir)
    blueprint = _blueprint_fields(source_dir)
    _require(
        blueprint["version"] == version,
        f"{slug}: release entry is v{version} but the tree is at v{blueprint['version']} - "
        f"the catalogue would describe an asset built from different sources",
    )
    _require(
        blueprint["origin_id"] == entry.get("id") == meta["id"],
        f"{slug}: id chain broken (adventure.origin_id / entry id / sidecar id must agree)",
    )

    cover, _ = _resolve_cover(source_dir, meta)
    source_sha = _sha256(cover) if cover is not None else None
    if source_sha and entry.get("cover_source_sha256") not in (None, source_sha):
        raise CatalogError(
            f"{slug}: release entry cover_source_sha256 does not match the cover in this tree"
        )

    cover_record: dict[str, str] = {}
    cover_url = entry.get("cover_url") or ""
    cover_prefix = f"{RELEASE_BASE}/{tag}/"
    if cover_url:
        _require(
            cover_url.startswith(cover_prefix) and "/" not in cover_url[len(cover_prefix):],
            f"{slug}: cover_url {cover_url!r} is not a bare asset under {cover_prefix}",
        )
        cover_sha = entry.get("cover_sha256") or ""
        _require(
            isinstance(cover_sha, str) and SHA256_RE.fullmatch(cover_sha) is not None,
            f"{slug}: cover_url is set but cover_sha256 is missing or malformed",
        )
        cover_record = {
            "cover_asset": cover_url[len(cover_prefix):],
            "cover_sha256": cover_sha,
        }
    elif cover is not None:
        print(
            f"warning: {slug}: the release carries no cover though the tree has "
            f"{cover.name} - the catalogue will not advertise a cover",
            file=sys.stderr,
        )

    record: dict[str, Any] = {
        "version": version,
        "tag": tag,
        "asset": f"{slug}-{version}.adz",
        "sha256": entry["sha256"],
        "size_bytes": entry["size_bytes"],
    }
    record.update(cover_record)
    if entry.get("released_at"):
        record["released_at"] = entry["released_at"]
    return record


def record_entry(entry_path: Path) -> dict[str, Any]:
    """Write a published release entry into `catalog.lock.json`.

    The lock is the only place published bytes are stated, and this is its only
    writer. `compose_record` refuses a slug whose tree version disagrees with the
    entry, so the lock can never point at an asset built from other sources than
    the ones the catalogue describes.
    """
    entry, slug, version = _validated_entry(entry_path)
    tag = _entry_tag(slug, version, entry)
    record = compose_record(entry, slug, version, tag)

    lock = load_lock()
    previous = lock["releases"].get(slug)
    lock["releases"][slug] = record
    lock["releases"] = dict(sorted(lock["releases"].items()))
    lock["generator"] = LOCK_GENERATOR
    LOCK_PATH.write_text(dumps(lock), encoding="utf-8")

    print(
        f"recorded {slug} v{version} in {LOCK_PATH.name}: "
        f"sha256={record['sha256'][:12]}… size={record['size_bytes']}"
        + (f" (was v{previous['version']} {previous['sha256'][:12]}…)" if previous else " (new)")
    )
    return lock


# --- CLI ------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="verify catalog.json instead of writing it")
    parser.add_argument("--json", action="store_true", help="print a machine-readable report")
    parser.add_argument(
        "--strict-copy",
        action="store_true",
        help="also fail when curated copy is missing (description length, tags)",
    )
    parser.add_argument(
        "--entry",
        metavar="PATH",
        help="verify a built catalog-entry.json against the source tree (pre-upload gate)",
    )
    parser.add_argument(
        "--record",
        metavar="PATH",
        help="record a published catalog-entry.json into catalog.lock.json (release job only)",
    )
    args = parser.parse_args(argv)

    try:
        if args.record:
            lock = record_entry(Path(args.record))
            generated, warnings, _ = build_catalog(lock)
            CATALOG_PATH.write_text(dumps(generated), encoding="utf-8")
            print(
                f"wrote {CATALOG_PATH.name}: {len(generated['adventures'])} adventures "
                f"(regenerated from {LOCK_PATH.name})"
            )
            for warning in warnings:
                print(f"warning: {warning}", file=sys.stderr)
            return 0

        generated, warnings, copy_problems = build_catalog()
    except CatalogError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    for warning in warnings:
        print(f"warning: {warning}", file=sys.stderr)

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
        print(f"{args.entry} matches the source tree (identity, version, cover provenance)")
        return 0

    text = dumps(generated)

    if not args.check:
        CATALOG_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {CATALOG_PATH.name}: {len(generated['adventures'])} adventures")
        return 0

    committed_text = CATALOG_PATH.read_text(encoding="utf-8") if CATALOG_PATH.is_file() else ""
    committed = json.loads(committed_text) if committed_text.strip() else {}
    problems = [] if committed_text == text else diff_catalog(committed, generated)
    copy_errors = copy_problems if args.strict_copy else []

    if args.json:
        print(
            json.dumps(
                {"ok": not problems and not copy_errors, "problems": problems,
                 "copy_problems": copy_problems, "strict_copy": args.strict_copy},
                indent=2,
            )
        )
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
            f"{CATALOG_PATH.name} does not match its inputs ({len(problems)} difference(s)):",
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
