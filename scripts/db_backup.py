"""
db_backup.py — weekly pg_dump, with a guard against publishing the wrong thing.

    python3 scripts/db_backup.py --out backups/

Neon Free gives a 6-hour history window and no instant restore, so Neon is not a
backup. The dump is published as a GitHub Release asset: off-Neon, versioned,
free, and not counted against the repository size the way a committed file would
be.

THE REPO IS PUBLIC, SO THESE DUMPS ARE PUBLIC.
That is acceptable for odds data and unacceptable for anything else, so this
refuses to produce a dump containing any table outside db.TABLES. The allowlist
is mechanical on purpose: a future `bets` or `bankroll` table would otherwise be
swept into a world-readable release by a workflow nobody re-read.

Uses the DIRECT connection: pg_dump over a pooled endpoint is explicitly
unsupported.
"""

import argparse
import gzip
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import db  # noqa: E402


def public_tables(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_type='BASE TABLE' "
                    "ORDER BY table_name")
        return [r[0] for r in cur.fetchall()]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="backups")
    ap.add_argument("--tag", default=None, help="release tag (default: db-YYYY-Www)")
    args = ap.parse_args()

    url = db.database_url()
    if not url:
        sys.exit("[fatal] no DATABASE_URL")

    conn = db.connect()
    found = public_tables(conn)
    allowed = set(db.TABLES)
    unexpected = [t for t in found if t not in allowed]
    print(f"[ok] tables present: {', '.join(found)}", file=sys.stderr)
    if unexpected:
        print(f"::error::REFUSING TO DUMP. Unexpected table(s): "
              f"{', '.join(unexpected)}. These dumps are published to a PUBLIC "
              f"repository. If the table is genuinely odds data, add it to "
              f"db.TABLES deliberately; if it holds bets, bankroll or anything "
              f"personal, it must not live in this database at all.")
        return 1
    missing = allowed - set(found)
    if missing:
        print(f"::warning::allowlisted table(s) absent from the database: "
              f"{', '.join(sorted(missing))}", file=sys.stderr)
    conn.close()

    if not shutil.which("pg_dump"):
        sys.exit("[fatal] pg_dump not on PATH")

    now = datetime.now(timezone.utc)
    tag = args.tag or f"db-{now:%G-W%V}"
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    raw = outdir / f"{tag}.dump"
    gz = outdir / f"{tag}.dump.gz"

    # --format=custom so pg_restore can be selective; -Z0 because we gzip after
    cmd = ["pg_dump", "--format=custom", "-Z0", "--no-owner", "--no-privileges",
           "--file", str(raw)]
    for t in sorted(allowed & set(found)):
        cmd += ["--table", f"public.{t}"]
    cmd.append(url)
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"::error::pg_dump failed: {r.stderr.strip()[:400]}")
        return 1

    # Second, independent check: read the object list back out of the dump
    # itself, in case --table ever silently failed to restrict anything. Done on
    # the FILE, before gzipping — pg_restore cannot read a custom-format dump
    # from stdin because it needs seekable input, and piping it made this check
    # find nothing and pass vacuously.
    listing = subprocess.run(["pg_restore", "-l", str(raw)],
                             capture_output=True, text=True)
    if listing.returncode != 0:
        print(f"::error::could not list the dump's contents: "
              f"{listing.stderr.strip()[:300]}")
        return 1
    names = {line.split()[-2] for line in listing.stdout.splitlines()
             if "TABLE DATA" in line}
    if not names:
        # A verification that can pass while finding nothing is worse than none.
        print("::error::the dump contains NO table data at all — refusing to "
              "publish an empty backup")
        return 1
    stray = sorted(n for n in names if n not in allowed)
    if stray:
        print(f"::error::the produced dump contains unexpected table data: "
              f"{', '.join(stray)}")
        return 1

    with open(raw, "rb") as fi, gzip.open(gz, "wb", compresslevel=9) as fo:
        shutil.copyfileobj(fi, fo)
    raw_size, gz_size = raw.stat().st_size, gz.stat().st_size
    raw.unlink()

    print(f"[ok] {gz.name}: {gz_size:,} bytes gzipped (from {raw_size:,})",
          file=sys.stderr)
    print(f"[ok] verified the dump contains only: {', '.join(sorted(names))}",
          file=sys.stderr)
    gh_out = os.getenv("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            f.write(f"tag={tag}\nfile={gz}\nbytes={gz_size}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
