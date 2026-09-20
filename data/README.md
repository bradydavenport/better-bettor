# `data/` — the committed data files

Everything here is written by a workflow and read by a model, usually over a raw
URL:

```
https://raw.githubusercontent.com/<you>/better-bettor/main/data/<file>
```

Each file is self-describing: `_meta` carries the glossary, so a consumer needs
the file and nothing else. This README is the human version of that.

| file | what it is | written by | cadence |
|---|---|---|---|
| `nfl.json` | betting lines (moneyline / spread / total) per game | `fetch_lines.py` | daily 13:00 UTC + on demand |
| `nfl.csv`, `nfl.html` | rendered views of the same | `render.py` | alongside `nfl.json` |
| `usage.json` | The Odds API credit counter | `fetch_lines.py` | alongside `nfl.json` |
| `rosters.json` | starting QB + absences per team | `fetch_rosters.py` | Wed + Sat evenings ET |

`nfl.json` is documented in the root [README](../README.md#output-shape).
`rosters.json` is documented below.

---

## `rosters.json` — weekly personnel

Answers one question the lines data cannot: **who is actually playing.** Models
consuming `nfl.json` had no personnel data at all, which left anything reasoning
on top of them recalling starters from memory. This file replaces that recall.

Deliberately not a 53-man roster. The signal is starters and absences; a bloated
file is worse than a small one here. Team keys are the same abbreviations
`nfl.json` uses, so the two files join on team directly.

### Sources

Both free, keyless, no account:

- **QB1** — the [nflverse-data `depth_charts`
  release](https://github.com/nflverse/nflverse-data/releases/tag/depth_charts),
  `depth_charts_<season>.csv.gz`. Snapshot-shaped (a `dt` column, not `week`),
  rewritten about twice a day. Because one file carries every prior snapshot,
  `changed_from_last_week` is a real diff rather than a guess.
- **OUT / DOUBTFUL / IR** — ESPN's public injuries JSON. One call covers all 32
  teams with per-player report timestamps. Undocumented and unversioned: when it
  changes shape the fetcher writes nulls and says so in `_meta.degraded`, it does
  not fall back to stale data.

nflverse is the preferred source generally, but its `injuries` export carries
game-status designations only — no IR/PUP rows and no return dates. It *is* used
for injuries when backfilling a past week (`--week`), where it wins outright:
ESPN's endpoint is live-only, nflverse keeps every past week's official report.

### Shape

```json
{
  "_meta": {
    "fetched_at": "2026-09-20T03:18:09Z",
    "source": "nflverse depth_charts (QB) + ESPN injuries (OUT/DOUBTFUL/IR)",
    "sources": { "qb": {...}, "injuries": {...} },
    "season": 2026, "week": 2, "is_backfill": false,
    "team_count": 32, "teams_missing_qb": [], "unmapped_source_teams": [],
    "degraded": [], "notes": "..."
  },
  "teams": {
    "ATL": {
      "qb": {
        "name": "Michael Penix Jr.", "changed_from_last_week": true,
        "previous_name": "Tua Tagovailoa", "status": "Out",
        "as_of": "2026-09-19T11:56:08Z"
      },
      "out": [
        {"name": "Billy Bowman Jr.", "pos": "CB", "reason": "Achilles",
         "as_of": "2026-09-18T23:03Z"}
      ],
      "doubtful": [
        {"name": "Tua Tagovailoa", "pos": "QB", "reason": "Oblique (Strain)",
         "as_of": "2026-09-18T17:41Z"}
      ],
      "ir": [
        {"name": "Cameron Williams", "pos": "OL", "status": "Injured Reserve",
         "reason": "Ankle", "eligible_week": 5,
         "expected_return": "2026-10-11", "as_of": "2026-09-12T20:59Z"}
      ],
      "updated_at": "2026-09-20T03:18:09Z"
    }
  }
}
```

### Fields

**`_meta`**

| field | meaning |
|---|---|
| `fetched_at` | when the *current content* was first observed — not when the script last ran. The file is only rewritten on change (see below); the workflow run log is the record of checks. |
| `source` / `sources` | per-field provenance, including each upstream's own snapshot timestamp |
| `season`, `week` | the regular-season week this content describes |
| `is_backfill` | true when `--week` targeted a past week; `ir` is then empty for want of a historical source |
| `teams_missing_qb` | teams whose QB1 came back null. Empty is the healthy state. |
| `unmapped_source_teams` | upstream team codes that did not normalize — **rows under these were dropped.** Non-empty means a source changed its spelling and `TEAM_ALIASES` needs an entry. |
| `degraded` | plain-English list of what is missing this run and why. Empty is the healthy state. |
| `notes` | the same glossary, inline, for a consumer holding only the file |

**`teams.<ABBR>.qb`** — null when no source reported one.

| field | meaning |
|---|---|
| `name` | the depth-chart QB1 at `as_of`. **A depth chart, not a confirmed start.** |
| `changed_from_last_week` | vs. the newest snapshot ≥6 days older. `null` = no comparable earlier snapshot, which is *not* the same as `false`. |
| `previous_name` | who held the slot before — populated only when `changed_from_last_week` is true |
| `status` | that player's own injury designation. `null` = not on the report at all; `"Active"` = on the report but cleared; anything else means **the charted QB1 may not take the first snap.** Always read this alongside `name`. |
| `as_of` | timestamp of the depth-chart snapshot the name came from |

**`out` / `doubtful`** — official game-status designations, `{name, pos, reason, as_of}`.
Questionable is deliberately excluded as too noisy to act on.

**`ir`** — roster-move absences, `{name, pos, status, reason, eligible_week, expected_return, as_of}`.
`status` is the source's own wording (`"Injured Reserve"`, PUP, etc.), kept verbatim so a
designation nobody has seen before shows up instead of vanishing. `eligible_week` is derived
from `expected_return` and is an **estimate, not an official designation** — null when that
date is missing, falls inside the current week (the source reuses the next game date as a
placeholder), or lands outside the regular season. A null `eligible_week` beside a populated
`expected_return` means the return is not expected this season.

**`updated_at`** — when *that team's block last changed*, carried forward untouched
otherwise. A four-day-old stamp means four days without reported movement, **not** a failed
pull. This is how you tell stale from quiet.

### Two rules this file is built on

**Never fabricate.** Every null means the source did not report it. Nothing is
inferred, and nothing is carried over from a model's memory — guessed personnel
data is the failure this file exists to eliminate. If both sources fail the
script exits without writing rather than leave behind a file of nulls that looks
like real data.

**Commit only on change.** `rosters.json` is rewritten only when a team's block
actually differs, so `git log data/rosters.json` reads as a changelog of
personnel movement instead of a cron heartbeat:

```bash
git log --oneline data/rosters.json     # data: rosters wk2 — ATL, PHI
```

### Running it by hand

```bash
python fetch_rosters.py                        # current week -> data/rosters.json
python fetch_rosters.py --week 1               # backfill week 1 (no IR data)
python fetch_rosters.py --dry-run --out -      # print, write nothing
python -m unittest test_rosters -v             # abbreviation + null-safety tests
```

### Team abbreviations

`rosters.json` and `nfl.json` use one set of 32 abbreviations; `test_rosters.py`
asserts they match `fetch_lines.py`'s map, so the join cannot silently break.
Both upstreams disagree with that set in exactly one place each — normalized in
`TEAM_ALIASES`:

| source | writes | we write |
|---|---|---|
| nflverse | `LA` | `LAR` |
| ESPN | `WSH` | `WAS` |

`JAC`/`JAX` and `AZ`/`ARI` are the other usual suspects; both feeds already
agree with us there today, but the map and its tests cover them, along with
relocations (`STL`, `SD`, `OAK`) and PFR-style codes (`GNB`, `KAN`, `NWE`).
