"""
backfill_history.py — reconstruct a line-history ledger from git history.

One-off, safe to rerun: data/history_backfill.jsonl is overwritten completely on
every run. It is derived data, not the ledger. The real ledger is
data/history.jsonl, which fetch_lines.py appends to and never rewrites; this
script deliberately writes somewhere else so a backfill can never clobber it.

    python scripts/backfill_history.py            # -> data/history_backfill.jsonl
    python scripts/backfill_history.py --dry-run  # tallies only, writes nothing

WHAT THIS CAN AND CANNOT RECOVER
--------------------------------
data/nfl.json is NOT a per-book feed. It is a single-book normalized view: one
flattened row per game carrying Pinnacle's numbers, because fetch_lines.py has
always requested `bookmakers=pinnacle` (one book). So:

  * Recoverable: Pinnacle h2h / spreads / totals, both outcomes per market, with
    real prices and points. Every snapshot in history is Pinnacle-only — checked,
    not assumed.
  * NOT recoverable: any other book. DraftKings, FanDuel and the rest were never
    fetched before this change, so there is nothing in git to recover. No rows
    are invented for them.
  * NOT recoverable: per-market `last_update`. nfl.json stores one
    bookmaker-level `book_last_update` per game, not one per market, so all of a
    game's markets share it. The live ledger records the finer market-level stamp.
  * NOT recoverable: movement between commits. The cron commits roughly daily, so
    this is a daily sample, not a tick history.

THREE SCHEMAS
-------------
nfl.json has changed shape twice, and all three versions are in history:

  v1  bare JSON list, per-game `last_update` + `fetched_at`, no `_meta`
  v2  {"_meta", "games"}, per-game `book_last_update`, no per-market stamps
  v3  as v2 plus `_meta.pulled_markets` and per-market `moneyline_at` /
      `spread_at` / `total_at`

TIMESTAMPS
----------
`fetched_at` comes from the per-market `*_at` stamp first, then `_meta.fetched_at`
(or v1's per-game `fetched_at`), then the commit time. The per-market stamp
matters: because fetch_lines.py runs with --merge, a snapshot carries forward
markets it did not pull, so `_meta.fetched_at` can be days newer than the line
beside it. Totals are the live example — pulled once around 2026-09-06, then
re-committed unchanged for about a week while `pulled_markets` stayed
["h2h","spreads"]. Stamping those with `_meta.fetched_at` would claim a line was
quoted on a day nobody asked for it.

SPREAD SIGN
-----------
nfl.json inverts the API's convention (`spread` is the HOME line, POSITIVE =
home favored; see fetch_lines.py's normalize()). The ledger keeps the API's
convention, where `point` is that outcome's own handicap and negative == favored.
So the home row gets -spread and the away row gets +spread.
"""

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from fetch_lines import HISTORY_FIELDS          # noqa: E402  - schema parity

TRACKED = "data/nfl.json"
DEFAULT_OUT = "data/history_backfill.jsonl"
SOURCE = "git_backfill"


