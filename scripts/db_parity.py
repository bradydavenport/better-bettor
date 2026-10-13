"""
db_parity.py — does Postgres agree with the JSONL ledger, exactly?

    python3 scripts/db_parity.py --pulls 20 --alarms-out alarms.json

Runs daily through the dual-write period. Projects both sides onto the same
identity the migration uses — including `last_update`, the field change-only
storage could never have reproduced — and compares MULTISETS over the most recent
N pulls. Any difference raises `alarm:db-parity` (a condition, so it auto-closes
when parity returns), and the cutover from JSONL to Postgres waits on a clean
streak rather than on a date.

It compares the recent window rather than everything so that a daily run stays
cheap as the ledger grows; the migration's own verification is the full-history
check.
"""

import argparse
import collections
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import db  # noqa: E402
from scripts.migrate_jsonl_to_db import _iso, identity, load_jsonl  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ALARM_PARITY = "alarm:db-parity"


def recent_pull_stamps(rows, n):
    return set(sorted({r.get("fetched_at") for r in rows if r.get("fetched_at")})[-n:])


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pulls", type=int, default=20,
                    help="how many of the most recent pulls to compare (default 20)")
    ap.add_argument("--history", default=str(REPO / "data" / "history.jsonl"))
    ap.add_argument("--alarms-out", default=None)
    args = ap.parse_args()

    alarms = []

    def finish(code):
        if args.alarms_out:
            Path(args.alarms_out).write_text(json.dumps(alarms, indent=2) + "\n")
        return code

    conn = db.connect()
    if conn is None:
        print("[note] no DATABASE_URL — parity check skipped", file=sys.stderr)
        return finish(0)

    rows = load_jsonl(args.history)
    if not rows:
        print("[note] no JSONL rows — nothing to compare", file=sys.stderr)
        return finish(0)

    window = recent_pull_stamps(rows, args.pulls)
    src_rows = [r for r in rows if r.get("fetched_at") in window]
    src = collections.Counter(identity(r) for r in src_rows)

    # _iso() converts to UTC before formatting. Doing it by hand here meant a
    # Postgres datetime in the server's local zone was formatted as-is, so no
    # stamp ever matched the window and parity reported zero rows on both a
    # healthy and a broken database — a check that could not fail.
    out = collections.Counter(
        identity(r) for r in db.reconstruct_rows(conn)
        if _iso(r["fetched_at"]) in window)

    missing, extra = src - out, out - src
    print(f"[ok] comparing the last {len(window)} pull(s): "
          f"{sum(src.values()):,} JSONL rows vs {sum(out.values()):,} in Postgres",
          file=sys.stderr)

    if not missing and not extra:
        print("[ok] PARITY CLEAN — identical prices, points and last_update "
              "stamps on every row", file=sys.stderr)
        alarms.append({
            "label": ALARM_PARITY,
            "title": "[alarm] Postgres and the JSONL ledger disagree",
            "body": f"Parity is clean again across the last {len(window)} pulls.",
            "state": "clear", "throttle_hours": 24, "auto_close": True})
        return finish(0)

    lines = []
    for ident, n in list(missing.items())[:10]:
        lines.append(f"- in JSONL, absent from Postgres (x{n}): `{ident}`")
    for ident, n in list(extra.items())[:10]:
        lines.append(f"- in Postgres, absent from JSONL (x{n}): `{ident}`")
    body = (
        f"Comparing the last {len(window)} pulls on "
        f"`(game, book, market, outcome, fetched_at, price, point, last_update, writer)`:\n\n"
        f"- **{sum(missing.values()):,}** rows in the JSONL are missing from Postgres\n"
        f"- **{sum(extra.values()):,}** rows in Postgres are not in the JSONL\n\n"
        + "\n".join(lines) +
        "\n\n**Do not cut over to Postgres while this is open.** The JSONL ledger "
        "is still authoritative during the dual-write period, so no data is at "
        "risk — but the dual-write path is not reproducing it faithfully."
    )
    print(f"::error::db parity: {sum(missing.values())} missing, "
          f"{sum(extra.values())} extra")
    for l in lines[:6]:
        print(f"  {l}", file=sys.stderr)
    alarms.append({
        "label": ALARM_PARITY,
        "title": "[alarm] Postgres and the JSONL ledger disagree",
        "body": body, "state": "firing", "throttle_hours": 24, "auto_close": True})
    return finish(0)       # the alarm is the signal; the run stays green


if __name__ == "__main__":
    sys.exit(main())
