"""
alarm.py — route alarm records to GitHub issues, so alarms actually reach a human.

Workflow annotations scroll away and nobody reads a green run's log. An issue
arrives as a notification and stays until dealt with.

    python3 scripts/alarm.py --from-file "$RUNNER_TEMP/alarms.json"

Detection lives in close_gate.py and fetch_lines.py; this file only talks to
GitHub. Keeping them apart means the detectors stay testable without a network
and this stays testable without touching a real repo (see --gh-bin).

ALARM RECORD
------------
    {"label": "alarm:pull-lines-stale",
     "title": "[alarm] pull-lines has stopped pulling",   # fixed, so it dedupes
     "body":  "...",
     "state": "firing" | "clear",
     "throttle_hours": 24,       # 0 = comment every time it fires
     "auto_close": true}

BEHAVIOUR
---------
firing, no open issue with that label  -> create the label if needed, open one
firing, an open issue exists           -> comment only if the newest comment (or
                                          the issue itself) is older than
                                          throttle_hours. throttle_hours 0
                                          always comments.
clear,  an open issue exists           -> comment when it recovered and how long
                                          it was firing, then close it.
                                          Skipped entirely when auto_close is
                                          false: some alarms are events, not
                                          conditions, and a missed closing line
                                          never "recovers".
clear,  no open issue                  -> nothing.

CONDITIONS VS EVENTS
--------------------
`alarm:pull-lines-stale` and `alarm:book-missing-*` are CONDITIONS: they fire
while broken and clear when fixed, so they auto-close.

`alarm:missed-close` is an EVENT. A close that was not captured is gone; there is
nothing to recover. Those issues stay open until a human closes them, and each
missed game is reported exactly once — the gate tracks which game_ids it has
already reported so its 6-hour lookback cannot re-report the same game every 15
minutes.

NEVER FAILS
-----------
Always exits 0. Alarm plumbing must not turn a successful data pull into a failed
run, and a broken `gh` must not cost a pull we already paid credits for. Issues
are the alarm channel; the run status is not.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone

DEFAULT_THROTTLE_HOURS = 24
LABEL_COLOR = "d73a4a"


def mention(assignee):
    """An @mention line, or nothing when no assignee is configured.

    Assignment alone was not enough to get a push notification through, so every
    issue body and every comment leads with an explicit @mention. The two
    mechanisms are independent: assignment shows in the issue list and filters,
    the mention is what reliably pages.
    """
    return f"@{assignee}\n\n" if assignee else ""


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Gh:
    """Thin wrapper around the `gh` CLI.

    Swapped for a stub in tests via --gh-bin, so the whole routing policy can be
    exercised without creating real issues in a real repo.
    """

    def __init__(self, binary="gh", repo=None, dry_run=False, assignee=None):
        self.binary = binary
        self.repo = repo
        self.dry_run = dry_run
        self.assignee = assignee
        self.calls = []

    def _run(self, args, stdin=None, mutating=False):
        cmd = [self.binary] + args
        if self.repo:
            cmd += ["--repo", self.repo]
        self.calls.append(cmd)
        if mutating and self.dry_run:
            print(f"[dry-run] would run: {' '.join(cmd)}", file=sys.stderr)
            return ""
        try:
            r = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
        except OSError as e:
            # gh missing or not executable. A GhError here is handled the same as
            # any other gh failure: warn, route nothing, exit 0.
            raise GhError(f"cannot execute {self.binary}: {e}")
        if r.returncode != 0:
            raise GhError(f"{' '.join(cmd[:3])} failed ({r.returncode}): "
                          f"{(r.stderr or r.stdout).strip()[:300]}")
        return r.stdout

    def open_issues(self):
        out = self._run(["issue", "list", "--state", "open", "--limit", "100",
                         "--json", "number,title,labels,createdAt"])
        try:
            return json.loads(out or "[]")
        except ValueError:
            return []

    def latest_activity(self, number, created_at):
        """When this issue was last spoken to — newest comment, else creation."""
        try:
            out = self._run(["issue", "view", str(number), "--json", "comments"])
            comments = (json.loads(out or "{}") or {}).get("comments") or []
        except (GhError, ValueError):
            comments = []
        stamps = [_parse(c.get("createdAt")) for c in comments]
        stamps = [s for s in stamps if s] or [_parse(created_at)]
        return max([s for s in stamps if s], default=None)

    def ensure_label(self, label, description=""):
        try:
            self._run(["label", "create", label, "--color", LABEL_COLOR,
                       "--description", description[:100]], mutating=True)
        except GhError:
            pass          # already exists, which is the common case

    def create_issue(self, title, body, label):
        args = ["issue", "create", "--title", title, "--label", label,
                "--body-file", "-"]
        if self.assignee:
            try:
                return self._run(args + ["--assignee", self.assignee],
                                 stdin=body, mutating=True)
            except GhError as e:
                # gh refuses an assignee who is not an assignable collaborator.
                # An un-assigned alarm still beats no alarm, so fall back rather
                # than losing the issue entirely — the body still @mentions them.
                print(f"::warning::alarm router: could not assign "
                      f"{self.assignee} ({e}); opening it unassigned",
                      file=sys.stderr)
        return self._run(args, stdin=body, mutating=True)

    def comment(self, number, body):
        return self._run(["issue", "comment", str(number), "--body-file", "-"],
                         stdin=body, mutating=True)

    def close(self, number):
        return self._run(["issue", "close", str(number)], mutating=True)


class GhError(RuntimeError):
    pass


def find_open(issues, label):
    for i in issues:
        labels = {(l.get("name") if isinstance(l, dict) else l)
                  for l in (i.get("labels") or [])}
        if label in labels:
            return i
    return None


def handle(gh, rec, issues, now):
    """Apply one alarm record. Returns a one-line description of what happened."""
    label = rec.get("label")
    state = (rec.get("state") or "firing").lower()
    title = rec.get("title") or f"[alarm] {label}"
    body = rec.get("body") or ""
    throttle = rec.get("throttle_hours", DEFAULT_THROTTLE_HOURS)
    auto_close = rec.get("auto_close", True)
    if not label:
        return "skipped: record has no label"

    existing = find_open(issues, label)

    if state == "clear":
        if not existing:
            return f"{label}: clear, nothing open"
        if not auto_close:
            return f"{label}: clear, but auto_close is off — leaving it open"
        since = _parse(existing.get("createdAt"))
        dur = (f", firing for {(now - since).total_seconds() / 3600:.1f}h"
               if since else "")
        gh.comment(existing["number"],
                   f"{mention(gh.assignee)}Recovered at {_iso(now)}{dur}."
                   f"\n\n{body}".strip())
        gh.close(existing["number"])
        return f"{label}: RECOVERED -> closed #{existing['number']}{dur}"

    # firing
    if not existing:
        gh.ensure_label(label, title)
        gh.create_issue(title,
                        f"{mention(gh.assignee)}{body}\n\n_Opened by the alarm "
                        f"router at {_iso(now)}._", label)
        return f"{label}: FIRING -> opened a new issue"

    n = existing["number"]
    if throttle:
        last = gh.latest_activity(n, existing.get("createdAt"))
        if last and now - last < timedelta(hours=throttle):
            age = (now - last).total_seconds() / 3600
            return (f"{label}: still firing, #{n} last touched {age:.1f}h ago "
                    f"(< {throttle}h) — staying quiet")
    gh.comment(n, f"{mention(gh.assignee)}Still firing at {_iso(now)}."
                  f"\n\n{body}".strip())
    return f"{label}: still firing -> commented on #{n}"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-file", required=True,
                    help="JSON list of alarm records (a missing file is fine)")
    ap.add_argument("--gh-bin", default="gh", help="the gh executable (tests stub it)")
    ap.add_argument("--repo", default=None, help="owner/name (gh infers by default)")
    ap.add_argument("--assignee", default=os.getenv("ALARM_ASSIGNEE") or None,
                    help="GitHub user to assign and @mention on every alarm "
                         "(default: $ALARM_ASSIGNEE). Without this, alarms open "
                         "issues that generate no push notification.")
    ap.add_argument("--dry-run", action="store_true",
                    help="read state but make no changes")
    ap.add_argument("--now", default=None, help="override the clock — for tests")
    args = ap.parse_args()

    now = _parse(args.now) or _now()

    try:
        with open(args.from_file) as f:
            records = json.load(f)
    except FileNotFoundError:
        print("[ok] no alarms file — nothing to route", file=sys.stderr)
        return 0
    except ValueError as e:
        print(f"::warning::alarm router: {args.from_file} is not valid JSON ({e})")
        return 0
    if not isinstance(records, list):
        print("::warning::alarm router: alarms file is not a list")
        return 0
    if not records:
        print("[ok] no alarm records", file=sys.stderr)
        return 0

    if not shutil.which(args.gh_bin):
        print(f"::warning::alarm router: `{args.gh_bin}` is not available; alarms "
              f"were detected but not routed to issues")
        return 0

    if not args.assignee:
        print("::warning::alarm router: no --assignee/$ALARM_ASSIGNEE — issues "
              "will be opened with nobody assigned and nobody @mentioned, which "
              "means no notification")
    gh = Gh(args.gh_bin, repo=args.repo, dry_run=args.dry_run,
            assignee=args.assignee)
    try:
        issues = gh.open_issues()
    except GhError as e:
        print(f"::warning::alarm router: could not list issues ({e}); alarms "
              f"were detected but not routed")
        return 0

    firing = sum(1 for r in records if (r.get("state") or "firing") == "firing")
    print(f"[ok] routing {len(records)} alarm record(s) ({firing} firing) "
          f"against {len(issues)} open issue(s)", file=sys.stderr)
    for rec in records:
        try:
            print(f"     {handle(gh, rec, issues, now)}", file=sys.stderr)
        except GhError as e:
            # One broken record must not stop the others, and must not fail the run.
            print(f"::warning::alarm router: {rec.get('label')} failed: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
