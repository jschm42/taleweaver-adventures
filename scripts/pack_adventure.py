#!/usr/bin/env python3
"""Deterministic packer/unpacker for TaleWeaver ``.adz`` adventure bundles.

An ``.adz`` file is a plain ZIP archive that contains exactly one
``adventure.adv`` blueprint at the archive root plus an optional ``assets/``
tree with scene images, portraits, etc.

The packer is *byte-reproducible*: packing the same source tree twice (from any
working directory, from a fresh clone, regardless of file mtimes or st_mode)
produces the same archive and therefore the same SHA-256 digest.  That makes
``.adz`` artifacts reviewable in git/CI instead of being opaque binary blobs.

Reproducibility contract implemented here
-----------------------------------------
1.  Entries are emitted in lexicographic order; ``adventure.adv`` always comes
    first (``"adventure.adv" < "assets/"`` already holds for ASCII, but the
    ordering is enforced explicitly rather than relied upon).
2.  Every entry gets the fixed DOS timestamp ``(1980, 1, 1, 0, 0, 0)``.  File
    mtimes and git metadata never reach the archive.
3.  ``external_attr = 0o644 << 16`` and ``create_system = 0`` for every entry,
    so a source tree extracted on a umask/FAT-style filesystem yields the same
    bytes as one extracted on a Unix filesystem.
4.  ``ZIP_DEFLATED`` with an explicit, fixed ``compresslevel``; the compression
    method is set per entry, not only as an archive default.
5.  No directory entries -- files only.  Entry names always use ``/`` and carry
    no leading ``./``.
6.  ``adventure.adv`` is mandatory; its absence aborts with exit code 1.
7.  No comments, no extra fields, no per-entry flag variations.

Known caveat: DEFLATE output depends on the zlib build (zlib vs. zlib-ng vs.
libdeflate) linked into CPython.  Reproducibility is guaranteed for a fixed
interpreter/zlib pair, which is what CI pins down.  Cross-*implementation*
differences are out of scope and should be treated as a version bump, not as
non-determinism in this script.

Usage
-----
    python3 scripts/pack_adventure.py pack   <source_dir> -o <out.adz>
    python3 scripts/pack_adventure.py unpack <file.adz>   -o <target_dir>

Exit codes: 0 success, 1 invalid input / missing blueprint, 2 usage error.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import zipfile
from pathlib import Path

# --- Reproducibility constants ------------------------------------------------

BLUEPRINT_NAME = "adventure.adv"
ASSETS_DIRNAME = "assets"

#: Fixed DOS timestamp for every entry: 1980-01-01 00:00:00 (the ZIP epoch).
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)

#: Fixed permission bits: rw-r--r--, shifted into the high 16 bits of
#: ``external_attr`` (Unix ``st_mode`` half of the field).
FIXED_EXTERNAL_ATTR = 0o644 << 16

#: ``create_system = 0`` -> MS-DOS/FAT.  Choosing a fixed non-Unix value keeps
#: the "version made by" byte identical no matter which OS produced the tree.
FIXED_CREATE_SYSTEM = 0

#: PKZIP spec version 2.0: plain deflate, no ZIP64.
FIXED_VERSION = 20

#: Explicit and fixed: the default of zlib is version dependent.
FIXED_COMPRESS_LEVEL = 9


class PackError(Exception):
    """Recoverable, user-facing error (mapped to exit code 1)."""


# --- helpers -----------------------------------------------------------------


def _iter_source_files(source_dir: Path) -> list[tuple[str, Path]]:
    """Return ``(archive_name, path)`` pairs for every file to pack.

    Files directly below ``source_dir`` come first, then files below
    ``assets/``; both groups sorted lexicographically by archive name.  Paths
    are POSIX-style and stripped of any leading ``./``.
    """
    blueprint = source_dir / BLUEPRINT_NAME
    if not blueprint.is_file():
        raise PackError(
            f"missing {BLUEPRINT_NAME} in {source_dir} -- "
            f"an .adz bundle requires the blueprint at the source root"
        )

    entries: list[tuple[str, Path]] = [(BLUEPRINT_NAME, blueprint)]

    assets_dir = source_dir / ASSETS_DIRNAME
    if assets_dir.is_dir():
        asset_files = [
            (path.relative_to(source_dir).as_posix(), path)
            for path in assets_dir.rglob("*")
            if path.is_file() and not path.is_symlink()
        ]
        entries.extend(sorted(asset_files, key=lambda item: item[0]))

    # Belt and braces: the archive name order is the ZIP order.
    head = [entry for entry in entries if entry[0] == BLUEPRINT_NAME]
    tail = sorted(
        (entry for entry in entries if entry[0] != BLUEPRINT_NAME),
        key=lambda item: item[0],
    )
    return head + tail


def _make_zipinfo(archive_name: str) -> zipfile.ZipInfo:
    """Build a :class:`ZipInfo` whose every variable field is pinned."""
    info = zipfile.ZipInfo(filename=archive_name, date_time=FIXED_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = FIXED_CREATE_SYSTEM
    info.create_version = FIXED_VERSION
    info.extract_version = FIXED_VERSION
    info.external_attr = FIXED_EXTERNAL_ATTR
    info.internal_attr = 0
    info.flag_bits = 0
    info.extra = b""
    info.comment = b""
    info.volume = 0
    info.reserved = 0
    return info


def _safe_target(root: Path, archive_name: str) -> Path:
    """Resolve ``archive_name`` below ``root``, rejecting zip-slip attempts."""
    if archive_name.startswith("/") or archive_name.startswith("\\") or ":" in archive_name[:2]:
        raise PackError(f"refusing absolute path in archive: {archive_name!r}")
    parts = Path(archive_name).parts
    if any(part in ("..", "") for part in parts):
        raise PackError(f"refusing unsafe path in archive: {archive_name!r}")
    target = (root / archive_name).resolve()
    root_resolved = root.resolve()
    if root_resolved != target and root_resolved not in target.parents:
        raise PackError(f"path escapes target directory: {archive_name!r}")
    return target


# --- commands ----------------------------------------------------------------


def pack(source_dir: Path, out_path: Path) -> int:
    """Pack ``source_dir`` into a deterministic ``.adz`` at ``out_path``."""
    if not source_dir.is_dir():
        raise PackError(f"source directory does not exist: {source_dir}")

    entries = _iter_source_files(source_dir)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Write to a sibling temp file and rename, so a failure never leaves a
    # truncated bundle behind.  The temp name itself never enters the archive.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{out_path.name}.", suffix=".tmp", dir=str(out_path.parent)
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with zipfile.ZipFile(
            tmp_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=FIXED_COMPRESS_LEVEL,
            allowZip64=False,
        ) as archive:
            for archive_name, path in entries:
                archive.writestr(
                    _make_zipinfo(archive_name),
                    path.read_bytes(),
                    compress_type=zipfile.ZIP_DEFLATED,
                    compresslevel=FIXED_COMPRESS_LEVEL,
                )
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, out_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    print(
        f"packed {len(entries)} entries -> {out_path}",
        file=sys.stderr,
    )
    return 0


def unpack(archive_path: Path, target_dir: Path) -> int:
    """Extract ``archive_path`` into ``target_dir`` (files only)."""
    if not archive_path.is_file():
        raise PackError(f"archive does not exist: {archive_path}")

    try:
        archive = zipfile.ZipFile(archive_path, mode="r")
    except zipfile.BadZipFile as exc:
        raise PackError(f"not a valid ZIP/.adz archive: {archive_path} ({exc})") from exc

    with archive:
        names = [info.filename for info in archive.infolist() if not info.is_dir()]
        if BLUEPRINT_NAME not in names:
            raise PackError(
                f"{archive_path} does not contain {BLUEPRINT_NAME} at the archive root"
            )

        target_dir.mkdir(parents=True, exist_ok=True)
        for info in archive.infolist():
            if info.is_dir():
                continue
            destination = _safe_target(target_dir, info.filename)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(info))

    print(f"unpacked {len(names)} entries -> {target_dir}", file=sys.stderr)
    return 0


# --- CLI ---------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pack_adventure.py",
        description="Deterministic packer/unpacker for TaleWeaver .adz adventure bundles.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    pack_parser = subparsers.add_parser("pack", help="build a .adz from a source directory")
    pack_parser.add_argument("source_dir", type=Path, help="directory with adventure.adv + assets/")
    pack_parser.add_argument(
        "-o", "--output", type=Path, required=True, help="path of the .adz to write"
    )

    unpack_parser = subparsers.add_parser("unpack", help="extract a .adz into a directory")
    unpack_parser.add_argument("archive", type=Path, help=".adz file to extract")
    unpack_parser.add_argument(
        "-o", "--output", type=Path, required=True, help="target directory"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "pack":
            return pack(args.source_dir, args.output)
        return unpack(args.archive, args.output)
    except PackError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
