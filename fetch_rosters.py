"""
fetch_rosters.py — pull weekly NFL personnel (starting QB + absences).

Companion to fetch_lines.py. data/nfl.json says what the market thinks; this
says who is actually playing. Downstream survivor models had no personnel data
at all, so anything reasoning on top of them was guessing at "who is starting at
QB". This file exists to make that guess unnecessary — and a wrong guess here is
worse than no answer, so every field is either sourced or null. Never inferred.

Scope is deliberately small: starters and absences, not 53-man rosters.
Per team: the QB1, players OUT or DOUBTFUL, and players on IR/PUP.

Sources (both free, keyless, no account)
----------------------------------------
QB1  -> nflverse-data `depth_charts` release, depth_charts_<season>.csv.gz
        https://github.com/nflverse/nflverse-data/releases/tag/depth_charts
        Snapshot-shaped (a `dt` column, not `week`), rewritten ~2x/day. Because
        one file carries every prior snapshot, `changed_from_last_week` is a real
        diff against a ~7-day-old snapshot rather than a guess — true on the very
        first run, with no prior rosters.json to lean on.

OUT / DOUBTFUL / IR -> ESPN's public injuries JSON
        https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/injuries
        One call covers all 32 teams and carries a per-player report timestamp,
        position, injury type and an IR return date.

Why not nflverse for injuries, given it is the better-behaved source? Checked,
not assumed: injuries_<season>.csv carries only game-status designations (Out /
Doubtful / Questionable) — it has no IR or PUP rows and no return dates, which
is a third of the scope. ESPN has all of it. nflverse IS used for injuries on
backfill (--week), where it wins outright: ESPN's endpoint is live-only with no
history, nflverse keeps every past week's official report.

ESPN's endpoint is undocumented and can change shape without notice. When it
does, this script writes nulls and says so in _meta.notes — it never falls back
to stale or invented data.

Output
------
    {
      "_meta": {
        "fetched_at": "2026-09-19T12:00:00Z", "season": 2026, "week": 2,
        "source": "nflverse depth_charts (QB) + ESPN injuries (OUT/DOUBTFUL/IR)",
        "sources": { "qb": {...}, "injuries": {...} },   # per-field provenance
        "team_count": 32, "teams_missing_qb": [], "unmapped_source_teams": [],
        "notes": "field glossary + what null means + how to spot stale data"
      },
      "teams": {
        "PHI": {
          "qb": {"name": "Jalen Hurts", "changed_from_last_week": false,
                 "previous_name": null, "status": null,
                 "as_of": "2026-09-19T11:56:08Z"},
          "out":      [{"name": "...", "pos": "CB", "reason": "Achilles (Surgery)",
                        "as_of": "..."}],
          "doubtful": [],
          "ir":       [{"name": "...", "pos": "S", "status": "Injured Reserve",
                        "eligible_week": 7, "expected_return": "2026-10-18",
                        "as_of": "..."}],
          "updated_at": "2026-09-19T12:00:00Z"
        }
      }
    }

Team keys are the same abbreviations fetch_lines.py writes into data/nfl.json,
so the two files join on team without a translation step. Both upstreams
disagree with that set in exactly one place each (nflverse says LA for the Rams,
ESPN says WSH for Washington); see TEAM_ALIASES and test_rosters.py.

Change-only writes
------------------
The file is rewritten only when `teams` actually changes, so `git log
data/rosters.json` reads as a changelog of personnel movement instead of a
heartbeat. Each team's `updated_at` carries forward untouched when that team
did not change, which is what makes a stale block identifiable as stale: a team
whose updated_at is four days old has had no reported movement in four days.
`_meta.fetched_at` is when the *current content* was first observed, not when
the script last ran — the workflow run log is the record of checks. --force-write
rewrites regardless.

Usage:
    python fetch_rosters.py --out data/rosters.json          # current week, live
    python fetch_rosters.py --week 1 --out data/rosters.json # backfill week 1
    python fetch_rosters.py --dry-run                        # print, write nothing

Env: none. No API key, for any of this.
"""

import argparse
import csv
import gzip
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# --------------------------------------------------------------------------- #
# teams
# --------------------------------------------------------------------------- #

