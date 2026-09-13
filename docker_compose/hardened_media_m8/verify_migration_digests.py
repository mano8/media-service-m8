#!/usr/bin/env python3
"""Digest parity for the object-storage data migration (T25-data-migration-runbook).

Joins the ``sha256`` column ``media_service`` stores on every media object
(``<TABLES_PREFIX>_media_object``, when the client declared one at
``complete``) against what ``rclone hashsum sha256 --download`` computed from
the bytes actually stored on the *destination* backend, and reports:

    matched     row has a sha256 and the destination bytes hash to it
    mismatch    row has a sha256 and the destination bytes hash to something else
    missing     row names a key the destination listing does not contain
    uncovered   row is present but not digest-checked: it has no sha256
                (the client never declared one) or the bucket was given as
                a bare listing — parity for those rests on ``rclone check
                --download`` (byte-for-byte against the source)

Exit status is 1 on any ``mismatch`` or ``missing`` — the runbook's rollback
trigger R2 — and 0 otherwise. Standard library only, so it runs wherever
``psql`` output can be copied to; nothing here talks to the network.

Inputs::

    --digests      CSV with a header row and exactly the columns
                   storage_bucket,object_key,sha256 — the runbook's ``COPY``
                   export (§3.3 / §5.3). ``sha256`` may be empty.
    --hashsum      ``<bucket>=<file>`` pairs, each file the output of
                   ``rclone hashsum sha256 --download dst:<bucket>``
                   (``<hex>  <key>`` per line, two spaces). Repeatable.
    --listing      ``<bucket>=<file>`` pairs, each file the output of
                   ``rclone lsf -R --files-only src:<bucket>`` (one key per
                   line). Presence only — every row found is ``uncovered``.
                   This is how §3.3 establishes the *baseline* ``missing`` set
                   on the source without downloading it. Repeatable.
    --missing-out  write the ``missing`` rows, sorted, one ``bucket/key`` per
                   line, so two runs (source baseline, destination) can be
                   ``diff``ed: an empty diff means the migration lost nothing.

Examples::

    # §3.3 — baseline on the source, from listings
    python verify_migration_digests.py --digests migration-reports/digests.csv \\
        --listing private-media=migration-reports/lsf-src-private-media.txt \\
        --missing-out migration-reports/missing-src.txt

    # §5.3 — destination, from downloaded hashes
    python verify_migration_digests.py --digests migration-reports/digests.csv \\
        --hashsum private-media=migration-reports/sha256-private-media.txt \\
        --missing-out migration-reports/missing-dst.txt
    diff migration-reports/missing-src.txt migration-reports/missing-dst.txt
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

_EXPECTED_COLUMNS = ("storage_bucket", "object_key", "sha256")


def load_hashsums(path: Path) -> dict[str, str]:
    """Parse one ``rclone hashsum`` output file into ``{key: hex_digest}``."""
    digests: dict[str, str] = {}
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, start=1):
            line = raw.rstrip("\r\n")
            if not line:
                continue
            digest, sep, key = line.partition("  ")
            if not sep or len(digest) != 64:
                raise SystemExit(
                    f"{path}:{line_number}: not an rclone hashsum line: {line!r}"
                )
            digests[key] = digest.lower()
    return digests


def load_listing(path: Path) -> dict[str, str]:
    """Parse one ``rclone lsf -R --files-only`` output into ``{key: ""}``."""
    with path.open(encoding="utf-8") as handle:
        return {line.rstrip("\r\n"): "" for line in handle if line.strip()}


def load_digest_rows(path: Path) -> list[tuple[str, str, str]]:
    """Parse the DB export into ``(bucket, key, sha256_or_empty)`` rows."""
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != _EXPECTED_COLUMNS:
            raise SystemExit(
                f"{path}: expected header {','.join(_EXPECTED_COLUMNS)}, "
                f"got {reader.fieldnames}"
            )
        return [
            (
                row["storage_bucket"],
                row["object_key"],
                (row["sha256"] or "").strip().lower(),
            )
            for row in reader
        ]


def verify(
    rows: list[tuple[str, str, str]], hashsums: dict[str, dict[str, str]]
) -> tuple[Counter, list[str]]:
    """Classify every row; return the tally and the human-readable problem lines."""
    tally: Counter = Counter()
    problems: list[str] = []
    for bucket, key, expected in rows:
        listing = hashsums.get(bucket)
        if listing is None:
            tally["missing"] += 1
            problems.append(f"missing   {bucket}/{key}  (no hashsum file for bucket)")
            continue
        actual = listing.get(key)
        if actual is None:
            tally["missing"] += 1
            problems.append(f"missing   {bucket}/{key}")
            continue
        if not expected or not actual:
            tally["uncovered"] += 1
            continue
        if actual == expected:
            tally["matched"] += 1
        else:
            tally["mismatch"] += 1
            problems.append(f"mismatch  {bucket}/{key}  db={expected}  stored={actual}")
    return tally, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--digests", required=True, type=Path)
    parser.add_argument(
        "--hashsum",
        action="append",
        default=[],
        metavar="BUCKET=FILE",
        help="rclone hashsum sha256 --download output for one bucket (repeatable)",
    )
    parser.add_argument(
        "--listing",
        action="append",
        default=[],
        metavar="BUCKET=FILE",
        help="rclone lsf -R --files-only output for one bucket, presence only "
        "(repeatable)",
    )
    parser.add_argument(
        "--missing-out",
        type=Path,
        default=None,
        help="write the sorted missing bucket/key lines here for a later diff",
    )
    args = parser.parse_args(argv)

    hashsums: dict[str, dict[str, str]] = {}
    for option, specs, loader in (
        ("--hashsum", args.hashsum, load_hashsums),
        ("--listing", args.listing, load_listing),
    ):
        for spec in specs:
            bucket, sep, file_name = spec.partition("=")
            if not sep or not bucket or not file_name:
                parser.error(f"{option} expects BUCKET=FILE, got {spec!r}")
            if bucket in hashsums:
                parser.error(f"bucket {bucket!r} given twice")
            hashsums[bucket] = loader(Path(file_name))

    rows = load_digest_rows(args.digests)
    tally, problems = verify(rows, hashsums)

    for line in problems:
        print(line)
    if args.missing_out is not None:
        missing = sorted(
            f"{bucket}/{key}"
            for bucket, key, _expected in rows
            if key not in hashsums.get(bucket, {})
        )
        args.missing_out.write_text(
            "".join(f"{line}\n" for line in missing), encoding="utf-8"
        )
    print(
        f"rows={len(rows)} matched={tally['matched']} "
        f"mismatch={tally['mismatch']} missing={tally['missing']} "
        f"uncovered={tally['uncovered']}"
    )
    if tally["uncovered"]:
        print(
            f"note: {tally['uncovered']} row(s) present but not digest-checked "
            "(no stored sha256, or a bare listing) — their parity rests on "
            "`rclone check --download` (runbook §5.2)"
        )
    return 1 if (tally["mismatch"] or tally["missing"]) else 0


if __name__ == "__main__":
    sys.exit(main())
