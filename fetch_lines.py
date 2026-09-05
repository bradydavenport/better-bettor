"""
fetch_lines.py — pull betting lines from an odds source and normalize them.

Circa has no free real-time feed (no public web client; odds live only in the
native apps). This fetches from The Odds API instead, defaulting to Pinnacle as
the sharp reference book. The adapter layer is built so a paid Circa source
(SportsGameOdds) can be swapped in later without touching the output shape.

Normalized output (one dict per game), sign convention: positive spread = home favored.

    {
      "game_id":            "<source id>",
      "commence_time":      "2025-09-07T17:00:00Z",
      "home_team":          "Detroit Lions",
      "away_team":          "Green Bay Packers",
      "home_abbr":          "DET",
      "away_abbr":          "GB",
      "book":               "pinnacle",
      "spread":             -2.5,     # home line; positive => home favored
      "spread_price_home":  -110,
      "spread_price_away":  -110,
      "total":              48.5,
      "total_over_price":   -105,
      "total_under_price":  -115,
      "moneyline_home":     -140,
      "moneyline_away":     +120,
      "last_update":        "2025-09-05T12:00:00Z",   # book's timestamp, if given
      "fetched_at":         "2025-09-05T12:01:03Z"
    }

Usage:
    python fetch_lines.py --sport nfl --out lines.json            # next 8 days
    python fetch_lines.py --sport nfl --drop-empty --out lines.json  # only priced games
    python fetch_lines.py --sport nfl --days 0 --raw              # whole season, raw JSON
    python fetch_lines.py --status                               # show credit counter
    python fetch_lines.py --sport nfl --force                    # override daily limit

Quota: The Odds API free tier = 500 credits / calendar month. cost = markets x
regions, so our default pull is 3 credits. Going over is NOT billed — the API
just 401s until the 1st. A local limiter (see Usage class) caps spend at 15
credits/day = 450/month, with a 50-credit buffer reachable via --force.
Counters persist in .usage.json.

Env:
    ODDS_API_KEY       The Odds API key (https://the-odds-api.com/ , free tier)
    ODDS_DAILY_LIMIT   daily credit cap (default 15); --daily-limit overrides
    SGO_API_KEY        SportsGameOdds key (only for --source sportsgameodds)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

# --------------------------------------------------------------------------- #
# sport keys
# --------------------------------------------------------------------------- #

# maps our short name -> (The Odds API key, SportsGameOdds leagueID)
SPORT_KEYS = {
    "nfl":   ("americanfootball_nfl",   "NFL"),
    "ncaaf": ("americanfootball_ncaaf", "NCAAF"),
    "nba":   ("basketball_nba",         "NBA"),
    "ncaab": ("basketball_ncaab",       "NCAAB"),
    "mlb":   ("baseball_mlb",           "MLB"),
    "nhl":   ("icehockey_nhl",          "NHL"),
}

# NFL full name -> abbreviation (used only for convenience fields; unknown -> None)
NFL_ABBR = {
    "Arizona Cardinals": "ARI", "Atlanta Falcons": "ATL", "Baltimore Ravens": "BAL",
    "Buffalo Bills": "BUF", "Carolina Panthers": "CAR", "Chicago Bears": "CHI",
    "Cincinnati Bengals": "CIN", "Cleveland Browns": "CLE", "Dallas Cowboys": "DAL",
    "Denver Broncos": "DEN", "Detroit Lions": "DET", "Green Bay Packers": "GB",
    "Houston Texans": "HOU", "Indianapolis Colts": "IND", "Jacksonville Jaguars": "JAX",
    "Kansas City Chiefs": "KC", "Las Vegas Raiders": "LV", "Los Angeles Chargers": "LAC",
    "Los Angeles Rams": "LAR", "Miami Dolphins": "MIA", "Minnesota Vikings": "MIN",
    "New England Patriots": "NE", "New Orleans Saints": "NO", "New York Giants": "NYG",
    "New York Jets": "NYJ", "Philadelphia Eagles": "PHI", "Pittsburgh Steelers": "PIT",
    "San Francisco 49ers": "SF", "Seattle Seahawks": "SEA", "Tampa Bay Buccaneers": "TB",
    "Tennessee Titans": "TEN", "Washington Commanders": "WAS",
}


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _abbr(sport, team):
    return NFL_ABBR.get(team) if sport == "nfl" else None


# --------------------------------------------------------------------------- #
# usage limiter
# --------------------------------------------------------------------------- #
#
# The Odds API free tier is 500 *credits* per calendar month. A credit is not a
# call: cost = (# markets) x (# regions). Our default pull is 3 markets x 1
# region = 3 credits. Going over does NOT incur a charge — the API just returns
# 401/429 until the 1st of the next month. This limiter is only there to stop
# you burning the month early.
#
#   daily limit 15 credits  = 5 pulls/day = 450/month, leaving a 50-credit
#   buffer you can dip into with --force.

MONTHLY_CAP = 500
DEFAULT_DAILY_LIMIT = int(os.getenv("ODDS_DAILY_LIMIT", "15"))
USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".usage.json")


class Usage:
    """Local credit counter, persisted to .usage.json next to this script."""

    def __init__(self, path=USAGE_FILE):
        self.path = path
        self.data = {
            "day": "", "day_credits": 0,
            "month": "", "month_credits": 0, "month_force_credits": 0,
            "api_remaining": None, "api_used": None,
            "last_call": None, "last_cost": None,
        }
        try:
            with open(self.path) as f:
                self.data.update(json.load(f))
        except (FileNotFoundError, ValueError):
            pass
        self._rollover()

    def _rollover(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = today[:7]
        if self.data["day"] != today:
            self.data["day"], self.data["day_credits"] = today, 0
        if self.data["month"] != month:
            self.data["month"] = month
            self.data["month_credits"] = 0
            self.data["month_force_credits"] = 0

    def save(self):
        with open(self.path, "w") as f:
            json.dump(self.data, f, indent=2)

    def check(self, cost, daily_limit, force=False):
        d = self.data
        if d["month_credits"] + cost > MONTHLY_CAP:
            sys.exit(f"[blocked] monthly cap {MONTHLY_CAP} would be exceeded "
                     f"({d['month_credits']} used). Resets on the 1st — no charge.")
        if d["api_remaining"] is not None and d["api_remaining"] < cost:
            sys.exit(f"[blocked] API reports only {d['api_remaining']} credits left "
                     f"this month (as of last call).")
        if not force and d["day_credits"] + cost > daily_limit:
            sys.exit(
                f"[blocked] daily limit {daily_limit} would be exceeded "
                f"({d['day_credits']} used today, this call costs {cost}).\n"
                f"          --force to dip into the monthly buffer, "
                f"--status to see counters."
            )

    def record(self, cost, force=False, api_remaining=None, api_used=None):
        d = self.data
        d["day_credits"] += cost
        d["month_credits"] += cost
        if force:
            d["month_force_credits"] += cost
        if api_remaining is not None:
            d["api_remaining"] = api_remaining
            # the API's own tally is authoritative when we have it
            d["month_credits"] = max(d["month_credits"], MONTHLY_CAP - api_remaining)
        if api_used is not None:
            d["api_used"] = api_used
        d["last_call"] = _now_iso()
        d["last_cost"] = cost
        self.save()

    def render(self, daily_limit):
        d = self.data
        soft = daily_limit * 30
        buf = MONTHLY_CAP - soft
        day_left = daily_limit - d["day_credits"]
        out = [
            "Usage — The Odds API  (credits, not calls; default pull = 3 credits)",
            f"  Today  {d['day']}   {d['day_credits']:>3} / {daily_limit}"
            f"   ({day_left} left ≈ {max(day_left, 0) // 3} pulls)",
            f"  Month  {d['month']}      {d['month_credits']:>3} / {MONTHLY_CAP}"
            + (f"   ({d['api_remaining']} left, API-confirmed at last call)"
               if d["api_remaining"] is not None else "   (no API call yet)"),
            f"  Buffer over {soft} soft cap: {d['month_force_credits']} / {buf} "
            f"used via --force",
        ]
        if d["last_call"]:
            out.append(f"  Last call {d['last_call']}  (cost {d['last_cost']})")
        return "\n".join(out)


# --------------------------------------------------------------------------- #
# adapters
# --------------------------------------------------------------------------- #


class TheOddsAPIAdapter:
    """The Odds API v4 — https://the-odds-api.com/liveapi/guides/v4/

    Pinnacle sits in the 'eu' region. We pass `bookmakers=` directly, which the
    API allows in place of `regions=` and bills the same as a single region.
    """

    BASE = "https://api.the-odds-api.com/v4"
    MARKETS = "h2h,spreads,totals"

    def __init__(self, api_key, book="pinnacle"):
        if not api_key:
            sys.exit("ODDS_API_KEY not set (put it in .env or export it).")
        self.api_key = api_key
        self.book = book
        # cost = (# markets) x (# regions); we use one region
        self.estimated_cost = len(self.MARKETS.split(","))
        self.last_cost = None
        self.api_remaining = None
        self.api_used = None

    def _get(self, path, **params):
        params["apiKey"] = self.api_key
        r = requests.get(f"{self.BASE}{path}", params=params, timeout=20)
        # surface quota + a readable error before raise_for_status eats the body
        if not r.ok:
            sys.exit(f"The Odds API {r.status_code}: {r.text}")
        rem = r.headers.get("x-requests-remaining")
        used = r.headers.get("x-requests-used")
        last = r.headers.get("x-requests-last")
        self.api_remaining = int(float(rem)) if rem is not None else None
        self.api_used = int(float(used)) if used is not None else None
        self.last_cost = int(float(last)) if last is not None else None
        if rem is not None:
            print(f"[quota] remaining={rem} used={used} this_call={last}",
                  file=sys.stderr)
        return r.json()

    def fetch_raw(self, sport, days=0):
        api_key = SPORT_KEYS[sport][0]
        params = dict(
            bookmakers=self.book,
            regions="eu",  # Pinnacle lives here; ignored when `bookmakers` is set
            markets=self.MARKETS,
            oddsFormat="american",
        )
        if days:
            # server-side window: trims the payload and doesn't cost extra
            cutoff = datetime.now(timezone.utc) + timedelta(days=days)
            params["commenceTimeTo"] = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._get(f"/sports/{api_key}/odds", **params)

    def normalize(self, sport, raw):
        games = []
        for ev in raw:
            home = ev.get("home_team")
            away = ev.get("away_team")
            books = ev.get("bookmakers", [])
            book = next((b for b in books if b.get("key") == self.book), None)

            g = {
                "game_id": ev.get("id"),
                "commence_time": ev.get("commence_time"),
                "home_team": home,
                "away_team": away,
                "home_abbr": _abbr(sport, home),
                "away_abbr": _abbr(sport, away),
                "book": self.book,
                "spread": None,
                "spread_price_home": None,
                "spread_price_away": None,
                "total": None,
                "total_over_price": None,
                "total_under_price": None,
                "moneyline_home": None,
                "moneyline_away": None,
                "last_update": book.get("last_update") if book else None,
                "fetched_at": _now_iso(),
            }

            if book:
                for mkt in book.get("markets", []):
                    outs = {o.get("name"): o for o in mkt.get("outcomes", [])}
                    if mkt.get("key") == "spreads":
                        h, a = outs.get(home), outs.get(away)
                        if h and h.get("point") is not None:
                            # API point is the team's handicap; home favored -> negative.
                            # Our convention: positive spread == home favored.
                            g["spread"] = -float(h["point"])
                            g["spread_price_home"] = h.get("price")
                        if a:
                            g["spread_price_away"] = a.get("price")
                    elif mkt.get("key") == "totals":
                        over = outs.get("Over")
                        under = outs.get("Under")
                        if over and over.get("point") is not None:
                            g["total"] = float(over["point"])
                            g["total_over_price"] = over.get("price")
                        if under:
                            g["total_under_price"] = under.get("price")
                    elif mkt.get("key") == "h2h":
                        h, a = outs.get(home), outs.get(away)
                        if h:
                            g["moneyline_home"] = h.get("price")
                        if a:
                            g["moneyline_away"] = a.get("price")
            else:
                g["note"] = f"no {self.book} line for this game"

            games.append(g)
        return games


class SportsGameOddsAdapter:
    """SportsGameOdds v2 — https://sportsgameodds.com/docs/

    UNTESTED. Circa (`bookmakerID=circa`) requires a paid Pro plan. The parse
    below is written from the published docs, not a live response — run with
    --raw first and expect to adjust field names.
    """

    BASE = "https://api.sportsgameodds.com/v2"

    def __init__(self, api_key, book="circa"):
        if not api_key:
            sys.exit("SGO_API_KEY not set (put it in .env or export it).")
        self.api_key = api_key
        self.book = book
        # SGO isn't credit-metered the same way; treat a call as 1 unit locally
        self.estimated_cost = 1
        self.last_cost = 1
        self.api_remaining = None
        self.api_used = None

    def _get(self, path, **params):
        r = requests.get(
            f"{self.BASE}{path}",
            params=params,
            headers={"x-api-key": self.api_key},
            timeout=20,
        )
        if not r.ok:
            sys.exit(f"SportsGameOdds {r.status_code}: {r.text}")
        return r.json()

    def fetch_raw(self, sport, days=0):
        league = SPORT_KEYS[sport][1]
        params = dict(leagueID=league, bookmakerID=self.book, oddsAvailable="true")
        if days:
            cutoff = datetime.now(timezone.utc) + timedelta(days=days)
            params["startsBefore"] = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._get("/events", **params)

    def normalize(self, sport, raw):
        # Docs shape: {"data": [ { ...event..., "odds": { "<oddID>": {...} } } ]}
        events = raw.get("data", raw) if isinstance(raw, dict) else raw
        games = []
        for ev in events:
            teams = ev.get("teams", {})
            home = (teams.get("home") or {}).get("names", {}).get("long") \
                or ev.get("homeTeam")
            away = (teams.get("away") or {}).get("names", {}).get("long") \
                or ev.get("awayTeam")
            g = {
                "game_id": ev.get("eventID") or ev.get("id"),
                "commence_time": ev.get("status", {}).get("startsAt")
                or ev.get("startTime"),
                "home_team": home,
                "away_team": away,
                "home_abbr": _abbr(sport, home),
                "away_abbr": _abbr(sport, away),
                "book": self.book,
                "spread": None,
                "spread_price_home": None,
                "spread_price_away": None,
                "total": None,
                "total_over_price": None,
                "total_under_price": None,
                "moneyline_home": None,
                "moneyline_away": None,
                "last_update": None,
                "fetched_at": _now_iso(),
                "note": "sportsgameodds adapter is untested — verify against --raw",
            }
            # Left deliberately shallow: the odds object keys vary by plan and
            # market config. Fill this in once you can see a real --raw payload.
            games.append(g)
        return games


ADAPTERS = {
    "theoddsapi": (TheOddsAPIAdapter, "ODDS_API_KEY", "pinnacle"),
    "sportsgameodds": (SportsGameOddsAdapter, "SGO_API_KEY", "circa"),
}


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sport", default="nfl", choices=sorted(SPORT_KEYS),
                    help="league (default: nfl)")
    ap.add_argument("--source", default="theoddsapi", choices=sorted(ADAPTERS),
                    help="odds source (default: theoddsapi)")
    ap.add_argument("--book", default=None,
                    help="bookmaker key override (default: source's default)")
    ap.add_argument("--days", type=int, default=8,
                    help="only games starting within N days (0 = no limit; "
                         "default: 8, i.e. the upcoming slate)")
    ap.add_argument("--drop-empty", action="store_true",
                    help="omit games that have no spread from this book yet")
    ap.add_argument("--daily-limit", type=int, default=DEFAULT_DAILY_LIMIT,
                    help=f"max credits to spend per day (default: {DEFAULT_DAILY_LIMIT}"
                         f", or $ODDS_DAILY_LIMIT). 1 pull = 3 credits.")
    ap.add_argument("--force", action="store_true",
                    help="ignore the daily limit for this run (dips into the "
                         "monthly buffer; still hard-stops at the 500 cap)")
    ap.add_argument("--status", action="store_true",
                    help="print the usage counter and exit (no API call)")
    ap.add_argument("--out", default=None,
                    help="write normalized JSON here (default: stdout)")
    ap.add_argument("--no-render", action="store_true",
                    help="skip writing the .csv / .html alongside --out")
    ap.add_argument("--raw", action="store_true",
                    help="dump the untouched API response instead of normalizing")
    args = ap.parse_args()

    usage = Usage()

    if args.status:
        print(usage.render(args.daily_limit))
        return

    cls, env_var, default_book = ADAPTERS[args.source]
    adapter = cls(os.getenv(env_var), book=args.book or default_book)

    metered = args.source == "theoddsapi"
    if metered:
        usage.check(adapter.estimated_cost, args.daily_limit, force=args.force)

    raw = adapter.fetch_raw(args.sport, days=args.days)

    if metered:
        usage.record(adapter.last_cost or adapter.estimated_cost,
                     force=args.force,
                     api_remaining=adapter.api_remaining,
                     api_used=adapter.api_used)
        d = usage.data
        print(f"[usage] today {d['day_credits']}/{args.daily_limit}  "
              f"month {d['month_credits']}/{MONTHLY_CAP}  "
              f"({d['api_remaining']} left)  —  --status for detail",
              file=sys.stderr)

    if args.raw:
        payload = raw
    else:
        payload = adapter.normalize(args.sport, raw)
        if args.drop_empty:
            payload = [g for g in payload if g.get("spread") is not None]
        payload.sort(key=lambda g: g.get("commence_time") or "")
        print(f"[ok] {len(payload)} games — {args.sport} @ {adapter.book}"
              + (f" (next {args.days}d)" if args.days else ""),
              file=sys.stderr)

    text = json.dumps(payload, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
        print(f"[ok] wrote {args.out}", file=sys.stderr)
        if not args.raw and not args.no_render:
            import render
            from pathlib import Path
            p = Path(args.out)
            csv_path, html_path = render.render(payload, stem=p.stem, outdir=p.parent)
            print(f"[ok] wrote {csv_path}  +  {html_path}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
