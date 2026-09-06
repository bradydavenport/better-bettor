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
| `--days N` | only games starting within N days (default 8; use 12 for a full NFL week incl. MNF) |
| `--drop-empty` | skip games the book hasn't priced yet |
| `--source` | `theoddsapi` (default) or `sportsgameodds` (paid, carries Circa; adapter is a stub) |
| `--book` | override the bookmaker key |
| `--raw` | dump the untouched API response |

### Credits / rate limiting

`cost = markets × regions`, so the default pull is **3 credits**. A local counter
in `.usage.json` caps spend:

```bash
python fetch_lines.py --status              # today / month / buffer, no API call
python fetch_lines.py --sport nfl --force   # override the daily cap for one run
```

- `--daily-limit N` (default 15, or `$ODDS_DAILY_LIMIT`) — blocks with exit 1 when a call would exceed it
- `--force` — dips into the 50-credit monthly buffer; still hard-stops at 500
- the month tally self-corrects from the API's `x-requests-remaining` header

## Output shape

One object per game. **`spread` is the home line, positive = home favored**
(`"spread": 3.5` ⇒ home favored by 3.5). This is non-standard — say so when
handing the file to another agent.

```json
{
  "commence_time": "2026-09-13T17:00:00Z",
  "home_team": "Pittsburgh Steelers", "away_team": "Atlanta Falcons",
  "home_abbr": "PIT", "away_abbr": "ATL",
  "book": "pinnacle",
  "spread": 3.0, "spread_price_home": -121, "spread_price_away": 107,
  "total": 42.5, "total_over_price": -105, "total_under_price": -111,
  "moneyline_home": -179, "moneyline_away": 157,
  "last_update": "2026-09-05T22:57:23Z", "fetched_at": "2026-09-05T22:57:23Z"
}
```

`lines.csv` is the same fields and sign convention, ~70% fewer bytes — use it
for large multi-week dumps; otherwise `lines.json` is small and self-describing.

## View

`lines.html` is a standalone viewer — double-click to open, no server. Local
kickoff times, freshness stamp, favorite side highlighted, light/dark aware.
Regenerate any time without spending a pull:

```bash
python render.py                 # lines.json -> lines.csv + lines.html
python render.py week1.json      # any normalized file
```

## Getting lines into a plain Claude.ai chat

`.github/workflows/pull-lines.yml` runs the fetch on a schedule (4×/day ≈ 360
credits/month) and commits `data/nfl.json`. To wire it up:

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

Then in any chat:

> Fetch `https://raw.githubusercontent.com/<you>/better-bettor/main/data/nfl.json`
> and use it as the current NFL lines. `spread` is the home line, positive means
> home favored.

Adjust cadence by editing the `cron:` line in the workflow. Note: the
`.usage.json` limiter does **not** persist between CI runs, so the schedule
frequency is the real budget — keep it sane.

### Keeping the code private instead

Have the workflow write to a **GitHub Gist** (raw gist URLs work without auth,
even for secret gists): add a PAT with `gist` scope as a secret and replace the
commit step with `gh gist edit <id> -a data/nfl.json`. Left as an exercise.
