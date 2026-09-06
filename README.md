# better-bettor

Pull betting lines from an odds source, normalize them, and hand them to a
model (or another Claude chat).

Circa has no free real-time feed — no public web client, odds live only in the
native apps — so the default source is **The Odds API** with **Pinnacle** as the
sharp reference book. The adapter layer leaves room to swap in a paid Circa
source (SportsGameOdds) later without changing the output shape.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env      # then paste your ODDS_API_KEY
```

Free key: <https://the-odds-api.com/> — 500 credits/month, no card, no overage
(the API just 401s until the 1st if you run out).

## Fetch

```bash
python fetch_lines.py --sport nfl --days 12 --drop-empty --out lines.json
```

Writes `lines.json` and (unless `--no-render`) `lines.csv` + `lines.html`.

| flag | meaning |
|---|---|
| `--sport` | `nfl` (default), `ncaaf`, `nba`, `ncaab`, `mlb`, `nhl` |
| `--markets` | `ml`, `spread`, `total` (comma-sep) or `all` (default). 1 credit per market. |
| `--merge` | patch only the pulled markets into an existing `--out` file; the rest keep their last value + `*_at` stamp |
| `--days N` | only games starting within N days (default 8; use 12 for a full NFL week incl. MNF) |
| `--drop-empty` | drop games with no line for any market (applied after `--merge`) |
| `--source` | `theoddsapi` (default) or `sportsgameodds` (paid, carries Circa; adapter is a stub) |
| `--book` | override the bookmaker key |
| `--raw` | dump the untouched API response |

```bash
python fetch_lines.py --sport nfl --markets spread --out spreads.json          # 1 credit
python fetch_lines.py --sport nfl --markets total --merge --out lines.json      # patch totals in
```

Aliases: `moneyline`/`h2h`/`money` → ml, `spreads`/`ats` → spread,
`totals`/`ou` → total.

### Per-market pulls + `--merge`

Each market has its own `*_at` timestamp (`moneyline_at`, `spread_at`,
`total_at`) = when it was last pulled. `--merge` reads the existing `--out`
file and carries the markets you *didn't* pull this run — value and stamp —
straight through, so **one file accumulates all three at their own cadences**:

- `--markets ml,spread` → moneyline + spread refreshed, `total`/`total_at` untouched
- `--markets total --merge` later → `total` refreshed, moneyline + spread untouched

`_meta.pulled_markets` = what this run fetched; `_meta.markets_present` = what
has a line in the file.

### Credits / rate limiting

`cost = markets × regions` (one region), so an all-markets pull is **3 credits**
and `--markets spread` is **1**. A local counter in `.usage.json` caps spend:

```bash
python fetch_lines.py --status              # today / month / buffer, no API call
python fetch_lines.py --sport nfl --force   # override the daily cap for one run
```

- `--daily-limit N` (default 15, or `$ODDS_DAILY_LIMIT`) — blocks with exit 1 when a call would exceed it
- `--force` — dips into the 50-credit monthly buffer; still hard-stops at 500
- the month tally self-corrects from the API's `x-requests-remaining` header

## Output shape

Self-describing object — the consumer needs nothing but the file:

```json
{
  "_meta": {
    "source": "theoddsapi", "book": "pinnacle", "sport": "nfl",
    "pulled_markets": ["h2h", "spreads"],
    "markets_present": ["h2h", "spreads", "totals"],
    "fetched_at": "2026-09-13T13:00:04Z", "game_count": 16, "window_days": 12,
    "spread_convention": "`spread` is the HOME line; POSITIVE = home favored ...",
    "usage": { "credits_used": 138, "credits_remaining": 362, "monthly_cap": 500,
               "pulls_left_est": 120, "month": "2026-09", "updated_at": "..." }
  },
  "games": [
    {
      "commence_time": "2026-09-13T17:00:00Z",
      "home_team": "Pittsburgh Steelers", "away_team": "Atlanta Falcons",
      "home_abbr": "PIT", "away_abbr": "ATL", "book": "pinnacle",
      "moneyline_home": -179, "moneyline_away": 157,
      "moneyline_at": "2026-09-13T13:00:04Z",
      "spread": 3.0, "spread_price_home": -121, "spread_price_away": 107,
      "spread_at": "2026-09-13T13:00:04Z",
      "total": 42.5, "total_over_price": -105, "total_under_price": -111,
      "total_at": "2026-09-12T22:11:40Z",
      "book_last_update": "2026-09-13T12:58:00Z"
    }
  ]
}
```

**`spread` is the home line, positive = home favored** (`3.0` ⇒ home favored by
3). Non-standard, but `_meta.spread_convention` states it in-band so you don't
have to. `--raw` skips the wrapper entirely.

`lines.csv` is `games` only (same fields / sign), ~70% fewer bytes — for large
multi-week dumps.

## Credit counter — `data/usage.json`

Every `--out` run also writes a sibling `usage.json` (the `_meta.usage` block on
its own). The scheduled workflow commits it, so there's always a current,
fetchable record of where the month stands:

```
https://raw.githubusercontent.com/<you>/better-bettor/main/data/usage.json
```

Numbers come from the API response headers, so they count **every** call on the
account — local runs and CI alike. `python fetch_lines.py --status` shows the
same thing locally (plus the `--force` buffer) without spending a call.

## View

`lines.html` is a standalone viewer — double-click to open, no server. Local
kickoff times, freshness stamp, credit counter, favorite side highlighted,
light/dark aware. Regenerate any time without spending a pull:

```bash
python render.py                 # lines.json -> lines.csv + lines.html
python render.py data/nfl.json   # wrapped or bare-list file, either works
```

## Getting lines into a plain Claude.ai chat

`.github/workflows/pull-lines.yml` runs the fetch on a schedule (1×/day,
ml + spread only ≈ 60 credits/month) and commits `data/nfl.json`. To wire it up:

1. **Create the repo as public** (raw URLs on private repos need an expiring
   token that Claude.ai can't use). Betting lines aren't sensitive; the key
   stays a secret regardless.
   ```bash
   gh repo create better-bettor --public --source=. --remote=origin --push
   ```
2. Add the key as a repo secret:
   ```bash
   gh secret set ODDS_API_KEY      # paste when prompted
   ```
3. Optionally trigger a first run now: Actions tab → **pull-lines** → *Run
   workflow*, or `gh workflow run pull-lines.yml`.

### Pulling a market on demand

The scheduled run does **ml + spread**. To fold in **totals** (or refresh just
one market), trigger the workflow with an input — it `--merge`s into the same
`data/nfl.json`, leaving the other markets and their timestamps alone:

```bash
gh workflow run pull-lines.yml -f markets=total     # or: ml | spread | ml,spread | all
```

or Actions tab → **pull-lines** → *Run workflow* → pick from the dropdown.
Locally it's the same idea:

```bash
python fetch_lines.py --sport nfl --days 12 --merge --markets total --out data/nfl.json
```

### The chat command

The file explains its own fields (`_meta`), so the whole prompt is:

> Fetch `https://raw.githubusercontent.com/<you>/better-bettor/main/data/nfl.json` — use `.games`.

