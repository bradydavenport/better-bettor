"""
replay_outputs.py — re-apply one run's outputs onto whatever origin/main now holds.

Used when a push is rejected because another workflow committed mid-run. Instead
of rebasing (which can conflict, and did so on nfl.json and sniped.json, neither
of which has a merge driver), the caller resets hard to origin/main and replays:

    git fetch origin && git reset --hard origin/main
    python3 scripts/replay_outputs.py --rows new_rows.jsonl --ids sniped_ids.json \
        --nfl nfl.json --usage usage.json
    git add ... && git commit && git push

Reset-plus-replay cannot produce a conflict, because nothing is being merged
textually. Each file gets the rule that is actually correct for it:

  history.jsonl   APPEND. Both sides' rows are real observations of real prices;
                  neither supersedes the other. Deduped on (fetched_at, writer)
                  in case a push landed but reported failure.

  sniped.json     SET UNION. A game either had its close captured or it did not,
                  and both sides' knowledge is additive. The earlier sniped_at
                  wins on collision, since that is when it actually happened.

  nfl.json        NEWEST WINS on _meta.fetched_at. NOT "ours wins": a slow run
                  replaying onto a newer origin would roll the file backwards and
                  silently discard a fresher pull. `_meta.last_pull` is merged
                  key-by-key taking the max either way, so a losing file still
                  contributes its writer's timestamp — otherwise the dead-man's
                  switch would forget that pull-lines ever ran.

  usage.json      NEWEST WINS on updated_at, same reasoning. The credit counters
                  derive from the API's own response headers, so the most recent
                  write is the authoritative one.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return default


def _iso_key(doc, field):
    """A sortable timestamp from a data file, or '' when absent/unreadable."""
    try:
        return (doc.get("_meta") or {}).get(field) or ""
    except AttributeError:
        return ""


def replay_rows(rows_file, dest):
    """Append our ledger rows, unless they are demonstrably already there."""
    ours = [l for l in Path(rows_file).read_text().splitlines() if l.strip()]
    if not ours:
        return 0, "no rows to replay"
    # identity of this run's batch: every row shares one (fetched_at, writer)
    try:
        first = json.loads(ours[0])
        ident = (first.get("fetched_at"), first.get("writer"))
    except ValueError:
        ident = (None, None)

    dest = Path(dest)
    if dest.exists() and ident != (None, None):
        # only the tail can plausibly hold them; bounded so this stays cheap
        size = dest.stat().st_size
        with open(dest, "rb") as f:
            if size > 8 * 1024 * 1024:
                f.seek(size - 8 * 1024 * 1024)
                f.readline()
            tail = f.read().decode("utf-8", "replace")
        for line in tail.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (r.get("fetched_at"), r.get("writer")) == ident:
                return 0, (f"rows for {ident} are already present — the push "
                           f"landed despite reporting failure; not duplicating")

    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "a") as f:
        for line in ours:
            f.write(line + "\n")
    return len(ours), f"appended {len(ours)} rows"


def replay_ids(ids_file, dest):
    """Union our sniped game_ids into whatever is there, earliest capture wins."""
    ours = _load(ids_file, {})
    if not isinstance(ours, dict) or not ours:
        return 0, "no sniped ids to replay"
    doc = _load(dest, {})
    sniped = (doc.get("sniped") or {}) if isinstance(doc, dict) else {}
    added = 0
    for gid, rec in ours.items():
        if gid not in sniped:
            sniped[gid] = rec
            added += 1
        else:
            # keep the earlier capture: that is when the close was actually taken
            if (rec.get("sniped_at") or "") < (sniped[gid].get("sniped_at") or ""):
                sniped[gid] = rec
    meta = doc.get("_meta") if isinstance(doc, dict) else None
    out = {"_meta": meta or {}, "sniped": dict(sorted(
        sniped.items(), key=lambda kv: kv[1].get("commence_time") or ""))}
    out["_meta"]["count"] = len(sniped)
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_text(json.dumps(out, indent=2) + "\n")
    return added, f"union: {added} new, {len(sniped)} tracked"


def _merge_last_pull(a, b):
    """Max per writer key across both files."""
    out = {}
    for src in (a or {}, b or {}):
        if not isinstance(src, dict):
            continue
        for k, v in src.items():
            if not isinstance(v, str):
                continue
            if k not in out or v > out[k]:
                out[k] = v
    return out


def replay_newest(ours_file, dest, stamp_field):
    """Keep whichever file is newer, but never lose a writer's last_pull entry."""
    ours = _load(ours_file, None)
    if ours is None:
        return f"{Path(dest).name}: nothing saved to replay"
    theirs = _load(dest, None)
    ours_ts, theirs_ts = _iso_key(ours, stamp_field), _iso_key(theirs or {}, stamp_field)

    winner, verdict = (ours, "ours is newer") if ours_ts >= theirs_ts else \
                      (theirs, "origin is NEWER — keeping theirs, not rolling back")
    if isinstance(winner, dict) and isinstance(winner.get("_meta"), dict):
        merged = _merge_last_pull(
            (ours or {}).get("_meta", {}).get("last_pull") if isinstance(ours, dict) else {},
            (theirs or {}).get("_meta", {}).get("last_pull") if isinstance(theirs, dict) else {})
        if merged:
            winner["_meta"]["last_pull"] = merged
    Path(dest).parent.mkdir(parents=True, exist_ok=True)
    Path(dest).write_text(json.dumps(winner, indent=2) + "\n")
    return (f"{Path(dest).name}: {verdict} "
            f"(ours {ours_ts or 'n/a'} vs origin {theirs_ts or 'n/a'})")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rows", help="this run's new history rows (.jsonl)")
    ap.add_argument("--ids", help="this run's sniped game_ids (.json)")
    ap.add_argument("--nfl", help="this run's nfl.json")
    ap.add_argument("--usage", help="this run's usage.json")
    ap.add_argument("--data-dir", default="data",
                    help="destination directory (default: data)")
    args = ap.parse_args()

    d = Path(args.data_dir)
    print("[replay] re-applying this run's outputs onto the current checkout",
          file=sys.stderr)

    if args.rows:
        n, msg = replay_rows(args.rows, d / "history.jsonl")
        print(f"[replay] history.jsonl: {msg}", file=sys.stderr)
    if args.ids:
        n, msg = replay_ids(args.ids, d / "sniped.json")
        print(f"[replay] sniped.json: {msg}", file=sys.stderr)
    if args.nfl:
        print(f"[replay] {replay_newest(args.nfl, d / 'nfl.json', 'fetched_at')}",
              file=sys.stderr)
    if args.usage:
        # usage.json keeps its stamp at the top level, not under _meta
        ours, theirs = _load(args.usage, None), _load(d / "usage.json", None)
        if ours is not None:
            o, t = (ours or {}).get("updated_at", ""), (theirs or {}).get("updated_at", "")
            winner = ours if o >= t else theirs
            verdict = "ours is newer" if o >= t else "origin is NEWER — keeping theirs"
            (d / "usage.json").write_text(json.dumps(winner, indent=2) + "\n")
            print(f"[replay] usage.json: {verdict} (ours {o or 'n/a'} vs "
                  f"origin {t or 'n/a'})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
