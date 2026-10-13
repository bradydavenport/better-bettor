"""
plan_pull.py — decide what a pull-lines run should request, and whether to run.

    python3 scripts/plan_pull.py --nfl data/nfl.json

Writes `markets=` and `skip=` to $GITHUB_OUTPUT and prints its reasoning.

WHY NOT github.event.schedule
-----------------------------
Market selection used to key off `github.event.schedule`, comparing it against
the cron string that fired. That is EMPTY on workflow_dispatch. The moment the
external doorbell starts dispatching pull-lines, a schedule-keyed rule falls
through to its last branch on every single run — so Tuesday never takes totals
again and nobody notices, because the run is green and the file still updates.

The UTC day of week is the same information with none of that coupling: it is
identical whether the run arrived by schedule, by doorbell, or by hand.

AND NOT THE INPUT DEFAULT
-------------------------
The `markets` input used to declare `default: "total"`. GitHub applies declared
input defaults to API dispatches, so a bare {"ref":"main"} from cron-job.org
would have requested TOTALS ONLY — one credit, no moneyline, no spread — while
looking like a perfectly healthy daily pull. The input now defaults to `auto`,
which explicitly means "decide by day of week", so the absence of a choice can
never masquerade as a choice.

THE RECENCY GUARD
-----------------
The `*/15` schedule is kept as a backup for the doorbell, which means on any day
GitHub actually honours the cron, BOTH fire and the day is pulled twice — double
credits, two commits, two sets of ledger rows for the same day. The guard skips a
run whose work was already done within --min-interval-hours, read from
`_meta.last_pull['pull-lines']`. An explicit markets override always wins, so a
deliberate manual pull is never blocked.
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

OPENER_ISO_DOW = 2          # Tuesday, per date -u +%u / isoweekday()
OPENER_MARKETS = "all"      # the opener takes totals too
DRIFT_MARKETS = "ml,spread"
DEFAULT_MIN_INTERVAL_H = 12
AUTO = "auto"


def _parse(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def choose_markets(override, now):
    """(markets, reason). An explicit override always wins."""
    ov = (override or "").strip()
    if ov and ov.lower() != AUTO:
        return ov, "explicit override"
    if now.isoweekday() == OPENER_ISO_DOW:
        return OPENER_MARKETS, f"{now:%A} (UTC) — the opener, all 3 markets"
    return DRIFT_MARKETS, f"{now:%A} (UTC) — drift pull"


def last_pull_at(nfl_path, writer="pull-lines"):
    """When this writer last pulled, from _meta.last_pull. None if unknown."""
    try:
        doc = json.loads(open(nfl_path).read())
    except (OSError, ValueError):
        return None
    lp = ((doc.get("_meta") or {}).get("last_pull") or {}) if isinstance(doc, dict) else {}
    return _parse(lp.get(writer))


def should_skip(nfl_path, now, min_interval_h, override):
    """(skip, reason). Never skips when markets were chosen explicitly."""
    ov = (override or "").strip()
    if ov and ov.lower() != AUTO:
        return False, "explicit override — never skipped"
    if min_interval_h <= 0:
        return False, "recency guard disabled"
    last = last_pull_at(nfl_path)
    if not last:
        return False, "no recorded previous pull-lines pull"
    age_h = (now - last).total_seconds() / 3600
    if age_h < min_interval_h:
        return True, (f"pull-lines already pulled {age_h:.1f}h ago (<{min_interval_h}h) "
                      f"— the doorbell and the backup schedule both fired")
    return False, f"last pull-lines pull was {age_h:.1f}h ago"


def set_output(key, value):
    path = os.getenv("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(f"{key}={value}\n")
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--override", default=os.getenv("MARKETS_OVERRIDE") or "",
                    help="the workflow_dispatch markets input; 'auto' or empty "
                         "means decide by UTC day of week")
    ap.add_argument("--nfl", default="data/nfl.json",
                    help="file holding _meta.last_pull (default: data/nfl.json)")
    ap.add_argument("--min-interval-hours", type=float,
                    default=DEFAULT_MIN_INTERVAL_H,
                    help=f"skip if pull-lines already pulled this recently "
                         f"(default: {DEFAULT_MIN_INTERVAL_H}; 0 disables)")
    ap.add_argument("--now", default=None, help="override the clock — for tests")
    args = ap.parse_args()

    now = _parse(args.now) or datetime.now(timezone.utc)
    markets, why = choose_markets(args.override, now)
    skip, skip_why = should_skip(args.nfl, now, args.min_interval_hours, args.override)

    print(f"now:     {now:%Y-%m-%dT%H:%M:%SZ} ({now:%A} UTC)")
    print(f"markets: {markets}   ({why})")
    print(f"skip:    {str(skip).lower()}   ({skip_why})")
    set_output("markets", markets)
    set_output("skip", str(skip).lower())
    if skip:
        print(f"::notice::pull-lines skipped — {skip_why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