**Make it a one-worder:** put that URL in a Claude
[Project](https://claude.ai/projects)'s custom instructions —

> When I ask for "lines", fetch `https://raw.githubusercontent.com/<you>/better-bettor/main/data/nfl.json` and work from `.games`; `_meta` documents the fields.

— then any chat in that project just needs **"lines"**. Raw GitHub caches for
~5 min; if you just ran the workflow, add `?t=NNN` (any changing number) to
bypass it.

### Cron

Runs 1×/day (`0 13 * * *` = 13:00 UTC) with `--merge --markets ml,spread` =
2 credits/run, ~60/month. Totals come from manual runs (above) and stay in the
file between cron runs because every run merges rather than overwrites. That
leaves ~440 of the 500 for on-demand pulls. `data/nfl.json` + `data/usage.json`
are re-committed each run they change — the durable counter, also a heartbeat.
The `.usage.json` limiter does **not** persist between CI runs, so the `cron:`
line + market choices are the real budget — keep it under 500 (over-cap just
401s, no charge).

### Keeping the code private instead

Have the workflow write to a **GitHub Gist** (raw gist URLs work without auth,
even for secret gists): add a PAT with `gist` scope as a secret and replace the
commit step with `gh gist edit <id> -a data/nfl.json`. Left as an exercise.
