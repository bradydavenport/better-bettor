"""
close_gate.py — decide whether right now is a closing-line moment.

Runs on a */15 cron all week. Almost every run is a no-op: it asks the free
/events endpoint when the next kickoffs are, finds nothing imminent, and exits
without spending a credit, writing a file, or producing a commit. Only when a
game is about to start does it open the gate and let the workflow pull.

    python3 scripts/close_gate.py                 # decide (writes nothing)
    python3 scripts/close_gate.py --mark          # after a successful pull

Stdlib only, on purpose. The no-op path is the overwhelmingly common one, so it
must not need `pip install` — the workflow runs this step on the runner's
preinstalled python3 and installs dependencies only once the gate opens.

WHY /events AND NOT nfl.json
----------------------------
nfl.json is Pinnacle-only and written with --drop-empty, so a game Pinnacle has
not listed is simply absent from it. That is not hypothetical: at the
2026-09-28T02:12Z pull, /events had 16 games and nfl.json kept 12, and all four
missing games were ones Pinnacle had not priced while 7-9 other books had. A gate
reading nfl.json would never snipe those closes at all.

/events is book-independent and free: the docs say "This endpoint does not count
against the usage quota", and a real response confirms it (x-requests-last: 0,
used unchanged). It is also immune to flex scheduling, which a cached local
schedule would not be.

THE GATE
--------
Open when any game kicks off within --window minutes and has not been sniped.
Never open once a game has started: an in-play line is not the close.

MARKING
-------
One pull returns every game in the response, so it captures the close for every
game kicking off at roughly the same time. Those all get marked, otherwise a
Sunday slate would burn a separate pull per kickoff minute.

The horizon is anchored to the EARLIEST game that opened the gate, plus --grace
minutes — not to `now`. Anchoring to an absolute kickoff time keeps --mark
deterministic even though it runs a minute or two after the decision.

Marking is deliberately a SEPARATE invocation, run only after the pull
succeeds. If the gate marked games up front and the pull then failed, that close
would be recorded as captured and never retried — the one outcome worth engineering
against, because a missed close cannot be recovered later.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

EVENTS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/events"
DEFAULT_STATE = "data/sniped.json"
PRUNE_DAYS = 14
MISS_LOOKBACK_HOURS = 6

# How far ahead a poller is willing to wait. GitHub caps a job at 6h, so this
# leaves headroom to finish the last pull and commit before being killed.
POLL_LOOKAHEAD_MIN = 330          # 5.5h
DOORBELL_MAX_GAP_MIN = 45
GAMEDAY_AHEAD_H = 12
GAMEDAY_BEHIND_H = 6


def _api_key():
    """ODDS_API_KEY from the environment, falling back to .env.

    Parsed by hand rather than with python-dotenv: this script is stdlib-only so
    the no-op path needs no `pip install`, and CI runs it before dependencies
    exist. In Actions the key arrives as an env var and .env is never present.
    """
    key = os.getenv("ODDS_API_KEY")
    if key:
        return key
    env = Path(__file__).resolve().parent.parent / ".env"
    try:
        for line in env.read_text().splitlines():
            line = line.strip()
            if line.startswith("ODDS_API_KEY"):
                _, _, val = line.partition("=")
                return val.strip().strip('"').strip("'") or None
    except OSError:
        pass
    return None


def _ssl_context():
    """A verifying SSL context that works on CI and on a local macOS python.

    Linux runners trust the system store, so the default context is fine there.
    python.org macOS builds ship no system trust for urllib, so a local run dies
    with CERTIFICATE_VERIFY_FAILED. certifi is already present transitively (via
    requests) but is imported optionally so this file keeps working with nothing
    installed, which is the whole point of the stdlib-only gate.
    """
    try:
        import ssl

        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:      # noqa: BLE001 - fall back to the system default
        return None


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class GateError(RuntimeError):
    """Upstream failed. The gate closes; it never guesses a schedule."""


def fetch_events(sport, api_key):
    url = EVENTS_URL.format(sport=sport) + "?" + urllib.parse.urlencode(
        {"apiKey": api_key})
    req = urllib.request.Request(url, headers={"User-Agent": "better-bettor/close-gate"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as r:
            cost = r.headers.get("x-requests-last")
            if cost not in (None, "0"):
                # /events is documented free. If that ever changes, say so loudly
                # rather than quietly burning a credit every 15 minutes.
                print(f"::warning::/events charged {cost} credit(s) this call — it is "
                      f"documented as free. A */15 cron makes this expensive; "
                      f"revisit the gate's schedule source.")
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            # No rate limit is documented and a 20-call burst passed cleanly, but
            # if an undocumented one exists, a closed gate is the safe failure.
            raise GateError("rate limited (HTTP 429) — gate closed for this run")
        raise GateError(f"/events HTTP {e.code}: {e.read()[:200]!r}")
    except (urllib.error.URLError, ValueError, TimeoutError) as e:
        raise GateError(f"/events unreachable: {e}")


def load_state_ref(ref):
    """sniped map as committed at `ref`, e.g. origin/main:data/sniped.json.

    A poller lives for hours, so its checkout goes stale. Another run finishing a
    snipe pushes to origin, and only this read sees it. The concurrency group
    stops two runs pulling simultaneously; THIS is what stops the run that starts
    after a poller from re-pulling a game the poller already took.
    """
    import subprocess
    try:
        subprocess.run(["git", "fetch", "-q", "origin"], capture_output=True,
                       timeout=60)
        r = subprocess.run(["git", "show", ref], capture_output=True, text=True,
                           timeout=60)
        if r.returncode != 0:
            return {}
        doc = json.loads(r.stdout)
    except Exception:                          # noqa: BLE001
        return {}
    return (doc.get("sniped") or {}) if isinstance(doc, dict) else {}


def load_state(path):
    try:
        doc = json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return (doc.get("sniped") or {}) if isinstance(doc, dict) else {}


def load_missed(path):
    """game_ids whose miss has already been REPORTED.

    A missed close is an event, not a condition: it never recovers, so the alarm
    must go out exactly once. The gate's 6-hour lookback would otherwise re-report
    the same game on all 24 runs inside that window.
    """
    try:
        doc = json.loads(Path(path).read_text())
    except (FileNotFoundError, ValueError):
        return {}
    return (doc.get("missed") or {}) if isinstance(doc, dict) else {}


def save_state(path, sniped, now, missed=None):
    """Write the ledger of sniped games, pruned so it cannot grow forever."""
    cutoff = now - timedelta(days=PRUNE_DAYS)
    kept = {gid: rec for gid, rec in sniped.items()
            if (_parse(rec.get("commence_time")) or now) >= cutoff}
    pruned = len(sniped) - len(kept)
    kept_missed = {gid: rec for gid, rec in (missed or {}).items()
                   if (_parse(rec.get("commence_time")) or now) >= cutoff}
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "_meta": {
            "updated_at": _iso(now),
            "count": len(kept),
            "pruned_older_than_days": PRUNE_DAYS,
            "notes": ("game_ids whose closing line has already been captured, so "
                      "the */15 gate does not pull for them again. Keyed by The "
                      "Odds API event id. Entries are dropped "
                      f"{PRUNE_DAYS} days after kickoff. Written only after a "
                      "pull succeeds — never before, so a failed pull retries."),
            "missed_note": ("games whose close was NEVER captured and whose miss "
                            "has already been reported as a GitHub issue comment. "
                            "Tracked so the 6h lookback reports each game once "
                            "instead of once every 15 minutes. These are losses, "
                            "not recoveries."),
        },
        "sniped": dict(sorted(kept.items(), key=lambda kv: kv[1].get("commence_time") or "")),
        "missed": dict(sorted(kept_missed.items(),
                             key=lambda kv: kv[1].get("commence_time") or "")),
    }, indent=2) + "\n")
    return len(kept), pruned


def decide(events, sniped, now, window_min, grace_min):
    """(due, horizon, missed) — what is closing, how far to mark, what we lost."""
    upcoming = []
    missed = []
    for e in events:
        k = _parse(e.get("commence_time"))
        if not k:
            continue
        mins = (k - now).total_seconds() / 60
        if mins <= 0:
            # already started: never pull, but notice a close we failed to catch
            if e.get("id") not in sniped and mins > -60 * MISS_LOOKBACK_HOURS:
                missed.append((e, mins))
            continue
        upcoming.append((e, mins, k))

    due = [(e, mins, k) for e, mins, k in upcoming
           if mins <= window_min and e.get("id") not in sniped]
    if not due:
        return [], None, missed

    # anchor the horizon to the earliest kickoff that opened the gate
    earliest = min(k for _, _, k in due)
    horizon = earliest + timedelta(minutes=grace_min)
    return due, horizon, missed


def pollable(events, sniped, now, lookahead_min=POLL_LOOKAHEAD_MIN):
    """Unsniped kickoffs far enough out to be worth waiting for, soonest first.

    The bash loop uses this to decide between sleeping and exiting. The gate
    itself never sleeps: keeping it a pure decision function is what makes the
    timing testable, and it means a hung poll cannot wedge inside Python.
    """
    out = []
    for e in events:
        k = _parse(e.get("commence_time"))
        if not k or e.get("id") in sniped:
            continue
        mins = (k - now).total_seconds() / 60
        if 0 < mins <= lookahead_min:
            out.append((e, mins))
    return sorted(out, key=lambda x: x[1])


def in_horizon(events, horizon, now):
    """Games this pull captured the close for: kicking off, up to the horizon."""
    out = []
    for e in events:
        k = _parse(e.get("commence_time"))
        if k and now < k <= horizon:
            out.append(e)
    return out


DEADMAN_HOURS = 26
DEADMAN_WRITER = "pull-lines"


def deadman_check(nfl_path, now, max_hours=DEADMAN_HOURS, writer=DEADMAN_WRITER):
    """Has the daily line pull stopped? Reads a local file; costs nothing.

    Checks `_meta.last_pull[writer]` specifically, NOT `_meta.fetched_at`. The
    sniper writes nfl.json too, so fetched_at only says "something pulled" — it
    would be refreshed by a Sunday snipe while the daily pull had been dead for
    days. The per-writer map is what can answer the actual question.

    Returns a message when the alarm should fire, else None.
    """
    try:
        doc = json.loads(Path(nfl_path).read_text())
    except FileNotFoundError:
        return f"{nfl_path} does not exist — the line pipeline has never run"
    except ValueError:
        return f"{nfl_path} is not readable JSON — the line pipeline may be broken"

    meta = (doc.get("_meta") or {}) if isinstance(doc, dict) else {}
    last = (meta.get("last_pull") or {}).get(writer)
    if not last:
        # Pre-dates last_pull, or that writer has genuinely never run. Either way
        # the switch cannot vouch for the pipeline, and silence would be a lie.
        return (f"{nfl_path} has no _meta.last_pull['{writer}'] — cannot confirm "
                f"the daily pull is alive (file predates writer tracking, or "
                f"{writer} has not run since it was added)")
    ts = _parse(last)
    if not ts:
        return f"_meta.last_pull['{writer}'] is not a readable timestamp: {last!r}"
    age = (now - ts).total_seconds() / 3600
    if age > max_hours:
        return (f"{writer} last pulled {age:.1f}h ago ({last}), over the "
                f"{max_hours}h limit — the daily line pull looks dead")
    return None


ALARM_STALE = "alarm:pull-lines-stale"
ALARM_MISSED = "alarm:missed-close"
ALARM_DOORBELL = "alarm:doorbell-stale"


def is_game_day(events, now):
    """True when a kickoff is close enough that the doorbell matters.

    Outside that, a quiet doorbell is normal and alarming on it would page for
    every weekday night.
    """
    for e in events:
        k = _parse(e.get("commence_time"))
        if not k:
            continue
        h = (k - now).total_seconds() / 3600
        if -GAMEDAY_BEHIND_H <= h <= GAMEDAY_AHEAD_H:
            return True
    return False


def last_run_created(repo, workflow, token, exclude_run_id=None):
    """When a run of `workflow` was last CREATED, newest first.

    Creation, deliberately — not start and not completion. A long-lived poller
    holds the concurrency group, so the dispatches arriving behind it are created
    and then immediately evicted. Those runs never start and never succeed, but
    they prove the doorbell is ringing. Measuring starts would read a healthy
    doorbell as dead for as long as a poller is alive.
    """
    url = (f"https://api.github.com/repos/{repo}/actions/workflows/"
           f"{workflow}/runs?per_page=30")
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "better-bettor/close-gate",
    })
    with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as r:
        runs = (json.loads(r.read().decode()) or {}).get("workflow_runs") or []
    return newest_created(runs, exclude_run_id)


def newest_created(runs, exclude_run_id=None):
    """Newest `created_at` across runs of ANY status, excluding this run.

    Status is deliberately ignored. While a poller holds the concurrency group the
    dispatches behind it are created and then evicted, so they sit at `cancelled`
    with no start time — yet their existence is the proof the doorbell is ringing.
    Filtering to successful or even started runs would report a perfectly healthy
    doorbell as dead for the entire life of every poller.
    """
    stamps = [_parse(x.get("created_at")) for x in runs
              if str(x.get("id")) != str(exclude_run_id)]
    return max([s for s in stamps if s], default=None)


def doorbell_check(events, now, repo, workflow, token, exclude_run_id=None,
                   max_gap_min=DOORBELL_MAX_GAP_MIN):
    """Is the external doorbell still ringing? Returns a message, or None."""
    if not is_game_day(events, now):
        return None
    try:
        last = last_run_created(repo, workflow, token,
                                exclude_run_id=exclude_run_id)
    except Exception as e:                      # noqa: BLE001
        return (f"could not read {workflow} run history to check the doorbell "
                f"({type(e).__name__}: {e})")
    if not last:
        return (f"no previous {workflow} run found at all — the doorbell has "
                f"never rung")
    gap = (now - last).total_seconds() / 60
    if gap > max_gap_min:
        return (f"the last {workflow} run was created {gap:.0f} min ago, over "
                f"the {max_gap_min} min limit, on a game day. The external "
                f"cron-job.org doorbell has probably stopped — check it, and "
                f"check whether the fine-grained PAT has expired.")
    return None


def alarm(label, title, body, state="firing", throttle_hours=24, auto_close=True):
    return {"label": label, "title": title, "body": body, "state": state,
            "throttle_hours": throttle_hours, "auto_close": auto_close}


def set_output(key, value):
    """Hand a decision to the workflow. Silent when not running in Actions."""
    path = os.getenv("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as f:
        f.write(f"{key}={value}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sport", default="americanfootball_nfl")
    ap.add_argument("--window", type=int, default=45,
                    help="open the gate when a game kicks off within N minutes "
                         "(default: 45)")
    ap.add_argument("--grace", type=int, default=15,
                    help="also mark games kicking off within N minutes of the "
                         "earliest game that opened the gate (default: 15)")
    ap.add_argument("--state", default=DEFAULT_STATE,
                    help=f"sniped-games ledger (default: {DEFAULT_STATE})")
    ap.add_argument("--mark", action="store_true",
                    help="record the snipe. Run ONLY after a pull succeeded.")
    ap.add_argument("--horizon", default=None,
                    help="with --mark: the horizon the gate decided (ISO 8601). "
                         "Passing it through makes marking exactly reproduce the "
                         "decision instead of recomputing against a later clock.")
    ap.add_argument("--ids-out", default=None,
                    help="with --mark: also write the captured game_ids here, so "
                         "a replay after a rejected push can re-apply them")
    ap.add_argument("--nfl", default="data/nfl.json",
                    help="file the dead-man's switch inspects (default: data/nfl.json)")
    ap.add_argument("--deadman-hours", type=int, default=DEADMAN_HOURS,
                    help=f"alarm when the daily pull is older than this "
                         f"(default: {DEADMAN_HOURS})")
    ap.add_argument("--state-ref", default=None,
                    help="also treat games sniped at this git ref as sniped, e.g. "
                         "origin/main:data/sniped.json. Lets a long-lived poller "
                         "see snipes other runs have pushed since it checked out.")
    ap.add_argument("--lookahead", type=int, default=POLL_LOOKAHEAD_MIN,
                    help=f"minutes ahead worth staying alive for "
                         f"(default: {POLL_LOOKAHEAD_MIN} = 5.5h, under the 6h "
                         f"job cap)")
    ap.add_argument("--heartbeat-repo", default=None,
                    help="owner/name — enables the doorbell heartbeat check")
    ap.add_argument("--heartbeat-workflow", default="snipe-closes.yml")
    ap.add_argument("--heartbeat-exclude-run", default=None,
                    help="this run's id, so it does not vouch for itself")
    ap.add_argument("--alarms-out", default=None,
                    help="write alarm records here for scripts/alarm.py to route "
                         "into GitHub issues")
    ap.add_argument("--no-deadman", action="store_true",
                    help="skip the dead-man's switch")
    ap.add_argument("--now", default=None,
                    help="override the clock (ISO 8601) — for tests")
    ap.add_argument("--events-file", default=None,
                    help="read events from a file instead of the API — for tests")
    args = ap.parse_args()

    now = _parse(args.now) or _now()

    # FIRST, before anything that can fail or return early. It reads one local
    # file and is independent of /events — so an upstream outage, a missing API
    # key or a closed gate must not silence it. It previously sat after the
    # events fetch, which meant a /events outage muted the alarm for the daily
    # pull as well: two unrelated failures collapsed into one silence.
    alarms = []
    if not args.no_deadman:
        stale = deadman_check(args.nfl, now, args.deadman_hours)
        if stale:
            print(f"::error::dead-man's switch: {stale}")
            print(f"[alarm] {stale}", file=sys.stderr)
            alarms.append(alarm(
                ALARM_STALE,
                "[alarm] pull-lines has stopped pulling",
                f"The dead-man's switch tripped.\n\n> {stale}\n\n"
                f"`data/nfl.json` `_meta.last_pull['pull-lines']` is the checked "
                f"field. Look at the **pull-lines** workflow: a cancelled run, a "
                f"credit block, or an API failure all look like this.\n\n"
                f"This issue closes itself once a pull-lines run lands."))
        else:
            # a condition: emit `clear` every run so a fixed pipeline auto-closes
            alarms.append(alarm(
                ALARM_STALE, "[alarm] pull-lines has stopped pulling",
                "The daily line pull is current again.", state="clear"))

    if args.events_file:
        try:
            events = json.loads(Path(args.events_file).read_text())
        except (OSError, ValueError) as e:
            print(f"::warning::close gate: --events-file unreadable: {e}")
            set_output("pull", "false")
            return 0
    else:
        key = _api_key()
        if not key:
            print("::error::ODDS_API_KEY not set — gate cannot read the schedule")
            set_output("pull", "false")
            return 0
        try:
            events = fetch_events(args.sport, key)
        except GateError as e:
            # A closed gate is the safe failure: no pull, no write, no commit.
            print(f"::warning::close gate: {e}")
            print(f"[skip] {e}", file=sys.stderr)
            set_output("pull", "false")
            return 0

    sniped = dict(load_state(args.state))
    if args.state_ref:
        remote = load_state_ref(args.state_ref)
        new_to_us = set(remote) - set(sniped)
        sniped.update(remote)
        if new_to_us:
            print(f"[note] {len(new_to_us)} game(s) already sniped by another run "
                  f"(seen via {args.state_ref})", file=sys.stderr)

    # Doorbell heartbeat: costs no credits, writes nothing, needs no commit.
    if args.heartbeat_repo:
        token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
        if not token:
            print("[note] no GITHUB_TOKEN; skipping the doorbell heartbeat",
                  file=sys.stderr)
        else:
            dead = doorbell_check(events, now, args.heartbeat_repo,
                                  args.heartbeat_workflow, token,
                                  exclude_run_id=args.heartbeat_exclude_run)
            if dead:
                print(f"::error::doorbell: {dead}")
                alarms.append(alarm(
                    ALARM_DOORBELL, "[alarm] the snipe doorbell has stopped",
                    f"{dead}\n\nWithout the doorbell, snipe-closes falls back to "
                    f"its `*/15` schedule, which this repo only honours about 4% "
                    f"of the time — so closes will be missed.\n\nThis issue "
                    f"closes itself once runs resume."))
            else:
                alarms.append(alarm(
                    ALARM_DOORBELL, "[alarm] the snipe doorbell has stopped",
                    "Runs are being created again.", state="clear"))

    due, horizon, missed = decide(events, sniped, now, args.window, args.grace)
    waiting = pollable(events, sniped, now, args.lookahead)

    # Missed closes are EVENTS. Report each game exactly once, then remember it,
    # so the 6h lookback does not re-report the same game on all 24 runs inside
    # the window. No throttle: a genuinely new miss should page immediately.
    reported = load_missed(args.state)
    fresh = [(e, mins) for e, mins in missed if e.get("id") not in reported]
    for e, mins in sorted(missed, key=lambda x: x[1]):
        seen = "" if e.get("id") not in reported else " (already reported)"
        print(f"::warning::missed close: {e.get('away_team')} @ {e.get('home_team')} "
              f"kicked off {abs(mins):.0f} min ago and was never sniped{seen}")

    if fresh and not args.mark:
        lines = "\n".join(
            f"- **{e.get('away_team')} @ {e.get('home_team')}** — kicked off "
            f"{e.get('commence_time')} ({abs(mins):.0f} min ago), game_id `{e.get('id')}`"
            for e, mins in sorted(fresh, key=lambda x: x[1]))
        alarms.append(alarm(
            ALARM_MISSED,
            "[alarm] closing line missed",
            f"These games started without their closing line being captured. "
            f"A missed close cannot be recovered later.\n\n{lines}\n\n"
            f"Likely causes: the gate was closed or rate limited through the whole "
            f"45-minute window, every push attempt failed, or the schedule moved "
            f"after the last gate run.\n\n"
            f"_This issue stays open until you close it — a missed close is an "
            f"event, not a condition, so it never 'recovers'. Each game is "
            f"reported once._",
            throttle_hours=0, auto_close=False))
        for e, _ in fresh:
            reported[e["id"]] = {
                "commence_time": e.get("commence_time"),
                "matchup": f"{e.get('away_team')} @ {e.get('home_team')}",
                "reported_at": _iso(now),
            }
        save_state(args.state, load_state(args.state), now, missed=reported)
        print(f"[ok] recorded {len(fresh)} newly missed close(s) in {args.state} "
              f"so they are not reported again", file=sys.stderr)
        set_output("missed_recorded", "true")

    def flush_alarms():
        if args.alarms_out:
            Path(args.alarms_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.alarms_out).write_text(json.dumps(alarms, indent=2) + "\n")
            print(f"[ok] wrote {len(alarms)} alarm record(s) to {args.alarms_out}",
                  file=sys.stderr)

    if args.mark:
        h = _parse(args.horizon) or horizon
        if not h:
            print("[note] --mark with nothing due and no --horizon; nothing recorded",
                  file=sys.stderr)
            return 0
        captured = in_horizon(events, h, now)
        for e in captured:
            sniped[e["id"]] = {
                "commence_time": e.get("commence_time"),
                "matchup": f"{e.get('away_team')} @ {e.get('home_team')}",
                "sniped_at": _iso(now),
            }
        if args.ids_out:
            Path(args.ids_out).parent.mkdir(parents=True, exist_ok=True)
            Path(args.ids_out).write_text(json.dumps({
                e["id"]: {
                    "commence_time": e.get("commence_time"),
                    "matchup": f"{e.get('away_team')} @ {e.get('home_team')}",
                    "sniped_at": _iso(now),
                } for e in captured}, indent=2) + "\n")
            print(f"[ok] wrote {len(captured)} id(s) to {args.ids_out} for replay",
                  file=sys.stderr)
        kept, pruned = save_state(args.state, sniped, now)
        print(f"[ok] marked {len(captured)} game(s) sniped through {_iso(h)} "
              f"-> {args.state} ({kept} tracked"
              + (f", {pruned} pruned)" if pruned else ")"), file=sys.stderr)
        for e in captured:
            print(f"     {e.get('commence_time')}  {e.get('away_team')} @ {e.get('home_team')}",
                  file=sys.stderr)
        flush_alarms()
        return 0

    if not due:
        nxt = min((( _parse(e['commence_time']) - now).total_seconds() / 60
                   for e in events if _parse(e.get("commence_time"))
                   and _parse(e["commence_time"]) > now), default=None)
        when = f"next kickoff in {nxt:.0f} min" if nxt is not None else "no upcoming games"
        print(f"[skip] gate closed — {when}, window is {args.window} min. "
              f"No API pull, no write, no commit.", file=sys.stderr)
        set_output("pull", "false")
        # Tell the caller whether sleeping is worth it, so a poller waits for a
        # kickoff that is coming and exits when nothing is.
        set_output("keep_polling", "true" if waiting else "false")
        if waiting:
            e, mins = waiting[0]
            print(f"[wait] {len(waiting)} unsniped kickoff(s) within "
                  f"{args.lookahead} min; next is {e.get('away_team')} @ "
                  f"{e.get('home_team')} in {mins:.0f} min", file=sys.stderr)
        flush_alarms()
        return 0

    captured = in_horizon(events, horizon, now)
    print(f"[ok] gate OPEN — {len(due)} game(s) inside the {args.window}-min window; "
          f"this pull covers {len(captured)} through {_iso(horizon)}", file=sys.stderr)
    for e, mins, _ in sorted(due, key=lambda x: x[1]):
        print(f"     T-{mins:4.0f} min  {e.get('away_team')} @ {e.get('home_team')}",
              file=sys.stderr)
    set_output("pull", "true")
    set_output("horizon", _iso(horizon))
    set_output("game_count", str(len(captured)))
    # More games may come later in this poller's lifetime; the loop re-enters.
    still = [w for w in waiting if w[0].get("id") not in
             {e["id"] for e in captured}]
    set_output("keep_polling", "true" if still else "false")
    flush_alarms()
    return 0


if __name__ == "__main__":
    sys.exit(main())