# The abbreviations fetch_lines.py writes (see NFL_ABBR there). This file must
# use the exact same set or the two data files stop joining.
CANON_TEAMS = frozenset({
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LAC", "LAR", "LV", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
    "TEN", "WAS",
})

# Upstream spelling -> our spelling. Only two of these are live mismatches
# today (verified against both feeds): nflverse writes LA for the Rams, ESPN
# writes WSH for Washington. The rest are defensive — other nflverse/PFR/ESPN
# exports and older seasons use them, and a silently dropped team is worse than
# a dead map entry. Relocations map to the current franchise.
TEAM_ALIASES = {
    "LA": "LAR", "RAM": "LAR", "STL": "LAR",      # nflverse says LA
    "WSH": "WAS", "WFT": "WAS",                    # ESPN says WSH
    "JAC": "JAX",
    "AZ": "ARI", "ARZ": "ARI",
    "GNB": "GB", "KAN": "KC", "NWE": "NE", "NOR": "NO", "SFO": "SF",
    "TAM": "TB", "TBB": "TB", "LVR": "LV", "OAK": "LV",
    "SD": "LAC", "SDG": "LAC", "CLV": "CLE", "BLT": "BAL", "HST": "HOU",
}


def norm_team(abbr):
    """Upstream team abbreviation -> the spelling data/nfl.json uses.

    Returns None for anything unrecognized. Callers surface that in
    _meta.unmapped_source_teams rather than dropping the row quietly — an
    unmapped team means a source changed its spelling, which is exactly the
    kind of drift that silently empties a team's block.
    """
    if not abbr:
        return None
    key = abbr.strip().upper()
    if key in CANON_TEAMS:
        return key
    return TEAM_ALIASES.get(key)


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #

ESPN_INJURIES = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/injuries"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
NFLVERSE_DEPTH = ("https://github.com/nflverse/nflverse-data/releases/download/"
                  "depth_charts/depth_charts_{season}.csv.gz")
NFLVERSE_INJURIES = ("https://github.com/nflverse/nflverse-data/releases/download/"
                     "injuries/injuries_{season}.csv.gz")

TIMEOUT = 60
UA = {"User-Agent": "better-bettor/1.0 (+https://github.com/; personnel fetcher)"}

