#!/usr/bin/env python3
"""Unpack a TaleWeaver ``.adz`` into this repository and rebuild ``library.json``.

    python scripts/add_adventure.py path/to/adventure.adz [--slug NAME]
    python scripts/add_adventure.py --rebuild

The adventure lands in ``<slug>/`` (``adventure.adv`` + ``assets/``). A directory that
already holds the same ``origin_id`` is replaced, so a retitled adventure keeps its slug.
``library.json`` is then regenerated from every ``<slug>/adventure.adv``; the TaleWeaver
app reads it and fetches the listed files straight from this repository.

Nothing is committed or pushed - review the diff, then commit and push by hand.
Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX_PATH = REPO_ROOT / "library.json"
BLUEPRINT = "adventure.adv"
ASSETS = "assets"
INDEX_SCHEMA = 1
# Generous sanity cap; the app enforces its own download limit.
MAX_UNPACKED_BYTES = 500 * 1024 * 1024
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class AdventureError(Exception):
    """Invalid input; reported without a traceback (exit code 1)."""


def _fs(path: Path) -> str:
    """Path string that survives Windows' 260-character limit (some asset names are long)."""
    resolved = str(path.resolve())
    if os.name != "nt" or resolved.startswith("\\\\?\\"):
        return resolved
    return "\\\\?\\" + resolved


def slugify(title: str) -> str:
    """``"Professor Quark's Quantum Quest"`` -> ``"professor-quark-s-quantum-quest"``."""
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def _check_member(name: str) -> None:
    path = PurePosixPath(name)
    if "\\" in name or path.is_absolute() or ".." in path.parts or ":" in name:
        raise AdventureError(f"unsafe path in bundle: {name!r}")
    if name != BLUEPRINT and path.parts[0] != ASSETS:
        raise AdventureError(f"unexpected file in bundle: {name!r} (only {BLUEPRINT} and {ASSETS}/)")


def read_bundle(bundle: Path) -> tuple[dict, dict[str, bytes]]:
    """Return ``(adventure section of the manifest, {archive name: bytes})``."""
    try:
        archive = zipfile.ZipFile(bundle)
    except (OSError, zipfile.BadZipFile) as exc:
        raise AdventureError(f"cannot open {bundle}: {exc}") from exc
    with archive:
        members = [info for info in archive.infolist() if not info.is_dir()]
        for info in members:
            _check_member(info.filename)
        if sum(info.file_size for info in members) > MAX_UNPACKED_BYTES:
            raise AdventureError(f"{bundle} unpacks to more than {MAX_UNPACKED_BYTES} bytes")
        files = {info.filename: archive.read(info) for info in members}
    if BLUEPRINT not in files:
        raise AdventureError(f"{bundle} has no {BLUEPRINT} at the archive root")
    try:
        adventure = json.loads(files[BLUEPRINT].decode("utf-8")).get("adventure") or {}
    except (UnicodeDecodeError, ValueError, AttributeError) as exc:
        raise AdventureError(f"{BLUEPRINT} in {bundle} is not valid JSON: {exc}") from exc
    for key in ("origin_id", "title", "version"):
        if not adventure.get(key):
            raise AdventureError(f"{BLUEPRINT} in {bundle} has no adventure.{key}")
    return adventure, files


def _read_adventure(source_dir: Path) -> dict:
    blueprint = json.loads((source_dir / BLUEPRINT).read_text(encoding="utf-8"))
    return blueprint.get("adventure") or {}


def _source_dirs() -> list[Path]:
    return sorted(path.parent for path in REPO_ROOT.glob(f"*/{BLUEPRINT}"))


def _existing_dir(origin_id: str) -> Path | None:
    return next((d for d in _source_dirs() if _read_adventure(d).get("origin_id") == origin_id), None)


def _readme(adventure: dict) -> str:
    return f"# {adventure['title']}\n\n{adventure.get('teaser') or ''}\n"


