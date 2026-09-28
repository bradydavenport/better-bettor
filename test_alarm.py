"""
test_alarm.py — the alarm routing policy.

    python -m unittest test_alarm -v

`gh` is replaced with a fake so the whole policy is exercised without creating a
single real issue. The two rules that matter:

  CONDITIONS (`alarm:pull-lines-stale`, `alarm:book-missing-*`) fire, throttle to
  one comment a day, and auto-close on recovery.

  EVENTS (`alarm:missed-close`) fire once per new occurrence with no throttle, and
  never auto-close — a closing line that was not captured does not come back.
"""

import unittest
from datetime import datetime, timedelta, timezone

from scripts.alarm import GhError, find_open, handle

NOW = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)


class FakeGh:
    """Records what would have been done, and models open/closed state."""

    def __init__(self, issues=None):
        self.issues = issues or []
        self.next = len(self.issues) + 1
        self.created, self.comments, self.closed, self.labels = [], [], [], []

    def latest_activity(self, number, created_at):
        i = next(x for x in self.issues if x["number"] == number)
        stamps = [c["createdAt"] for c in i.get("comments", [])] or [i["createdAt"]]
        return max(stamps)

    def ensure_label(self, label, description=""):
        self.labels.append(label)

    def create_issue(self, title, body, label):
        self.issues.append({"number": self.next, "title": title,
                            "labels": [{"name": label}], "createdAt": NOW,
                            "comments": []})
        self.created.append((title, label, body))
        self.next += 1

    def comment(self, number, body):
        self.comments.append((number, body))
        next(x for x in self.issues if x["number"] == number)["comments"].append(
            {"createdAt": NOW, "body": body})

    def close(self, number):
        self.closed.append(number)


def issue(number, label, created_at, comments=()):
    return {"number": number, "title": "t", "labels": [{"name": label}],
            "createdAt": created_at,
            "comments": [{"createdAt": c, "body": "b"} for c in comments]}


def rec(label="alarm:x", state="firing", throttle=24, auto_close=True, body="b"):
    return {"label": label, "title": "[alarm] t", "body": body, "state": state,
            "throttle_hours": throttle, "auto_close": auto_close}


class TestFindOpen(unittest.TestCase):

    def test_matches_by_label(self):
        issues = [issue(1, "alarm:a", NOW), issue(2, "alarm:b", NOW)]
        self.assertEqual(find_open(issues, "alarm:b")["number"], 2)

    def test_none_when_absent(self):
        self.assertIsNone(find_open([issue(1, "alarm:a", NOW)], "alarm:z"))

    def test_tolerates_plain_string_labels(self):
        self.assertIsNotNone(find_open(
            [{"number": 1, "labels": ["alarm:a"], "createdAt": NOW}], "alarm:a"))


class TestConditions(unittest.TestCase):

    def test_firing_with_nothing_open_creates_one(self):
        gh = FakeGh()
        handle(gh, rec(), gh.issues, NOW)
        self.assertEqual(len(gh.created), 1)
        self.assertEqual(gh.labels, ["alarm:x"], "the label must be ensured first")

    def test_firing_again_inside_the_throttle_stays_quiet(self):
        gh = FakeGh([issue(1, "alarm:x", NOW - timedelta(hours=2))])
        msg = handle(gh, rec(), gh.issues, NOW)
        self.assertEqual(gh.comments, [])
        self.assertEqual(gh.created, [])
        self.assertIn("staying quiet", msg)

    def test_firing_again_past_the_throttle_comments_once(self):
        gh = FakeGh([issue(1, "alarm:x", NOW - timedelta(hours=30))])
        handle(gh, rec(), gh.issues, NOW)
        self.assertEqual(len(gh.comments), 1)
        self.assertEqual(gh.created, [], "must not open a second issue")

    def test_throttle_measures_from_the_newest_comment(self):
        gh = FakeGh([issue(1, "alarm:x", NOW - timedelta(hours=50),
                           comments=[NOW - timedelta(hours=1)])])
        handle(gh, rec(), gh.issues, NOW)
        self.assertEqual(gh.comments, [], "a recent comment must silence it")

    def test_clear_closes_with_a_recovery_comment(self):
        gh = FakeGh([issue(1, "alarm:x", NOW - timedelta(hours=5))])
        handle(gh, rec(state="clear"), gh.issues, NOW)
        self.assertEqual(gh.closed, [1])
        self.assertIn("Recovered at", gh.comments[0][1])
        self.assertIn("firing for 5.0h", gh.comments[0][1])

    def test_clear_with_nothing_open_does_nothing(self):
        gh = FakeGh()
        handle(gh, rec(state="clear"), gh.issues, NOW)
        self.assertEqual((gh.created, gh.comments, gh.closed), ([], [], []))

    def test_a_second_trip_after_closing_opens_a_new_issue(self):
        gh = FakeGh([issue(1, "alarm:x", NOW - timedelta(hours=5))])
        handle(gh, rec(state="clear"), gh.issues, NOW)
        gh.issues = []                      # closed issues are not "open"
        handle(gh, rec(), gh.issues, NOW + timedelta(days=7))
        self.assertEqual(len(gh.created), 1, "a fresh trip must page again")


class TestEvents(unittest.TestCase):
    """Missed closes: no throttle, never auto-closed."""

    def test_zero_throttle_always_comments(self):
        gh = FakeGh([issue(1, "alarm:missed-close", NOW - timedelta(minutes=1))])
        handle(gh, rec("alarm:missed-close", throttle=0, auto_close=False),
               gh.issues, NOW)
        self.assertEqual(len(gh.comments), 1,
                         "a newly missed game must report immediately")

    def test_clear_does_not_close_when_auto_close_is_off(self):
        gh = FakeGh([issue(1, "alarm:missed-close", NOW - timedelta(hours=8))])
        msg = handle(gh, rec("alarm:missed-close", state="clear", auto_close=False),
                     gh.issues, NOW)
        self.assertEqual(gh.closed, [], "a missed close never recovers")
        self.assertEqual(gh.comments, [])
        self.assertIn("auto_close is off", msg)


class TestRobustness(unittest.TestCase):

    def test_a_record_with_no_label_is_skipped(self):
        gh = FakeGh()
        msg = handle(gh, {"state": "firing"}, gh.issues, NOW)
        self.assertIn("no label", msg)
        self.assertEqual(gh.created, [])

    def test_unknown_state_is_treated_as_firing(self):
        gh = FakeGh()
        handle(gh, rec(state="weird"), gh.issues, NOW)
        self.assertEqual(len(gh.created), 1)

    def test_defaults_apply_when_fields_are_missing(self):
        gh = FakeGh([issue(1, "alarm:x", NOW - timedelta(hours=2))])
        handle(gh, {"label": "alarm:x", "state": "firing"}, gh.issues, NOW)
        self.assertEqual(gh.comments, [], "default throttle is 24h")


if __name__ == "__main__":
    unittest.main(verbosity=2)
