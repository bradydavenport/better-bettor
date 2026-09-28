"""
fetch_lines.py — pull betting lines from an odds source and normalize them.

Circa has no free real-time feed (no public web client; odds live only in the
native apps). This fetches from The Odds API instead, defaulting to Pinnacle as
the sharp reference book. The adapter layer is built so a paid Circa source
(SportsGameOdds) can be swapped in later without touching the output shape.

Output is a self-describing object: { "_meta": {...}, "games": [ {...}, ... ] }.
_meta carries the spread convention, field glossary and the live credit usage,
so a consumer only needs the file — no side instructions. --raw skips the
wrapper and dumps the untouched upstream response.

    {
      "_meta": {
        "source": "theoddsapi", "book": "pinnacle", "sport": "nfl",
        "pulled_markets": ["h2h", "spreads"],       # what THIS run fetched
        "markets_present": ["h2h", "spreads", "totals"],  # what has a line in the file
        "fetched_at": "2026-09-07T17:00:00Z", "game_count": 16,
        "spread_convention": "`spread` is the HOME line; POSITIVE = home favored ...",
        "usage": { "credits_used": 138, "credits_remaining": 362, "monthly_cap": 500, ... }
      },
      "games": [
        {
          "game_id": "<source id>",
          "commence_time": "2026-09-07T17:00:00Z",
          "home_team": "Detroit Lions", "away_team": "Green Bay Packers",
          "home_abbr": "DET", "away_abbr": "GB", "book": "pinnacle",
          "moneyline_home": -140, "moneyline_away": 120,
          "moneyline_at": "2026-09-07T13:00:04Z",
          "spread": -2.5, "spread_price_home": -110, "spread_price_away": -110,
          "spread_at": "2026-09-07T13:00:04Z",
          "total": 48.5, "total_over_price": -105, "total_under_price": -115,
          "total_at": "2026-09-06T22:11:40Z",     # older — pulled in a separate run
          "book_last_update": "2026-09-07T12:58:00Z"
        }
      ]
    }

Each market carries its own *_at stamp (when it was last pulled). With --merge,
a run that pulls only some markets leaves the others — value and stamp —
untouched, so one file accumulates all three at their own refresh cadences.

When --out is a path, a sibling usage.json (just the _meta.usage block) is
written next to it as a standalone, always-current credit counter.

Usage:
    python fetch_lines.py --sport nfl --out lines.json               # all 3 markets, 3 credits
    python fetch_lines.py --sport nfl --markets spread --out s.json   # spread only, 1 credit
    python fetch_lines.py --sport nfl --markets total --merge --out lines.json  # patch totals in
    python fetch_lines.py --sport nfl --drop-empty --out lines.json   # only priced games
    python fetch_lines.py --sport nfl --days 0 --raw                  # whole season, raw JSON
    python fetch_lines.py --status                                    # show credit counter

Markets: --markets takes ml / spread / total (aliases: moneyline, h2h, spreads,
ats, totals, ou), comma-separated, or `all`. Cost is 1 credit per market x 1
region, so fewer markets = a cheaper pull.

Quota: The Odds API free tier = 500 credits / calendar month. Going over is NOT
billed — the API just 401s until the 1st. A local limiter (see Usage class)
caps spend at 15 credits/day, with a buffer reachable via --force. Counters
persist in .usage.json; the API's own tally comes back in response headers.

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
from pathlib import Path

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

# The Odds API bills 1 credit per market per region. We use one region, so the
# per-pull cost == number of markets requested. Friendly names -> API keys:
CANON_MARKETS = ("h2h", "spreads", "totals")
MARKET_ALIASES = {
    "ml": "h2h", "moneyline": "h2h", "money": "h2h", "h2h": "h2h",
    "spread": "spreads", "spreads": "spreads", "ats": "spreads",
    "total": "totals", "totals": "totals", "ou": "totals", "o/u": "totals",
    "all": "h2h,spreads,totals",
}


def parse_markets(spec):
    """'spread,ml' -> 'h2h,spreads' (canonical order, deduped). 'all' -> all 3."""
    picked = []
    for tok in spec.lower().replace(" ", "").split(","):
        if not tok:
            continue
        mapped = MARKET_ALIASES.get(tok)
        if mapped is None:
            sys.exit(f"unknown market '{tok}' — use ml, spread, total (comma-sep) or all")
        for k in mapped.split(","):
            if k not in picked:
                picked.append(k)
    if not picked:
        sys.exit("--markets resolved to nothing")
    return ",".join(m for m in CANON_MARKETS if m in picked)


# market -> (value/price fields ..., timestamp field). The first entry is the
# "does this market have a line" field.
MARKET_FIELDS = {
    "h2h":     ("moneyline_home", "moneyline_away", "moneyline_at"),
    "spreads": ("spread", "spread_price_home", "spread_price_away", "spread_at"),
    "totals":  ("total", "total_over_price", "total_under_price", "total_at"),
}


def _has_any_market(g):
    """True if the game carries a line for at least one market."""
    return any(g.get(fields[0]) is not None for fields in MARKET_FIELDS.values())


def markets_present(games):
    """Canonical-ordered list of markets that have a line somewhere in `games`."""
    return [m for m in CANON_MARKETS
            if any(g.get(MARKET_FIELDS[m][0]) is not None for g in games)]


def merge_forward(old_games, new_games, pulled):
    """Carry each market NOT in `pulled` (fields + its *_at stamp) from the
    matching old game (by game_id) into the new games. New pull wins for the
    markets it covers; untouched markets keep their previous value and stamp."""
    old_by_id = {g.get("game_id"): g for g in old_games}
    carry = [m for m in CANON_MARKETS if m not in pulled]
    for g in new_games:
        prev = old_by_id.get(g.get("game_id"))
        if not prev:
            continue
        for m in carry:
            for f in MARKET_FIELDS[m]:
                if prev.get(f) is not None:
                    g[f] = prev[f]
    return new_games

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


def set_output(key, value):
    """Tell the workflow what happened. Silent outside GitHub Actions.

    The commit step is gated on `pulled`, so a run that made no API call cannot
    reach `git add` at all — belt and braces alongside writing nothing.
    """
    path = os.getenv("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a") as f:
            f.write(f"{key}={value}\n")
    except OSError:
        pass


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

# THE committed ledger. This used to be a gitignored .usage.json next to the
# script, which meant every CI run started from zero counters and check() could
# never fire there — the guard was decorative in the only place it mattered.
# One tracked file, loaded and saved by CI and locally alike, fixes that.
#
# It holds the public snapshot at the top level (unchanged shape, so anything
# fetching data/usage.json by raw URL keeps working) plus the private running
# counters under "_counters".
#
# One ledger tracks ONE API key. If CI's ODDS_API_KEY secret differs from the
# local .env key, the numbers will fight each other — the api_* fields come from
# whichever key called last.
USAGE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "data", "usage.json")


class BudgetStop(RuntimeError):
    """The limiter refused this call.

    Not a failure: it is the guard doing its job. main() turns it into a CI
    annotation and a clean exit, so a cron that has hit its budget does not paint
    itself red every run — and, critically, returns before anything is written.
    """


class Usage:
    """Credit counter, persisted to the committed data/usage.json."""

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
                stored = json.load(f)
        except (FileNotFoundError, ValueError):
            stored = None
        if stored:
            self.data.update(self._counters_from(stored))
        self._rollover()

    @staticmethod
    def _counters_from(stored):
        """Recover counters from a ledger file.

        Prefers the "_counters" block. Falls back to deriving them from the
        public snapshot fields, so the first run on this code picks up the real
        numbers already committed instead of restarting at zero.
        """
        if isinstance(stored.get("_counters"), dict):
            return stored["_counters"]
        return {
            "day": stored.get("today") or "",
            "day_credits": stored.get("today_credits") or 0,
            "month": stored.get("month") or "",
            "month_credits": stored.get("credits_used") or 0,
            "month_force_credits": stored.get("buffer_used") or 0,
            "api_remaining": stored.get("credits_remaining"),
            "api_used": stored.get("credits_used"),
            "last_call": stored.get("last_call"),
            "last_cost": stored.get("last_cost"),
        }

    def _rollover(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        month = today[:7]
        if self.data["day"] != today:
            self.data["day"], self.data["day_credits"] = today, 0
        if self.data["month"] != month:
            self.data["month"] = month
            self.data["month_credits"] = 0
            self.data["month_force_credits"] = 0

    def save(self, daily_limit=DEFAULT_DAILY_LIMIT):
        """Write the one ledger: public snapshot + private counters."""
        payload = self.snapshot(daily_limit)
        payload["_counters"] = self.data
        d = os.path.dirname(os.path.abspath(self.path))
        if d:
            os.makedirs(d, exist_ok=True)
        with open(self.path, "w") as f:
            json.dump(payload, f, indent=2)
        return payload

    def check(self, cost, daily_limit, force=False):
        d = self.data
        if d["month_credits"] + cost > MONTHLY_CAP:
            raise BudgetStop(
                f"monthly cap {MONTHLY_CAP} would be exceeded "
                f"({d['month_credits']} used). Resets on the 1st — no charge.")
        if d["api_remaining"] is not None and d["api_remaining"] < cost:
            raise BudgetStop(
                f"API reports only {d['api_remaining']} credits left this month "
                f"(as of last call). --force does NOT override this; it is the "
                f"API's own number, not our estimate.")
        if not force and d["day_credits"] + cost > daily_limit:
            raise BudgetStop(
                f"daily limit {daily_limit} would be exceeded "
                f"({d['day_credits']} used today, this call costs {cost}). "
                f"--force to dip into the monthly buffer, --status to see counters."
            )

    def record(self, cost, force=False, api_remaining=None, api_used=None,
               daily_limit=DEFAULT_DAILY_LIMIT):
        d = self.data
        d["day_credits"] += cost
        d["month_credits"] += cost
        if force:
            d["month_force_credits"] += cost
        if api_remaining is not None:
            d["api_remaining"] = api_remaining
            # The API's own tally IS the truth, not a tie-break. `max()` used to
            # be used here, which meant a local counter that had drifted high
            # could never come back down — including after a quota window reset.
            d["month_credits"] = MONTHLY_CAP - api_remaining
        if api_used is not None:
            d["api_used"] = api_used
        d["last_call"] = _now_iso()
        d["last_cost"] = cost
        self.save(daily_limit)

    def snapshot(self, daily_limit):
        """Public, durable view of the counter — written to data/usage.json and
        embedded in each lines file's _meta. Numbers come from the API response
        headers, so they count every call on the account (local runs + CI)."""
        d = self.data
        used = d["api_used"] if d["api_used"] is not None else d["month_credits"]
        remaining = (d["api_remaining"] if d["api_remaining"] is not None
                     else max(MONTHLY_CAP - used, 0))
        return {
            "month": d["month"],
            "credits_used": used,
            "credits_remaining": remaining,
            "monthly_cap": MONTHLY_CAP,
            "pct_used": round(100 * used / MONTHLY_CAP, 1) if MONTHLY_CAP else None,
            "pulls_left_est": remaining // 3,
            "today": d["day"],
            "today_credits": d["day_credits"],
            "daily_limit": daily_limit,
            "buffer_used": d["month_force_credits"],
            "last_call": d["last_call"],
            "last_cost": d["last_cost"],
            "updated_at": _now_iso(),
            "note": ("The Odds API free tier = 500 credits per calendar month, "
                     "resets on the 1st. Exceeding it returns HTTP 401 until the "
                     "reset — never a charge. Cost = 1 credit per market per pull "
                     "(all 3 markets = 3, --markets spread = 1)."),
        }

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
# book roster
# --------------------------------------------------------------------------- #
#
# Pinnacle is the BENCHMARK book: it alone drives data/nfl.json and everything
# downstream of it. The rest are logged to data/history.jsonl only, for closing-
# line value against the books Brady actually bets.
#
# Billing, quoted from the v4 docs: "Every group of 10 bookmakers is the
# equivalent of 1 region." cost = markets x regions, so 10 books cost exactly
# what 1 book costs. Verified against a real response: 10 books x markets=h2h
# returned `x-requests-last: 1`. Adding the 9 extra books was free.
#
# Prediction-market exchanges rather than sportsbooks. Their quotes are peer-to-peer
# and the price almost certainly excludes the platform's fee, so it is not directly
# comparable to a book's vig-inclusive line. Rows from these carry `exchange: true`
# as a FLAG ONLY — nothing here adjusts a price, because the correct adjustment
# depends on the fee schedule and we do not have it.
EXCHANGE_BOOKS = frozenset({"kalshi", "novig"})

# KEEP THIS LIST AT 10 OR FEWER. An 11th book silently doubles every pull.
BENCHMARK_BOOK = "pinnacle"
EXTRA_BOOKS = (
    "draftkings", "fanduel", "betmgm", "betrivers", "hardrockbet",
    "espnbet",      # theScore Bet / ESPN Bet — one key, the docs list both names
    "ballybet", "novig", "kalshi",
)
# Deliberately absent: Caesars (`williamhill_us`) and Fanatics (`fanatics`) are
# both flagged "Only available on paid subscriptions" in the docs. Requesting
# williamhill_us on the free tier is accepted but returns 0 of 16 games — a
# silent empty, not an error. Do not re-add them without a paid plan.
LOGGED_BOOKS = (BENCHMARK_BOOK,) + EXTRA_BOOKS
assert len(LOGGED_BOOKS) <= 10, "over 10 books doubles the credit cost per pull"


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

    def __init__(self, api_key, book="pinnacle", markets=None, books=None):
        if not api_key:
            sys.exit("ODDS_API_KEY not set (put it in .env or export it).")
        self.api_key = api_key
        # `book` is the benchmark: the only one normalize() reads, so the only
        # one that reaches data/nfl.json. `books` is the full roster we request
        # and log. The benchmark is always in the roster.
        self.book = book
        roster = list(books) if books else [book]
        if book not in roster:
            roster.insert(0, book)
        self.books = roster
        self.markets = markets or self.MARKETS
        # cost = (# markets) x (# regions), and <=10 books == 1 region
        self.estimated_cost = len(self.markets.split(",")) * ((len(roster) + 9) // 10)
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
            bookmakers=",".join(self.books),
            regions="eu",  # Pinnacle lives here; ignored when `bookmakers` is set
            markets=self.markets,
            oddsFormat="american",
        )
        if days:
            # server-side window: trims the payload and doesn't cost extra
            cutoff = datetime.now(timezone.utc) + timedelta(days=days)
            params["commenceTimeTo"] = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")
        return self._get(f"/sports/{api_key}/odds", **params)

    def normalize(self, sport, raw):
        now = _now_iso()
        pulled = self.markets.split(",")
        games = []
        for ev in raw:
            home = ev.get("home_team")
            away = ev.get("away_team")
            # The response now carries up to 10 books. This picks the benchmark
            # by key and ignores the rest, so nfl.json and everything downstream
            # of it are unchanged by the wider pull. Keys are unique per event,
            # so the match is exact, not "whichever came first".
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
                "moneyline_home": None, "moneyline_away": None, "moneyline_at": None,
                "spread": None, "spread_price_home": None, "spread_price_away": None,
                "spread_at": None,
                "total": None, "total_over_price": None, "total_under_price": None,
                "total_at": None,
                "book_last_update": book.get("last_update") if book else None,
            }
            # stamp every market we asked for on this pull, line or not — so the
            # file records "checked totals at T, none posted" vs "never checked"
            for m in pulled:
                g[MARKET_FIELDS[m][-1]] = now

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

    def __init__(self, api_key, book="circa", markets=None):
        if not api_key:
            sys.exit("SGO_API_KEY not set (put it in .env or export it).")
        self.api_key = api_key
        self.book = book
        self.markets = markets or "h2h,spreads,totals"  # kept for parity; not billed per-market
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
                "moneyline_home": None, "moneyline_away": None, "moneyline_at": None,
                "spread": None, "spread_price_home": None, "spread_price_away": None,
                "spread_at": None,
                "total": None, "total_over_price": None, "total_under_price": None,
                "total_at": None,
                "book_last_update": None,
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
# append-only history log
# --------------------------------------------------------------------------- #
#
# data/history.jsonl is the ledger: one JSON object per line, per book x game x
# market x outcome, written from the RAW response before normalize() or
# merge_forward() touch anything. Every pull appends; nothing is ever rewritten,
# deduped or sorted in place. Repeat rows with no movement are data — they prove
# the line held. Downstream readers dedupe on (book, game_id, market, outcome,
# last_update) if they want distinct quotes.
#
# This is an all-books record. nfl.json stays Pinnacle-only; see LOGGED_BOOKS.

HISTORY_FIELDS = ("fetched_at", "last_update", "game_id", "commence_time",
                  "home", "away", "book", "market", "outcome", "price",
                  "point", "source")
# Present only when true, so absence means "an ordinary sportsbook".
HISTORY_OPTIONAL_FIELDS = ("exchange",)


def history_rows(raw, fetched_at, source="live"):
    """Flatten a raw The Odds API odds response into ledger rows.

    Timestamps: `last_update` is the MARKET-level stamp, not the bookmaker-level
    one — the v4 docs deprecate the latter. Falls back to the bookmaker stamp
    when a market omits it, then to null.

    `point` is the API's own value, untouched: for spreads that is the outcome
    team's handicap (negative == favored), which is the OPPOSITE sign convention
    from nfl.json's `spread` field. The ledger keeps the API convention so it can
    be compared against other sources without unwinding our normalization.
    """
    rows = []
    for ev in raw or []:
        game_id = ev.get("id")
        commence = ev.get("commence_time")
        home, away = ev.get("home_team"), ev.get("away_team")
        for bk in ev.get("bookmakers") or []:
            book_stamp = bk.get("last_update")
            for mkt in bk.get("markets") or []:
                for out in mkt.get("outcomes") or []:
                    price = out.get("price")
                    row = {
                        "fetched_at": fetched_at,
                        "last_update": mkt.get("last_update") or book_stamp,
                        "game_id": game_id,
                        "commence_time": commence,
                        "home": home,
                        "away": away,
                        "book": bk.get("key"),
                        "market": mkt.get("key"),
                        "outcome": out.get("name"),
                        "price": int(price) if price is not None else None,
                        "point": out.get("point"),
                        "source": source,
                    }
                    if bk.get("key") in EXCHANGE_BOOKS:
                        row["exchange"] = True
                    rows.append(row)
    return rows


def append_history(path, rows):
    """Append rows to the ledger. Returns the count written.

    Append mode only — never read-modify-write, so a crash mid-write can cost at
    most one partial line rather than the whole ledger.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return len(rows)


