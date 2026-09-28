"""
test_close_gate.py — the gate's decisions, pinned with a frozen clock.

    python -m unittest test_close_gate -v

Every case injects `now` and a synthetic event list, so nothing here touches the
network or the real schedule. The important pair:

  * the gate stays SHUT when nothing is imminent        (no pull, no write)
  * the gate OPENS when a game is inside the window     (the falsification)

Only the first is easy to pass by accident — a gate that never opens satisfies it
perfectly — so both are asserted together throughout.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.close_gate import (
    PRUNE_DAYS,
    deadman_check,
    decide,
    in_horizon,
    load_state,
    save_state,
)

NOW = datetime(2026, 9, 27, 17, 0, 0, tzinfo=timezone.utc)


def ev(minutes_out, gid=None, home="Home", away="Away"):
    """An event kicking off `minutes_out` from NOW (negative = already started)."""
    k = NOW + timedelta(minutes=minutes_out)
    return {"id": gid or f"g{minutes_out}", "commence_time": k.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "home_team": home, "away_team": away}


class TestGateOpensAndShuts(unittest.TestCase):

    def test_shut_when_nothing_is_imminent(self):
        due, horizon, missed = decide([ev(200), ev(1400)], {}, NOW, 45, 15)
        self.assertEqual(due, [])
        self.assertIsNone(horizon)
        self.assertEqual(missed, [])

    def test_OPENS_when_a_game_is_inside_the_window(self):
        """The falsification: a quiet gate must not be a broken gate."""
        due, horizon, _ = decide([ev(30)], {}, NOW, 45, 15)
        self.assertEqual(len(due), 1, "a game 30 min out MUST open the gate")
        self.assertIsNotNone(horizon)

    def test_boundary_44_opens_46_does_not(self):
        self.assertEqual(len(decide([ev(44)], {}, NOW, 45, 15)[0]), 1)
        self.assertEqual(len(decide([ev(46)], {}, NOW, 45, 15)[0]), 0)

    def test_exactly_at_the_window_edge_opens(self):
        self.assertEqual(len(decide([ev(45)], {}, NOW, 45, 15)[0]), 1)

    def test_never_opens_after_kickoff(self):
        """An in-play line is not the close."""
        for mins in (-1, -30, -200):
            due, _, _ = decide([ev(mins)], {}, NOW, 45, 15)
            self.assertEqual(due, [], f"must not pull {abs(mins)} min after kickoff")

    def test_kickoff_exactly_now_does_not_open(self):
        self.assertEqual(decide([ev(0)], {}, NOW, 45, 15)[0], [])

    def test_already_sniped_game_does_not_reopen_the_gate(self):
        e = ev(30, gid="already")
        due, _, _ = decide([e], {"already": {"commence_time": e["commence_time"]}},
                           NOW, 45, 15)
        self.assertEqual(due, [], "a captured close must not be pulled twice")

    def test_an_unsniped_game_still_opens_it_alongside_a_sniped_one(self):
        a, b = ev(30, gid="done"), ev(35, gid="todo")
        due, _, _ = decide([a, b], {"done": {"commence_time": a["commence_time"]}},
                           NOW, 45, 15)
        self.assertEqual([d[0]["id"] for d in due], ["todo"])


class TestMarkingHorizon(unittest.TestCase):
    """The horizon is anchored to the earliest game that opened the gate, +grace."""

    def test_T30_and_T35_are_marked_together(self):
        events = [ev(30, gid="a"), ev(35, gid="b")]
        due, horizon, _ = decide(events, {}, NOW, 45, 15)
        self.assertEqual(len(due), 2)
        marked = {e["id"] for e in in_horizon(events, horizon, NOW)}
        self.assertEqual(marked, {"a", "b"},
                         "one pull covers both, so both must be marked")

    def test_T70_is_not_marked(self):
        events = [ev(30, gid="a"), ev(35, gid="b"), ev(70, gid="late")]
        due, horizon, _ = decide(events, {}, NOW, 45, 15)
        marked = {e["id"] for e in in_horizon(events, horizon, NOW)}
        self.assertEqual(marked, {"a", "b"})
        self.assertNotIn("late", marked,
                         "a game 70 min out has its own close later; marking it "
                         "here would silently skip that close")

    def test_horizon_is_earliest_plus_grace_not_now_plus_grace(self):
        events = [ev(40, gid="a"), ev(50, gid="b")]
        _, horizon, _ = decide(events, {}, NOW, 45, 15)
        self.assertEqual(horizon, NOW + timedelta(minutes=55))
        # b at T+50 falls inside 40+15=55, so one pull covers it
        self.assertEqual({e["id"] for e in in_horizon(events, horizon, NOW)}, {"a", "b"})

    def test_horizon_is_absolute_so_marking_later_is_identical(self):
        """--mark runs a minute or two after the decision; the set must not shift."""
        events = [ev(30, gid="a"), ev(35, gid="b"), ev(70, gid="late")]
        _, horizon, _ = decide(events, {}, NOW, 45, 15)
        later = NOW + timedelta(minutes=2)
        self.assertEqual({e["id"] for e in in_horizon(events, horizon, NOW)},
                         {e["id"] for e in in_horizon(events, horizon, later)})

    def test_a_stacked_sunday_slate_costs_one_pull(self):
        """Nine 1pm kickoffs plus two at 1:05 -> all eleven marked at once."""
        events = [ev(45, gid=f"e{i}") for i in range(9)] + \
                 [ev(50, gid="late1"), ev(50, gid="late2")]
        due, horizon, _ = decide(events, {}, NOW, 45, 15)
        self.assertEqual(len(in_horizon(events, horizon, NOW)), 11)
        # and with all of them marked, the gate shuts on the next run
        sniped = {e["id"]: {"commence_time": e["commence_time"]}
                  for e in in_horizon(events, horizon, NOW)}
        again, _, _ = decide(events, sniped, NOW + timedelta(minutes=15), 45, 15)
        self.assertEqual(again, [], "the slate must not be pulled a second time")


class TestMissedCloses(unittest.TestCase):

    def test_a_game_that_started_unsniped_is_reported(self):
        _, _, missed = decide([ev(-20, gid="oops")], {}, NOW, 45, 15)
        self.assertEqual([e["id"] for e, _ in missed], ["oops"])

    def test_a_sniped_game_that_started_is_not_reported(self):
        e = ev(-20, gid="fine")
        _, _, missed = decide([e], {"fine": {"commence_time": e["commence_time"]}},
                              NOW, 45, 15)
        self.assertEqual(missed, [])

    def test_ancient_games_are_not_reported(self):
        _, _, missed = decide([ev(-60 * 24)], {}, NOW, 45, 15)
        self.assertEqual(missed, [])


class TestDeadmanSwitch(unittest.TestCase):
    """Has the DAILY pull stopped? Reads one local file; costs nothing.

    Keyed on `_meta.last_pull['pull-lines']`, not `_meta.fetched_at`. The sniper
    writes the same file, so fetched_at only says "something pulled" — and a
    single overwritten `writer` field has the same blind spot.
    """

    def _nfl(self, td, last_pull, fetched_at="2026-09-27T16:59:00Z"):
        p = Path(td) / "nfl.json"
        p.write_text(json.dumps({"_meta": {"fetched_at": fetched_at,
                                           "last_pull": last_pull}, "games": []}))
        return p

    def _ago(self, hours):
        return (NOW - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_silent_when_the_daily_pull_is_recent(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._nfl(td, {"pull-lines": self._ago(25)})
            self.assertIsNone(deadman_check(p, NOW))

    def test_fires_when_the_daily_pull_is_stale(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._nfl(td, {"pull-lines": self._ago(27)})
            msg = deadman_check(p, NOW)
            self.assertIsNotNone(msg)
            self.assertIn("pull-lines", msg)

    def test_boundary_just_under_and_just_over(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(deadman_check(self._nfl(td, {"pull-lines": self._ago(25.9)}), NOW))
            self.assertIsNotNone(deadman_check(self._nfl(td, {"pull-lines": self._ago(26.1)}), NOW))

    def test_a_fresh_SNIPER_pull_does_not_mask_a_dead_daily_pull(self):
        """The exact case a `fetched_at` check would miss."""
        with tempfile.TemporaryDirectory() as td:
            # fetched_at is a minute old because the sniper just wrote it...
            p = self._nfl(td, {"snipe-closes": self._ago(0.01)},
                          fetched_at=self._ago(0.01))
            msg = deadman_check(p, NOW)
            self.assertIsNotNone(msg, "a fresh sniper pull must not vouch for "
                                      "pull-lines")

    def test_no_last_pull_map_is_an_alarm_not_silence(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNotNone(deadman_check(self._nfl(td, {}), NOW))

    def test_missing_file_is_an_alarm(self):
        self.assertIsNotNone(deadman_check("/nonexistent/nfl.json", NOW))

    def test_unparseable_file_is_an_alarm(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nfl.json"
            p.write_text("{not json")
            self.assertIsNotNone(deadman_check(p, NOW))

    def test_garbage_timestamp_is_an_alarm(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNotNone(
                deadman_check(self._nfl(td, {"pull-lines": "not a date"}), NOW))


class TestStateFile(unittest.TestCase):

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sniped.json"
            e = ev(30, gid="x")
            save_state(p, {"x": {"commence_time": e["commence_time"],
                                 "matchup": "A @ B", "sniped_at": "t"}}, NOW)
            self.assertEqual(list(load_state(p)), ["x"])

    def test_old_entries_are_pruned(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sniped.json"
            old = (NOW - timedelta(days=PRUNE_DAYS + 1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            new = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            kept, pruned = save_state(p, {"old": {"commence_time": old},
                                          "new": {"commence_time": new}}, NOW)
            self.assertEqual((kept, pruned), (1, 1))
            self.assertEqual(list(load_state(p)), ["new"])

    def test_missing_file_is_an_empty_ledger_not_a_crash(self):
        self.assertEqual(load_state("/nonexistent/nope.json"), {})

    def test_corrupt_file_is_an_empty_ledger_not_a_crash(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "bad.json"
            p.write_text("{not json")
            self.assertEqual(load_state(p), {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
