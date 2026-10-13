"""
migrate_jsonl_to_db.py — load the JSONL ledgers into Postgres, and prove it.

    python3 scripts/migrate_jsonl_to_db.py --verify-only
    python3 scripts/migrate_jsonl_to_db.py            # migrate, then verify

Loads data/history.jsonl (live) and data/history_backfill.jsonl (reconstructed
from git), preserving `writer` so snipe-test rehearsal rows stay quarantined from
CLV grading exactly as they are today.

VERIFICATION IS ROW-LEVEL, NOT COUNTS
-------------------------------------
Counts would hide a swap: two rows with their prices exchanged pass a count check
perfectly. So this reads every observation back OUT of Postgres through the same
joins a consumer would use, projects both sides onto the identity

    (game_id, book, market, outcome, fetched_at, price, point, last_update, writer)

and asserts the two MULTISETS are equal. Multisets, not sets, so a duplicated row
on either side is caught too. Any difference is printed with examples from both
directions rather than just a count.

`last_update` is in the identity on purpose. It is the field that change-only
storage could never have reproduced — a book restamping without moving its price
emits no row under that design — and it is the strongest single check that the
ingestion path did not quietly normalize anything.
"""

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import db  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
LIVE = REPO / "data" / "history.jsonl"
BACKFILL = REPO / "data" / "history_backfill.jsonl"


def load_jsonl(path):
    rows = []
    try:
        for line in Path(path).read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except FileNotFoundError:
        print(f"[note] {path} absent — skipping", file=sys.stderr)
    return rows


def _iso(v):
    """Normalize a timestamp to a comparable string from either side."""
    if v is None:
        return None
    if hasattr(v, "strftime"):
        from datetime import timezone
        return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(v).strip()
    return s.replace("+00:00", "Z") if s else None


def identity(r):
    """The tuple both sides must agree on, exactly."""
    pt = r.get("point")
    return (
        r.get("game_id"), r.get("book"), r.get("market"), r.get("outcome"),
        _iso(r.get("fetched_at")),
        r.get("price"),
        None if pt is None else round(float(pt), 1),
        _iso(r.get("last_update")),
        db.resolve_writer(r),
    )


def group_into_pulls(rows):
    """{(writer, fetched_at): [rows]} — one transaction per group."""
    out = collections.defaultdict(list)
    for r in rows:
        out[(db.resolve_writer(r), r.get("fetched_at"))].append(r)
    return out


def migrate(conn, rows, label):
    w = db.Writer(conn)
    groups = group_into_pulls(rows)
    pulls = inserted = 0
    for (writer, fetched_at), batch in sorted(groups.items(), key=lambda kv: kv[0][1] or ""):
        # db.pull_key() — the SAME key the live dual-write derives, so a pull
        # already written by a dual-write is skipped here rather than inserted a
        # second time under a different name. An earlier version prefixed this
        # with "migrated:", which made the migration order-dependent: a
        # dual-write landing before it duplicated 960 observations, and only the
        # daily parity check would have noticed.
        _, n = w.write_pull(batch, pull_key=db.pull_key(writer, fetched_at))
        pulls += 1
        inserted += n
    return pulls, inserted


def verify(conn, source_rows):
    src = collections.Counter(identity(r) for r in source_rows)
    out = collections.Counter(identity(r) for r in db.reconstruct_rows(conn))
    missing = src - out          # in the JSONL, absent from Postgres
    extra = out - src            # in Postgres, absent from the JSONL
    return src, out, missing, extra


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--verify-only", action="store_true",
                    help="skip loading; just compare what is already there")
    ap.add_argument("--apply-schema", action="store_true",
                    help="run db/schema.sql first (idempotent)")
    args = ap.parse_args()

    conn = db.connect(args.database_url)
    if conn is None:
        sys.exit("[fatal] no DATABASE_URL — nothing to migrate into")

    live, back = load_jsonl(LIVE), load_jsonl(BACKFILL)
    source = live + back
    print(f"[ok] source: {len(live):,} live + {len(back):,} backfill = "
          f"{len(source):,} rows", file=sys.stderr)
    if not source:
        sys.exit("[fatal] no source rows found")

    if args.apply_schema:
        db.apply_schema(conn)
        print("[ok] schema applied", file=sys.stderr)

    if not args.verify_only:
        for rows, label in ((live, "live"), (back, "backfill")):
            if rows:
                p, n = migrate(conn, rows, label)
                print(f"[ok] {label}: {p} pull(s), {n:,} observations inserted",
                      file=sys.stderr)

    src, out, missing, extra = verify(conn, source)
    print(f"\n[verify] source identities {sum(src.values()):,} "
          f"({len(src):,} distinct)", file=sys.stderr)
    print(f"[verify] postgres identities {sum(out.values()):,} "
          f"({len(out):,} distinct)", file=sys.stderr)

    # The one declared transformation, asserted on its own rather than left to
    # hide inside the normalization both sides share.
    want_unknown = sum(1 for r in source if db.resolve_writer(r) == db.UNKNOWN_WRITER)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM observations ob JOIN pulls p "
                    "ON p.id = ob.pull_id WHERE p.writer = %s",
                    (db.UNKNOWN_WRITER,))
        got_unknown = cur.fetchone()[0]
    ok_unknown = want_unknown == got_unknown
    print(f"[verify] writer='{db.UNKNOWN_WRITER}' rows: source {want_unknown:,} "
          f"vs postgres {got_unknown:,}  {'OK' if ok_unknown else 'MISMATCH'}",
          file=sys.stderr)
    print(f"         (rows predating the `writer` field; backfill rows recover "
          f"theirs from source='git_backfill')", file=sys.stderr)

    if not missing and not extra and ok_unknown:
        print("[ok] EXACT MULTISET MATCH — no row lost, none duplicated, "
              "none altered", file=sys.stderr)
        with conn.cursor() as cur:
            for t in db.TABLES:
                cur.execute(f"SELECT count(*) FROM {t}")
                print(f"     {t:14s} {cur.fetchone()[0]:>8,}", file=sys.stderr)
            cur.execute("SELECT pg_size_pretty(sum(pg_total_relation_size(c.oid))) "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname='public' AND c.relkind='r'")
            print(f"     TOTAL SIZE     {cur.fetchone()[0]:>8}", file=sys.stderr)
        return 0

    print(f"\n[FAIL] {sum(missing.values()):,} row(s) in the JSONL are missing "
          f"from Postgres", file=sys.stderr)
    for ident, n in list(missing.items())[:5]:
        print(f"    missing x{n}: {ident}", file=sys.stderr)
    print(f"[FAIL] {sum(extra.values()):,} row(s) in Postgres are not in the "
          f"JSONL", file=sys.stderr)
    for ident, n in list(extra.items())[:5]:
        print(f"    extra   x{n}: {ident}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