# ESPN status -> our bucket. Anything not listed and not Active/Questionable is
# treated as a roster-move absence (the `ir` list) with its status kept verbatim,
# so a designation we have not seen before shows up instead of vanishing.
GAMEDAY_STATUS = {"out": "out", "doubtful": "doubtful"}
NOT_ABSENT = {"active", "questionable", "probable", "day-to-day"}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s):
    """ESPN and nflverse mix 'Z' forms and bare dates. None on anything odd."""
    if not s:
        return None
    s = s.strip().replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%d"):
        try:
            dt = datetime.fromisoformat(s) if fmt is None else datetime.strptime(s, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _get(url, **params):
    """GET with a readable error instead of a traceback."""
    try:
        r = requests.get(url, params=params or None, headers=UA, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise SourceError(f"{url} unreachable: {e}")
    if r.status_code != 200:
        raise SourceError(f"{url} -> HTTP {r.status_code}: {r.text[:200]}")
    return r


class SourceError(RuntimeError):
    """One upstream failed. Callers degrade to nulls rather than dying."""


# --------------------------------------------------------------------------- #
# schedule / week
# --------------------------------------------------------------------------- #


def fetch_calendar():
    """(season, current_week, [(week, start, end), ...]) from ESPN's scoreboard.

    The calendar is what maps a date to a week number — needed both to label
    this pull and to turn an IR return *date* into an `eligible_week`.
    """
    d = _get(ESPN_SCOREBOARD).json()
    lg = (d.get("leagues") or [{}])[0]
    season = (lg.get("season") or {}).get("year")
    weeks = []
    for block in lg.get("calendar") or []:
        if not str(block.get("label", "")).lower().startswith("regular"):
            continue
        for e in block.get("entries") or []:
            start, end = _parse_iso(e.get("startDate")), _parse_iso(e.get("endDate"))
            if start and end and str(e.get("value", "")).isdigit():
                weeks.append((int(e["value"]), start, end))
    weeks.sort()
    now = datetime.now(timezone.utc)
    current = week_for(now, weeks) or (d.get("week") or {}).get("number")
    return season, current, weeks


def week_for(dt, weeks):
    """Which regular-season week contains `dt`. None if outside the season."""
    if not dt:
        return None
    for num, start, end in weeks:
        if start <= dt <= end:
            return num
    return None


def week_bounds(week, weeks):
    for num, start, end in weeks:
        if num == week:
            return start, end
    return None, None


# --------------------------------------------------------------------------- #
# QB1 — nflverse depth charts
# --------------------------------------------------------------------------- #


def fetch_depth_chart_qbs(season):
    """[{dt, team, player_name, pos_rank}] for QBs only, newest season file.

    The full file is ~50MB uncompressed and mostly non-QB rows, so it is
    streamed and filtered rather than loaded.
    """
    r = _get(NFLVERSE_DEPTH.format(season=season))
    rows = []
    with gzip.open(io.BytesIO(r.content), "rt", newline="") as fh:
        for rec in csv.DictReader(fh):
            if rec.get("pos_abb") == "QB" and rec.get("pos_rank") == "1":
                rows.append(rec)
    if not rows:
        raise SourceError(f"depth_charts_{season}: no QB1 rows "
                          f"(schema changed? columns are position-shaped, not week-shaped)")
    return rows


def qb1_at(rows, as_of):
    """{team: (player_name, snapshot_dt)} from the newest snapshot <= as_of."""
    stamps = sorted({r["dt"] for r in rows if _parse_iso(r["dt"]) and _parse_iso(r["dt"]) <= as_of})
    if not stamps:
        return {}, None
    snap = stamps[-1]
    out = {}
    for r in rows:
        if r["dt"] != snap:
            continue
        team = norm_team(r.get("team"))
        # A team should have exactly one QB1 per snapshot; if a source ever
        # emits two, keep the first and let the count check in build() show it.
        if team and team not in out:
            out[team] = r.get("player_name") or None
    return out, snap


def qb1_week_earlier(rows, snap_dt):
    """{team: name} from the newest snapshot at least 6 days before `snap_dt`.

    6 rather than 7 so a Wednesday pull still compares against the *previous*
    week's chart and not a same-week one. Returns {} when the file has no
    snapshot that old — callers then write null, not False.
    """
    cutoff = snap_dt - timedelta(days=6)
    prior, _ = qb1_at(rows, cutoff)
    return prior


# --------------------------------------------------------------------------- #
# injuries — ESPN (live) / nflverse (backfill)
# --------------------------------------------------------------------------- #


def fetch_espn_injuries(weeks, current_week):
    """(by_team, meta, unmapped) — OUT/DOUBTFUL/IR plus a QB status index.

    by_team[abbr] = {"out": [...], "doubtful": [...], "ir": [...],
                     "status": {player_name: designation}}
    """
    payload = _get(ESPN_INJURIES).json()
    by_team, unmapped = {}, set()
    for group in payload.get("injuries") or []:
        for item in group.get("injuries") or []:
            ath = item.get("athlete") or {}
            team = norm_team(((ath.get("team") or {}).get("abbreviation")))
            if not team:
                raw = ((ath.get("team") or {}).get("abbreviation")
                       or group.get("displayName") or "?")
                unmapped.add(str(raw))
                continue
            slot = by_team.setdefault(
                team, {"out": [], "doubtful": [], "ir": [], "status": {}})

            name = ath.get("displayName")
            pos = ((ath.get("position") or {}).get("abbreviation")) or None
            status = (item.get("status") or "").strip()
            as_of = item.get("date")
            if not name:
                continue

            slot["status"][name] = status or None

            key = status.lower()
            if key in NOT_ABSENT or not status:
                continue

            details = item.get("details") or {}
            reason = _reason(details)

            if key in GAMEDAY_STATUS:
                slot[GAMEDAY_STATUS[key]].append({
                    "name": name, "pos": pos, "reason": reason, "as_of": as_of,
                })
            else:
                # IR / PUP / NFI / suspension / anything new
                ret = details.get("returnDate")
                elig = week_for(_parse_iso(ret), weeks)
                # ESPN reuses returnDate as "next game" for non-IR designations;
                # only a date past the current week is a real return estimate.
                if elig is not None and current_week is not None and elig <= current_week:
                    elig, ret = None, None
                slot["ir"].append({
                    "name": name, "pos": pos, "status": status,
                    "reason": reason, "eligible_week": elig,
                    "expected_return": ret, "as_of": as_of,
                })

    meta = {
        "name": "ESPN public injuries API",
        "url": ESPN_INJURIES,
        "fetched_at": _now_iso(),
        "source_timestamp": payload.get("timestamp"),
        "note": "undocumented ESPN endpoint; no key required, no stability guarantee",
    }
    return by_team, meta, sorted(unmapped)


def _reason(details):
    """'Achilles (Surgery)' from ESPN's type/detail pair. None when unreported."""
    if not details:
        return None
    typ = (details.get("type") or "").strip()
    detail = (details.get("detail") or "").strip()
    if detail.lower() in ("not specified", ""):
        detail = ""
    if typ and detail:
        return f"{typ} ({detail})"
    return typ or detail or None


def fetch_nflverse_injuries(season, week):
    """Historical official report for one past week. OUT/DOUBTFUL only.

    Used for --week backfill: ESPN's endpoint has no history. nflverse carries
    no IR/PUP rows at all, so `ir` comes back empty and _meta says why.
    """
    r = _get(NFLVERSE_INJURIES.format(season=season))
    by_team, unmapped = {}, set()
    seen_week = False
    with gzip.open(io.BytesIO(r.content), "rt", newline="") as fh:
        for rec in csv.DictReader(fh):
            if rec.get("season_type") != "REG" or str(rec.get("week")) != str(week):
                continue
            seen_week = True
            status = (rec.get("report_status") or "").strip().lower()
            if status not in GAMEDAY_STATUS:
                continue
            team = norm_team(rec.get("team"))
            if not team:
                unmapped.add(str(rec.get("team")))
                continue
            slot = by_team.setdefault(
                team, {"out": [], "doubtful": [], "ir": [], "status": {}})
            reason = " / ".join(x for x in (rec.get("report_primary_injury"),
                                            rec.get("report_secondary_injury")) if x)
            entry = {
                "name": rec.get("full_name"),
                "pos": rec.get("position") or None,
                "reason": reason or None,
                "as_of": None,   # the weekly export carries no per-row timestamp
            }
            slot[GAMEDAY_STATUS[status]].append(entry)
            slot["status"][entry["name"]] = rec.get("report_status")

    if not seen_week:
        raise SourceError(f"injuries_{season}: no REG rows for week {week} "
                          f"(nflverse has not published that week yet)")
    meta = {
        "name": "nflverse-data injuries release",
        "url": NFLVERSE_INJURIES.format(season=season),
        "fetched_at": _now_iso(),
        "note": ("official weekly injury report; carries game-status designations "
                 "only — no IR/PUP rows and no return dates, so `ir` is empty on "
                 "backfilled weeks"),
    }
    return by_team, meta, sorted(unmapped)


# --------------------------------------------------------------------------- #
# assembly
# --------------------------------------------------------------------------- #


def build_teams(qbs, qb_snap, prior_qbs, injuries):
    """Per-team blocks, minus updated_at (added by carry_updated_at)."""
    teams = {}
    for abbr in sorted(CANON_TEAMS):
        inj = injuries.get(abbr) or {}
        name = qbs.get(abbr)

        if name:
            prev = prior_qbs.get(abbr)
            # No comparable earlier snapshot -> null. Not False: "we don't know"
            # and "no change" are different answers and only one of them is safe.
            changed = None if not prior_qbs else (prev is not None and prev != name)
            qb = {
                "name": name,
                "changed_from_last_week": changed,
                "previous_name": prev if changed else None,
                "status": (inj.get("status") or {}).get(name),
                "as_of": qb_snap,
            }
        else:
            qb = None

        teams[abbr] = {
            "qb": qb,
            "out": sorted(inj.get("out") or [], key=lambda p: p["name"] or ""),
            "doubtful": sorted(inj.get("doubtful") or [], key=lambda p: p["name"] or ""),
            "ir": sorted(inj.get("ir") or [], key=lambda p: p["name"] or ""),
        }
    return teams


def carry_updated_at(old_teams, new_teams, now):
    """Stamp only the teams whose content actually changed.

    Mirrors merge_forward() in fetch_lines.py: an untouched field keeps its old
    timestamp, so `updated_at` answers "when did this team last move" rather
    than "when did the script last run". Returns (teams, changed_abbrs).
    """
    changed = []
    for abbr, block in new_teams.items():
        prev = (old_teams or {}).get(abbr) or {}
        prev_body = {k: v for k, v in prev.items() if k != "updated_at"}
        if prev_body == block:
            block["updated_at"] = prev.get("updated_at") or now
        else:
            block["updated_at"] = now
            if prev:
                changed.append(abbr)
    return new_teams, changed


NOTES = (
    "Personnel for the listed week. `qb.name` is the depth-chart QB1 at "
    "`qb.as_of` — a depth chart, not a confirmed start, so cross-check "
    "`qb.status` (the player's own injury designation: null = not on the "
    "injury report at all, \"Active\" = on the report but cleared to play, and "
    "anything else means the charted QB1 may not be the man who takes the "
    "first snap). `qb.changed_from_last_week` compares that snapshot against the "
    "newest one at least 6 days older; null means no comparable earlier "
    "snapshot existed, which is NOT the same as false. `out` and `doubtful` "
    "are the official game-status designations; Questionable is deliberately "
    "excluded as too noisy to act on. `ir` covers roster-move absences "
    "(Injured Reserve, PUP, and any designation the source reports verbatim in "
    "`status`); `eligible_week` is derived from `expected_return` and is an "
    "estimate, not an official designation. It is null whenever that date is "
    "missing, falls inside the current week (the source reuses the next game "
    "date as a placeholder), or lands outside the regular season — so a null "
    "`eligible_week` next to a populated `expected_return` means the return is "
    "not expected this season. Every null means the source did not "
    "report it; nothing here is inferred or carried over from memory. Times are "
    "UTC ISO-8601. Per-team `updated_at` is when that team's block last CHANGED, "
    "so an old stamp means no reported movement, not a failed pull; "
    "`_meta.fetched_at` likewise marks when the current content first appeared "
    "(the file is only rewritten on change). Team keys match data/nfl.json."
)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="data/rosters.json",
                    help="write JSON here (default: data/rosters.json; - for stdout)")
    ap.add_argument("--week", type=int, default=None,
                    help="target regular-season week (default: the current one). "
                         "A past week backfills: QB1 from that week's last depth "
                         "chart snapshot, designations from nflverse's archived "
                         "official report (no IR data exists for past weeks)")
    ap.add_argument("--season", type=int, default=None,
                    help="season year (default: whatever ESPN says is current)")
    ap.add_argument("--injury-source", default="auto",
                    choices=("auto", "espn", "nflverse"),
                    help="auto (default) = ESPN live for the current week, "
                         "nflverse archive for a past one")
    ap.add_argument("--force-write", action="store_true",
                    help="rewrite --out even when no team changed")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the result to stdout and write nothing")
    args = ap.parse_args()

    now = _now_iso()

    # ---- when are we ------------------------------------------------------ #
    try:
        season, current_week, weeks = fetch_calendar()
    except SourceError as e:
        sys.exit(f"[fatal] cannot resolve the NFL calendar: {e}")
    season = args.season or season
    week = args.week or current_week
    if week is None:
        sys.exit("[fatal] no current regular-season week (offseason?) — pass --week")
    if not season:
        sys.exit("[fatal] could not determine the season — pass --season")

    _, week_end = week_bounds(week, weeks)
    # For a past week, look at the chart as it stood at that week's end.
    as_of = min(datetime.now(timezone.utc), week_end) if week_end else datetime.now(timezone.utc)
    backfill = week != current_week
    print(f"[ok] season {season}, week {week}"
          + (f" (backfill; current is {current_week})" if backfill else " (current)"),
          file=sys.stderr)

    # ---- QB1 -------------------------------------------------------------- #
    qb_meta = {"name": "nflverse-data depth_charts release",
               "url": NFLVERSE_DEPTH.format(season=season)}
    degraded = []
    try:
        rows = fetch_depth_chart_qbs(season)
        qbs, qb_snap = qb1_at(rows, as_of)
        snap_dt = _parse_iso(qb_snap)
        prior_qbs = qb1_week_earlier(rows, snap_dt) if snap_dt else {}
        qb_meta.update(snapshot_at=qb_snap,
                       compared_against=None,
                       fetched_at=now)
        if prior_qbs and snap_dt:
            _, prior_snap = qb1_at(rows, snap_dt - timedelta(days=6))
            qb_meta["compared_against"] = prior_snap
        print(f"[ok] QB1 for {len(qbs)} teams @ {qb_snap}"
              + ("" if prior_qbs else "  (no earlier snapshot — change flags null)"),
              file=sys.stderr)
    except SourceError as e:
        qbs, prior_qbs, qb_snap = {}, {}, None
        qb_meta.update(fetched_at=now, error=str(e))
        degraded.append(f"QB unavailable ({e}); every qb is null")
        print(f"[warn] {e}", file=sys.stderr)

    # ---- absences --------------------------------------------------------- #
    use = args.injury_source
    if use == "auto":
        use = "nflverse" if backfill else "espn"
    try:
        if use == "espn":
            if backfill:
                print("[note] --injury-source espn on a past week returns TODAY's "
                      "designations, not that week's", file=sys.stderr)
            injuries, inj_meta, unmapped = fetch_espn_injuries(weeks, current_week)
        else:
            injuries, inj_meta, unmapped = fetch_nflverse_injuries(season, week)
            degraded.append("backfilled week: source carries no IR/PUP rows, "
                            "so every `ir` list is empty (absent, not verified empty)")
        n = sum(len(v["out"]) + len(v["doubtful"]) + len(v["ir"]) for v in injuries.values())
        print(f"[ok] {n} absences across {len(injuries)} teams via {use}", file=sys.stderr)
    except SourceError as e:
        injuries, unmapped = {}, []
        inj_meta = {"name": use, "fetched_at": now, "error": str(e)}
        degraded.append(f"injury data unavailable ({e}); out/doubtful/ir are empty "
                        f"because nothing was fetched, not because nobody is hurt")
        print(f"[warn] {e}", file=sys.stderr)

    if not qbs and not injuries:
        sys.exit("[fatal] both sources failed — refusing to write a file of nulls "
                 "that would look like real data")

    # ---- assemble --------------------------------------------------------- #
    teams = build_teams(qbs, qb_snap, prior_qbs, injuries)
    missing_qb = [t for t, b in teams.items() if not b["qb"]]

    out_path = None if args.out == "-" else Path(args.out)
    old = {}
    if out_path and out_path.exists():
        try:
            old = json.loads(out_path.read_text()).get("teams") or {}
        except (ValueError, AttributeError):
            print(f"[note] {out_path} is unreadable; writing fresh", file=sys.stderr)

    teams, changed = carry_updated_at(old, teams, now)
    # A first write is a change; after that, only real movement counts.
    is_new = not old
    dirty = is_new or bool(changed)

    meta = {
        "fetched_at": now,
        "source": "nflverse depth_charts (QB) + " + (
            "ESPN injuries (OUT/DOUBTFUL/IR)" if use == "espn"
            else "nflverse injuries (OUT/DOUBTFUL, archived)"),
        "sources": {"qb": qb_meta, "injuries": inj_meta},
        "season": season,
        "week": week,
        "is_backfill": backfill,
        "team_count": len(teams),
        "teams_missing_qb": missing_qb,
        "unmapped_source_teams": unmapped,
        "degraded": degraded,
        "notes": NOTES,
    }
    obj = {"_meta": meta, "teams": teams}

    if missing_qb:
        print(f"[warn] no QB1 for {', '.join(missing_qb)} — written as null",
              file=sys.stderr)
    if unmapped:
        print(f"[warn] unmapped source team codes: {', '.join(unmapped)} — "
              f"those rows were DROPPED; add them to TEAM_ALIASES", file=sys.stderr)

    text = json.dumps(obj, indent=2)
    if args.dry_run or not out_path:
        print(text)
        return

    if not dirty and not args.force_write:
        print(f"[ok] no personnel change since {(old and 'the last write') or 'now'} "
              f"— {out_path} left untouched (--force-write to override)",
              file=sys.stderr)
        return

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text + "\n")
    print(f"[ok] wrote {out_path}", file=sys.stderr)
    # Stable last line: the workflow turns this into the commit subject, so the
    # git log reads as "who moved", not "the cron fired again".
    if is_new:
        print("[changed] all (first write)", file=sys.stderr)
    else:
        print(f"[changed] {', '.join(changed) or 'none (forced write)'}", file=sys.stderr)


if __name__ == "__main__":
    main()