def install(adventure: dict, files: dict[str, bytes], slug: str | None) -> Path:
    """Replace the adventure's source directory with the bundle contents."""
    existing = _existing_dir(adventure["origin_id"])
    slug = slug or (existing.name if existing else slugify(adventure["title"]))
    if not SLUG_RE.fullmatch(slug):
        raise AdventureError(f"invalid slug {slug!r}; pass --slug with lowercase-kebab-case")
    target = REPO_ROOT / slug
    if existing and existing != target:
        raise AdventureError(
            f"{existing.name}/ already holds origin_id {adventure['origin_id']}; "
            f"drop --slug or remove that directory first"
        )
    if not existing and target.exists():
        raise AdventureError(f"{slug}/ exists but holds a different adventure; pass --slug")

    staging = REPO_ROOT / f".{slug}.staging"
    shutil.rmtree(_fs(staging), ignore_errors=True)
    for name, data in files.items():
        destination = staging / name
        os.makedirs(_fs(destination.parent), exist_ok=True)
        with open(_fs(destination), "wb") as handle:
            handle.write(data)
    readme = target / "README.md"
    if readme.is_file():
        shutil.copyfile(_fs(readme), _fs(staging / "README.md"))
    else:
        (staging / "README.md").write_text(_readme(adventure), encoding="utf-8", newline="\n")

    shutil.rmtree(_fs(target), ignore_errors=True)
    os.replace(_fs(staging), _fs(target))
    return target


def _asset_files(source_dir: Path) -> list[str]:
    """Relative POSIX names below ``assets/``; walks the long-path form, since
    ``Path.rglob`` silently skips names beyond 260 characters on Windows."""
    root = _fs(source_dir)
    names = []
    for folder, _, filenames in os.walk(_fs(source_dir / ASSETS)):
        relative = Path(os.path.relpath(folder, root)).as_posix()
        names.extend(f"{relative}/{filename}" for filename in filenames)
    return sorted(names)


def _index_entry(source_dir: Path) -> dict:
    adventure = _read_adventure(source_dir)
    files = [BLUEPRINT, *_asset_files(source_dir)]
    paths = [source_dir / name for name in files]
    cover = adventure.get("image_url") or ""
    return {
        "id": adventure.get("origin_id") or "",
        "slug": source_dir.name,
        "title": adventure.get("title") or source_dir.name,
        "teaser": adventure.get("teaser") or "",
        "version": adventure.get("version") or "",
        "author": adventure.get("creator") or adventure.get("author") or "",
        "language": adventure.get("language") or "",
        "license": adventure.get("license") or "",
        "cover": cover if cover in files else None,
        "size_bytes": sum(os.path.getsize(_fs(path)) for path in paths),
        "files": files,
    }


def build_index() -> dict:
    """The whole index is a function of the source tree: no timestamps, sorted by slug."""
    return {
        "schema": INDEX_SCHEMA,
        "adventures": [_index_entry(source_dir) for source_dir in _source_dirs()],
    }


def write_index() -> int:
    index = build_index()
    text = json.dumps(index, indent=2, ensure_ascii=False) + "\n"
    INDEX_PATH.write_text(text, encoding="utf-8", newline="\n")
    return len(index["adventures"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("bundle", nargs="?", type=Path, help=".adz file to add or update")
    parser.add_argument("--slug", help="directory name for a new adventure (default: from the title)")
    parser.add_argument("--rebuild", action="store_true", help="only regenerate library.json")
    args = parser.parse_args(argv)
    if not args.bundle and not args.rebuild:
        parser.error("give a .adz file or --rebuild")

    try:
        if args.bundle:
            adventure, files = read_bundle(args.bundle)
            target = install(adventure, files, args.slug)
            print(f"unpacked {len(files)} files -> {target.name}/ ({adventure['title']} v{adventure['version']})")
        count = write_index()
    except AdventureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {INDEX_PATH.name}: {count} adventures - review, then commit and push")
    return 0


if __name__ == "__main__":
    sys.exit(main())
