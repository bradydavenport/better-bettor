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
    python fetch_lines.py --sport nfl --out lines.json
    python fetch_lines.py --sport nfl --book pinnacle --raw        # dump untouched API JSON
    python fetch_lines.py --sport ncaaf --source theoddsapi --out cfb.json

Env:
    ODDS_API_KEY   The Odds API key (https://the-odds-api.com/ , free 500 req/mo)
    SGO_API_KEY    SportsGameOdds key (only needed for --source sportsgameodds)
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

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
# adapters
# --------------------------------------------------------------------------- #


class TheOddsAPIAdapter:
    """The Odds API v4 — https://the-odds-api.com/liveapi/guides/v4/

    Pinnacle sits in the 'eu' region. We pass `bookmakers=` directly, which the
    API allows in place of `regions=` and bills the same as a single region.
    """

    BASE = "https://api.the-odds-api.com/v4"

    def __init__(self, api_key, book="pinnacle"):
        if not api_key:
            sys.exit("ODDS_API_KEY not set (put it in .env or export it).")
        self.api_key = api_key
        self.book = book

    def _get(self, path, **params):
        params["apiKey"] = self.api_key
        r = requests.get(f"{self.BASE}{path}", params=params, timeout=20)
        # surface quota + a readable error before raise_for_status eats the body
        if not r.ok:
            sys.exit(f"The Odds API {r.status_code}: {r.text}")
        rem = r.headers.get("x-requests-remaining")
        used = r.headers.get("x-requests-used")
        last = r.headers.get("x-requests-last")
        if rem is not None:
            print(f"[quota] remaining={rem} used={used} this_call={last}",
                  file=sys.stderr)
        return r.json()

    def fetch_raw(self, sport):
        api_key = SPORT_KEYS[sport][0]
        return self._get(
            f"/sports/{api_key}/odds",
            bookmakers=self.book,
            regions="eu",  # Pinnacle lives here; ignored when `bookmakers` is set
            markets="h2h,spreads,totals",
            oddsFormat="american",
        )

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

    def fetch_raw(self, sport):
        league = SPORT_KEYS[sport][1]
        return self._get(
            "/events",
            leagueID=league,
            bookmakerID=self.book,
            oddsAvailable="true",
        )

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
    ap.add_argument("--out", default=None,
                    help="write normalized JSON here (default: stdout)")
    ap.add_argument("--raw", action="store_true",
                    help="dump the untouched API response instead of normalizing")
    args = ap.parse_args()

    cls, env_var, default_book = ADAPTERS[args.source]
    adapter = cls(os.getenv(env_var), book=args.book or default_book)

    raw = adapter.fetch_raw(args.sport)

    if args.raw:
        payload = raw
    else:
        payload = adapter.normalize(args.sport, raw)
        print(f"[ok] {len(payload)} games — {args.sport} @ {adapter.book}",
              file=sys.stderr)

    text = json.dumps(payload, indent=2)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
        print(f"[ok] wrote {args.out}", file=sys.stderr)
    else:
        print(text)


if __name__ == "__main__":
    main()