def log_history_safely(path, raw, fetched_at):
    """Append to the ledger, loudly, and never let it take the run down.

    The ledger is valuable but nfl.json is the product. Any failure here is
    reported and swallowed so the pull still writes its file — we already paid
    the credits for this response.
    """
    try:
        rows = history_rows(raw, fetched_at)
        n = append_history(path, rows)
        books = sorted({r["book"] for r in rows})
        print(f"[ok] history += {n} rows -> {path}  ({len(books)} books: "
              f"{', '.join(books)})", file=sys.stderr)
        return n
    except Exception as e:                       # noqa: BLE001 - deliberate
        print(f"[warn] history append FAILED ({type(e).__name__}: {e}) — "
              f"continuing; nfl.json is unaffected and this pull's rows are lost",
              file=sys.stderr)
        return 0


# --------------------------------------------------------------------------- #
# coverage alarm
# --------------------------------------------------------------------------- #
#
# A book can vanish from the response without any error: request a key the plan
# does not cover and you get HTTP 200 with that book simply absent. That is not
# hypothetical — williamhill_us (Caesars) returns 0 of 16 games on the free tier.
# A silent zero looks exactly like "no lines posted yet", so it needs an alarm.
#
# The threshold cannot be a flat percentage. Real coverage on one pull ranged from
# pinnacle at 75% to five books at 100%, so a fixed ">80% is normal" bar would cry
# wolf about Pinnacle on every run. Instead each book is compared against ITS OWN
# recent history, and the degraded-coverage alarm only fires for books that
# normally clear 80%.