def git(*args):
    r = subprocess.run(["git", "-C", str(REPO), *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def commits_touching(path):
    """[(sha, commit_time_iso)] oldest first, so the ledger reads forward."""
    out = git("log", "--format=%H %ct", "--follow", "--reverse", "--", path)
    rows = []
    for line in out.splitlines():
        sha, _, ct = line.partition(" ")
        if not sha:
            continue
        stamp = datetime.fromtimestamp(int(ct), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        rows.append((sha, stamp))
    return rows


def load_snapshot(sha, path):
    """(games, meta, schema) for one commit, or (None, None, reason)."""
    try:
        blob = git("show", f"{sha}:{path}")
    except RuntimeError:
        return None, None, "missing"
    try:
        doc = json.loads(blob)
    except ValueError:
        return None, None, "unparseable"
    if isinstance(doc, list):
        return doc, {}, "v1_bare_list"
    if not isinstance(doc, dict) or "games" not in doc:
        return None, None, "unknown_shape"
    meta = doc.get("_meta") or {}
    schema = "v3_per_market_stamps" if meta.get("pulled_markets") else "v2_wrapped"
    return doc["games"], meta, schema


def market_rows(g, meta, commit_time):
    """Ledger rows for one game. Emits an outcome only when it has a price.

    Carried-forward markets are SKIPPED, not stamped. When `_meta.pulled_markets`
    says a snapshot did not pull a market, the numbers sitting in that market are
    byte-identical leftovers from an earlier pull that --merge carried across, and
    the snapshot's own `fetched_at` never applied to them. Emitting them would
    assert an observation that never happened — a Sep 6 total re-dated to Sep 19.
    Snapshots predating `pulled_markets` (v1/v2) pulled all three markets, so
    nothing is skipped there.
    """
    pulled = meta.get("pulled_markets")
    snapshot_at = meta.get("fetched_at") or g.get("fetched_at") or commit_time
    # v1 kept the book stamp per game as `last_update`; v2/v3 renamed it.
    last_update = g.get("book_last_update") or g.get("last_update")

    home, away = g.get("home_team"), g.get("away_team")
    spread = g.get("spread")
    total = g.get("total")

    plans = [
        ("h2h", g.get("moneyline_at"), [
            (home, g.get("moneyline_home"), None),
            (away, g.get("moneyline_away"), None),
        ]),
        ("spreads", g.get("spread_at"), [
            # back to the API's convention: negative point == favored
            (home, g.get("spread_price_home"),
             -spread if spread is not None else None),
            (away, g.get("spread_price_away"),
             spread if spread is not None else None),
        ]),
        ("totals", g.get("total_at"), [
            ("Over", g.get("total_over_price"), total),
            ("Under", g.get("total_under_price"), total),
        ]),
    ]

    rows, stamp_src = [], Counter()
    for market, market_at, outcomes in plans:
        if pulled is not None and market not in pulled and not market_at:
            stamp_src[f"skipped_carried_forward_{market}"] += sum(
                1 for _, price, _ in outcomes if price is not None)
            continue
        fetched_at = market_at or snapshot_at
        for outcome, price, point in outcomes:
            if price is None:
                continue
            stamp_src["per_market_at" if market_at else "snapshot_fallback"] += 1
            rows.append({
                "fetched_at": fetched_at,
                "last_update": last_update,
                "game_id": g.get("game_id"),
                "commence_time": g.get("commence_time"),
                "home": home,
                "away": away,
                "book": g.get("book"),
                "market": market,
                "outcome": outcome,
                "price": int(price),
                "point": float(point) if point is not None else None,
                "source": SOURCE,
            })
    return rows, stamp_src


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"output path, overwritten each run (default: {DEFAULT_OUT})")
    ap.add_argument("--path", default=TRACKED,
                    help=f"tracked file to walk (default: {TRACKED})")
    ap.add_argument("--dry-run", action="store_true",
                    help="tally only, write nothing")
    args = ap.parse_args()

    commits = commits_touching(args.path)
    if not commits:
        sys.exit(f"[fatal] no commits touch {args.path}")
    print(f"[ok] {len(commits)} commits touch {args.path} "
          f"({commits[0][1]} -> {commits[-1][1]})", file=sys.stderr)

    all_rows = []
    schemas, markets, books, stamps, skipped = Counter(), Counter(), Counter(), Counter(), Counter()

    for sha, commit_time in commits:
        games, meta, schema = load_snapshot(sha, args.path)
        if games is None:
            skipped[schema] += 1
            print(f"[warn] {sha[:8]} skipped ({schema})", file=sys.stderr)
            continue
        schemas[schema] += 1
        n = 0
        for g in games:
            rows, stamp_src = market_rows(g, meta, commit_time)
            all_rows.extend(rows)
            stamps.update(stamp_src)
            for r in rows:
                markets[r["market"]] += 1
                books[r["book"]] += 1
            n += len(rows)
        print(f"  {sha[:8]}  {commit_time}  {schema:22s} games={len(games):2d} rows={n:3d}",
              file=sys.stderr)

    # schema parity with the live ledger — a drift here would split the dataset
    for r in all_rows[:1]:
        extra, missing = set(r) - set(HISTORY_FIELDS), set(HISTORY_FIELDS) - set(r)
        if extra or missing:
            sys.exit(f"[fatal] schema drift vs fetch_lines.HISTORY_FIELDS: "
                     f"extra={extra} missing={missing}")

    dupes = len(all_rows) - len({
        (r["book"], r["game_id"], r["market"], r["outcome"],
         r["price"], r["point"], r["fetched_at"]) for r in all_rows})

    print(f"\n[ok] {len(all_rows)} rows", file=sys.stderr)
    print(f"     schemas: {dict(schemas)}", file=sys.stderr)
    print(f"     markets: {dict(markets)}", file=sys.stderr)
    print(f"     books:   {dict(books)}   <- Pinnacle only; no other book is in git",
          file=sys.stderr)
    print(f"     stamps:  {dict(stamps)}", file=sys.stderr)
    print(f"     exact duplicate rows: {dupes} "
          f"({100 * dupes // max(len(all_rows), 1)}% — --merge re-commits "
          f"carried-forward markets unchanged; kept, not dropped)", file=sys.stderr)
    if skipped:
        print(f"     skipped commits: {dict(skipped)}", file=sys.stderr)

    if args.dry_run:
        print("[ok] --dry-run: nothing written", file=sys.stderr)
        return

    out = Path(REPO / args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w") as f:                    # "w": derived, rebuilt each run
        for r in all_rows:
            f.write(json.dumps(r) + "\n")
    print(f"[ok] wrote {out} ({os.path.getsize(out)} bytes)", file=sys.stderr)


if __name__ == "__main__":
    main()