COVERAGE_BASELINE_PULLS = 20
COVERAGE_FLOOR = 0.50
COVERAGE_USUALLY = 0.80
_TAIL_BYTES = 8 * 1024 * 1024


def _tail_rows(path, max_bytes=_TAIL_BYTES):
    """Parse the last chunk of a JSONL ledger. Bounded, so it stays cheap as the
    file grows across a season."""
    p = Path(path)
    if not p.exists():
        return []
    size = p.stat().st_size
    with open(p, "rb") as f:
        if size > max_bytes:
            f.seek(size - max_bytes)
            f.readline()            # discard the partial first line
        raw = f.read().decode("utf-8", "replace")
    rows = []
    for line in raw.splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def coverage_baselines(history_path, pulls=COVERAGE_BASELINE_PULLS):
    """{book: median coverage fraction} over the most recent `pulls` pulls."""
    rows = [r for r in _tail_rows(history_path) if r.get("source") == "live"]
    by_pull = {}
    for r in rows:
        by_pull.setdefault(r.get("fetched_at"), []).append(r)
    recent = [by_pull[k] for k in sorted(by_pull)[-pulls:]]
    per_book = {}
    for batch in recent:
        games = {r.get("game_id") for r in batch}
        if not games:
            continue
        for book in {r.get("book") for r in batch}:
            covered = len({r.get("game_id") for r in batch if r.get("book") == book})
            per_book.setdefault(book, []).append(covered / len(games))
    out = {}
    for book, vals in per_book.items():
        vals.sort()
        mid = len(vals) // 2
        out[book] = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    return out, len(recent)


def coverage_alarm(raw, roster, history_path):
    """Annotate CI when a book goes missing or drops well below its own norm.

    Never raises: a broken alarm must not cost a pull we already paid for.
    """
    try:
        games = {ev.get("id") for ev in raw or []}
        if not games:
            print("::warning::coverage: the response contained no games at all")
            return
        seen = {}
        for ev in raw or []:
            for bk in ev.get("bookmakers") or []:
                seen.setdefault(bk.get("key"), set()).add(ev.get("id"))
        baselines, n_pulls = coverage_baselines(history_path)

        lines = []
        for book in roster:
            covered = len(seen.get(book, ()))
            frac = covered / len(games)
            base = baselines.get(book)
            note = ""
            if covered == 0:
                # a book returning nothing at all is an outage or a dead key
                print(f"::error::coverage: {book} returned 0 of {len(games)} games. "
                      f"Either the book posted nothing, the key is wrong, or the "
                      f"plan does not cover it (the API returns 200 with the book "
                      f"simply absent).")
                note = "  <-- ZERO"
            elif frac < COVERAGE_FLOOR and base is not None and base > COVERAGE_USUALLY:
                print(f"::warning::coverage: {book} covered {covered}/{len(games)} "
                      f"games ({frac:.0%}) but normally covers {base:.0%} over the "
                      f"last {n_pulls} pulls.")
                note = f"  <-- low (baseline {base:.0%})"
            lines.append(f"     {book:14s} {covered:2d}/{len(games)}  {frac:5.1%}"
                         + (f"  baseline {base:.0%}" if base is not None else
                            "  baseline n/a")
                         + note)
        print(f"[ok] book coverage ({len(games)} games, baselines from "
              f"{n_pulls} prior pull(s)):", file=sys.stderr)
        for line in lines:
            print(line, file=sys.stderr)
    except Exception as e:                      # noqa: BLE001 - deliberate
        print(f"[warn] coverage alarm failed ({type(e).__name__}: {e}) — "
              f"the pull itself is unaffected", file=sys.stderr)


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #


def self_test_limiter():
    """Prove check() actually blocks. No API calls, no credits.

    This exists because the guard was silently inert in CI for the whole of its
    life: Usage read a gitignored file, so every workflow run began at zero
    credits and check() waved everything through. A regression to that state
    looks like nothing at all from the outside, so it needs a test that fails
    loudly. Run: python fetch_lines.py --self-test-limiter
    """
    import tempfile

    results = []

    def case(name, ledger, cost, limit, force, expect_block):
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "usage.json")
            with open(path, "w") as f:
                json.dump(ledger, f)
            u = Usage(path)
            # freeze the rollover: the fixture's day/month must survive so the
            # counters under test are the ones check() actually sees
            u.data["day"] = u.data["month"] = None
            u.data.update(ledger.get("_counters", {}))
            blocked = False
            wrote_before = os.path.getmtime(path)
            try:
                u.check(cost, limit, force=force)
            except BudgetStop:
                blocked = True
            # a refused call must not have touched the ledger on its way out
            assert os.path.getmtime(path) == wrote_before, \
                "check() wrote to the ledger while refusing a call"
            ok = blocked == expect_block
            results.append((ok, name,
                            f"expected {'BLOCK' if expect_block else 'PASS'}, "
                            f"got {'BLOCK' if blocked else 'PASS'}"))

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def ledger(**kw):
        c = {"day": today, "day_credits": 0, "month": month, "month_credits": 0,
             "month_force_credits": 0, "api_remaining": None, "api_used": None,
             "last_call": None, "last_cost": None}
        c.update(kw)
        return {"_counters": c}

    # the regression that started all this: a fresh, counter-less ledger
    case("empty ledger allows a normal pull", {}, 2, 15, False, False)
    case("healthy ledger allows a normal pull",
         ledger(month_credits=60, api_remaining=440), 2, 15, False, False)

    # the monthly cap is a hard wall, force or not
    case("monthly cap blocks",
         ledger(month_credits=MONTHLY_CAP - 1), 2, 15, False, True)
    case("monthly cap blocks even with --force",
         ledger(month_credits=MONTHLY_CAP - 1), 2, 15, True, True)

    # the API's own number outranks our estimate
    case("api_remaining below cost blocks",
         ledger(month_credits=0, api_remaining=1), 2, 15, False, True)
    case("api_remaining blocks even with --force",
         ledger(month_credits=0, api_remaining=1), 2, 15, True, True)
    case("api_remaining exactly covering cost passes",
         ledger(month_credits=0, api_remaining=2), 2, 15, False, False)

    # the daily limit is the soft one --force is meant to bypass
    case("daily limit blocks",
         ledger(day_credits=15, api_remaining=400), 2, 15, False, True)
    case("daily limit yields to --force",
         ledger(day_credits=15, api_remaining=400), 2, 15, True, False)

    # a CI-shaped ledger: counters recovered from the PUBLIC fields only
    case("public-only ledger is still read (no _counters block)",
         {"month": month, "credits_used": 499, "credits_remaining": 1,
          "today": today, "today_credits": 0, "buffer_used": 0},
         2, 15, False, True)

    width = max(len(n) for _, n, _ in results)
    for ok, name, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:<{width}}  ({detail})")
    failed = [n for ok, n, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    if failed:
        sys.exit(f"[FAIL] limiter guard is not firing: {', '.join(failed)}")
    print("[ok] limiter blocks on monthly cap, api_remaining and daily limit")


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
    ap.add_argument("--markets", default="all",
                    help="markets to pull: any of ml, spread, total (comma-sep) "
                         "or all (default). Cost = 1 credit per market, so "
                         "`--markets spread` is a 1-credit pull.")
    ap.add_argument("--drop-empty", action="store_true",
                    help="omit games with no line for any market (after any --merge)")
    ap.add_argument("--merge", action="store_true",
                    help="merge into an existing --out file: markets not pulled "
                         "this run keep their previous value and *_at timestamp")
    ap.add_argument("--daily-limit", type=int, default=DEFAULT_DAILY_LIMIT,
                    help=f"max credits to spend per day (default: {DEFAULT_DAILY_LIMIT}"
                         f", or $ODDS_DAILY_LIMIT). 1 credit per market per pull.")
    ap.add_argument("--force", action="store_true",
                    help="ignore the daily limit for this run (dips into the "
                         "monthly buffer; still hard-stops at the 500 cap)")
    ap.add_argument("--status", action="store_true",
                    help="print the usage counter and exit (no API call)")
    ap.add_argument("--self-test-limiter", action="store_true",
                    help="prove check() still blocks when the counters say we "
                         "are over budget, then exit (no API call, no credits)")
    ap.add_argument("--out", default=None,
                    help="write normalized JSON here (default: stdout)")
    ap.add_argument("--no-render", action="store_true",
                    help="skip writing the .csv / .html alongside --out")
    ap.add_argument("--raw", action="store_true",
                    help="dump the untouched API response instead of normalizing")
    ap.add_argument("--no-history", action="store_true",
                    help="skip the append to the history ledger (see --history)")
    ap.add_argument("--history", default=None,
                    help="ledger path (default: history.jsonl beside --out, or "
                         "data/history.jsonl when writing to stdout)")
    ap.add_argument("--benchmark-book", default=None,
                    help=f"the one book that drives --out and everything "
                         f"downstream (default: {BENCHMARK_BOOK}). The other "
                         f"books in LOGGED_BOOKS are logged only.")
    args = ap.parse_args()

    if args.self_test_limiter:
        set_output("pulled", "false")
        self_test_limiter()
        return

    usage = Usage()

    if args.status:
        set_output("pulled", "false")
        print(usage.render(args.daily_limit))
        return

    markets = parse_markets(args.markets)

    cls, env_var, default_book = ADAPTERS[args.source]
    # The Odds API pull requests the whole book roster for the ledger; every
    # other source keeps its single-book behaviour.
    roster = LOGGED_BOOKS if args.source == "theoddsapi" else None
    benchmark = args.benchmark_book or args.book or default_book
    adapter = cls(os.getenv(env_var), book=benchmark, markets=markets,
                  books=roster)

    metered = args.source == "theoddsapi"
    if metered:
        try:
            usage.check(adapter.estimated_cost, args.daily_limit, force=args.force)
        except BudgetStop as e:
            # No API call, so: nothing written, nothing to commit, and a clean
            # exit rather than a red run for a condition the guard is built to
            # produce. Visible in CI as an annotation.
            print(f"::warning::pull skipped — {e}")
            print(f"[skip] {e}", file=sys.stderr)
            print("[skip] no API call made; no file written, nothing to commit",
                  file=sys.stderr)
            set_output("pulled", "false")
            return

    raw = adapter.fetch_raw(args.sport, days=args.days)

    # Ledger first: straight off the raw response, before normalize() collapses
    # it to one book and before merge_forward() carries anything over. Wrapped so
    # it can never cost us the pull we just paid for.
    if not args.no_history and args.source == "theoddsapi":
        if args.history:
            hist_path = Path(args.history)
        elif args.out:
            hist_path = Path(args.out).parent / "history.jsonl"
        else:
            hist_path = Path("data/history.jsonl")
        log_history_safely(hist_path, raw, _now_iso())
        coverage_alarm(raw, adapter.books, hist_path)

    if metered:
        usage.record(adapter.last_cost or adapter.estimated_cost,
                     force=args.force,
                     api_remaining=adapter.api_remaining,
                     api_used=adapter.api_used,
                     daily_limit=args.daily_limit)
        d = usage.data
        print(f"[usage] today {d['day_credits']}/{args.daily_limit}  "
              f"month {d['month_credits']}/{MONTHLY_CAP}  "
              f"({d['api_remaining']} left)  —  --status for detail",
              file=sys.stderr)

    if args.raw:
        out_obj = raw
    else:
        games = adapter.normalize(args.sport, raw)

        if args.merge and args.out:
            try:
                prev = json.loads(Path(args.out).read_text())
                old_games = prev["games"] if isinstance(prev, dict) else prev
                games = merge_forward(old_games, games, markets.split(","))
                print(f"[ok] merged into existing {args.out} "
                      f"(carried forward: "
                      f"{', '.join(m for m in CANON_MARKETS if m not in markets) or 'nothing'})",
                      file=sys.stderr)
            except (FileNotFoundError, ValueError, KeyError, TypeError):
                print(f"[note] --merge: no usable {args.out} to merge into; writing fresh",
                      file=sys.stderr)

        if args.drop_empty:
            games = [g for g in games if _has_any_market(g)]
        games.sort(key=lambda g: g.get("commence_time") or "")
        print(f"[ok] {len(games)} games — {args.sport} @ {adapter.book}"
              + (f" (next {args.days}d)" if args.days else ""),
              file=sys.stderr)
        meta = {
            "source": args.source,
            "book": adapter.book,
            "sport": args.sport,
            "pulled_markets": markets.split(","),      # what THIS run fetched
            "markets_present": markets_present(games),  # what has a line in the file
            "fetched_at": _now_iso(),
            "game_count": len(games),
            "window_days": args.days or None,
            "spread_convention": (
                "`spread` is the HOME team's line; POSITIVE = home favored "
                "(spread 3.5 -> home favored by 3.5, away is +3.5). "
                "spread_price_*, moneyline_* and *_price_* are American odds. "
                "Times are UTC ISO-8601. Each market has its own *_at stamp = "
                "when it was last pulled; null there = not pulled yet, null on a "
                "value = pulled but the book had no line."
            ),
        }
        if metered:
            meta["usage"] = usage.snapshot(args.daily_limit)
        out_obj = {"_meta": meta, "games": games}

    text = json.dumps(out_obj, indent=2)
    if args.out:
        p = Path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n")
        print(f"[ok] wrote {p}", file=sys.stderr)

        if metered:
            # record() already saved the canonical ledger (USAGE_FILE). Only drop
            # a sibling copy when --out lives somewhere else, and never let that
            # copy shadow the ledger — one file is the counter, by design.
            up = p.parent / "usage.json"
            if os.path.abspath(up) != os.path.abspath(USAGE_FILE):
                up.write_text(json.dumps(usage.snapshot(args.daily_limit), indent=2) + "\n")
                print(f"[ok] wrote {up} (copy; ledger is {USAGE_FILE})", file=sys.stderr)
            else:
                print(f"[ok] ledger {up}", file=sys.stderr)

        if not args.raw and not args.no_render:
            import render
            csv_path, html_path = render.render(
                out_obj["games"], meta=out_obj["_meta"], stem=p.stem, outdir=p.parent)
            print(f"[ok] wrote {csv_path}  +  {html_path}", file=sys.stderr)
        set_output("pulled", "true")
    else:
        print(text)
        set_output("pulled", "true")


if __name__ == "__main__":
    main()
