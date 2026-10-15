"""
test_opener.py — the event-gated line opener.

    python -m unittest test_opener -v

Two properties carry this feature, and both have a falsification test:

  * it fires after the LAST Sunday kickoff + 4h, not after the early or late
    slates (otherwise it is just a worse cron)
  * it fires even though /events has by then DROPPED those games — measured
    2026-10-15: zero of 28 events had a past kickoff, and games our own ledger
    recorded days earlier were gone. A gate that recomputed at fire time would
    never fire at all.

Real Week 7 kickoff times throughout (ET): 09:30 international, 13:00 early,
16:05 / 16:25 late, 20:20 SNF.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from scripts.close_gate import (
    ET_ZONE,
    OPENER_AFTER_H,
    iso_week_key,
    load_opener,
    opener_due,
    plan_openers,
    save_state,
    sunday_slates,
)

ET = ZoneInfo("America/New_York")


def et(y, mo, d, h, mi=0):
    """An aware UTC datetime from an ET wall-clock time."""
    return datetime(y, mo, d, h, mi, tzinfo=ET).astimezone(timezone.utc)


def ev(dt, gid=None):
    return {"id": gid or f"g{dt:%m%d%H%M}",
            "commence_time": dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "home_team": "Home", "away_team": "Away"}


# Week 7, 2026: Sunday 2026-10-18 (EDT)
WEEK7 = [ev(et(2026, 10, 18, 9, 30)), ev(et(2026, 10, 18, 13, 0)),
         ev(et(2026, 10, 18, 16, 5)), ev(et(2026, 10, 18, 16, 25)),
         ev(et(2026, 10, 18, 20, 20), gid="snf")]
SNF = et(2026, 10, 18, 20, 20)
FIRE = SNF + timedelta(hours=4)          # Mon 00:20 ET = 04:20Z
WED = et(2026, 10, 14, 20, 0)            # a run well before Sunday


class TestWeekKey(unittest.TestCase):

    def test_sunday_and_the_monday_it_fires_on_are_different_iso_weeks(self):
        """The reason the key is the Sunday's week, not now's."""
        self.assertEqual(iso_week_key(datetime(2026, 10, 18).date()), "2026-W42")
        self.assertEqual(iso_week_key(datetime(2026, 10, 19).date()), "2026-W43")


class TestSundayGrouping(unittest.TestCase):

    def test_snf_groups_under_sunday_even_though_it_is_monday_in_utc(self):
        slates = sunday_slates(WEEK7)
        self.assertEqual(list(slates), [datetime(2026, 10, 18).date()])
        self.assertEqual(len(slates[datetime(2026, 10, 18).date()]), 5)
        self.assertEqual(SNF.strftime("%H:%MZ"), "00:20Z",
                         "SNF really is Monday in UTC — that is the trap")

    def test_a_thursday_or_monday_game_is_not_a_sunday_slate(self):
        self.assertEqual(sunday_slates([ev(et(2026, 10, 15, 20, 15)),
                                        ev(et(2026, 10, 19, 20, 15))]), {})


class TestFiresOnlyAfterTheLastSundayGame(unittest.TestCase):
    """The falsification you asked for."""

    def setUp(self):
        self.opener, _ = plan_openers(WEEK7, WED, {})

    def _due_at(self, when):
        return opener_due(self.opener, when)[0]

    def test_does_NOT_fire_after_the_international_slate(self):
        self.assertIsNone(self._due_at(et(2026, 10, 18, 13, 30)))

    def test_does_NOT_fire_after_the_early_slate(self):
        self.assertIsNone(self._due_at(et(2026, 10, 18, 17, 0)))

    def test_does_NOT_fire_after_the_late_slate(self):
        self.assertIsNone(self._due_at(et(2026, 10, 18, 20, 25)))

    def test_does_NOT_fire_while_snf_is_still_being_played(self):
        self.assertIsNone(self._due_at(et(2026, 10, 18, 23, 0)))

    def test_FIRES_four_hours_after_snf(self):
        self.assertEqual(self._due_at(FIRE), "2026-W42")

    def test_the_boundary(self):
        self.assertIsNone(self._due_at(FIRE - timedelta(minutes=1)))
        self.assertEqual(self._due_at(FIRE + timedelta(minutes=1)), "2026-W42")

    def test_fires_EXACTLY_ONCE_across_many_ticks(self):
        """The doorbell runs every 10 minutes; only one of them may pull."""
        fired = []
        opener = self.opener
        for i in range(36):                      # six hours of ticks
            t = FIRE + timedelta(minutes=10 * i)
            week, _ = opener_due(opener, t)
            if week:
                fired.append((t, week))
                opener = dict(opener)
                opener[week] = {**opener[week], "triggered_at": t.isoformat()}
        self.assertEqual(len(fired), 1, f"fired {len(fired)} times: {fired}")
        self.assertEqual(fired[0][1], "2026-W42")


class TestSurvivesEventsDroppingFinishedGames(unittest.TestCase):
    """/events drops a game once it finishes — measured, not hypothetical."""

    def test_fires_after_the_sunday_games_vanish_from_events(self):
        opener, changed = plan_openers(WEEK7, WED, {})
        self.assertTrue(changed)
        saved_fire = opener["2026-W42"]["fire_at"]

        # Now /events returns ONLY future games: Sunday's are gone.
        after = [ev(et(2026, 10, 22, 20, 15)), ev(et(2026, 10, 25, 13, 0))]
        self.assertEqual(sunday_slates(after).get(datetime(2026, 10, 18).date()), None)

        opener2, _ = plan_openers(after, FIRE, opener)
        self.assertEqual(opener2["2026-W42"]["fire_at"], saved_fire,
                         "the saved fire_at must survive the games disappearing")
        self.assertEqual(opener_due(opener2, FIRE)[0], "2026-W42")

    def test_a_week_never_seen_before_its_sunday_cannot_fire(self):
        """Honest limitation: with no run before kickoff there is nothing to
        compute from, and the Monday 13:07 cron is the backstop."""
        after = [ev(et(2026, 10, 22, 20, 15))]
        opener, _ = plan_openers(after, FIRE, {})
        self.assertEqual(opener_due(opener, FIRE), (None, None))

    def test_plan_refuses_to_revise_a_week_whose_last_kickoff_has_passed(self):
        opener, _ = plan_openers(WEEK7, WED, {})
        tampered = [ev(et(2026, 10, 18, 13, 0))]          # as if SNF never existed
        opener2, changed = plan_openers(tampered, FIRE, opener)
        self.assertFalse(changed)
        self.assertEqual(opener2["2026-W42"]["fire_at"], opener["2026-W42"]["fire_at"])


class TestFlexedAndMissingGames(unittest.TestCase):

    def test_a_flexed_snf_moves_the_fire_time_with_no_code_change(self):
        flexed = WEEK7[:-1] + [ev(et(2026, 10, 18, 20, 50), gid="snf")]
        opener, _ = plan_openers(flexed, WED, {})
        self.assertEqual(opener["2026-W42"]["fire_at"],
                         (et(2026, 10, 18, 20, 50) + timedelta(hours=4))
                         .strftime("%Y-%m-%dT%H:%M:%SZ"))

    def test_a_week_with_no_sunday_night_game_fires_after_the_late_slate(self):
        no_snf = WEEK7[:-1]                       # latest is 16:25 ET
        opener, _ = plan_openers(no_snf, WED, {})
        want = et(2026, 10, 18, 16, 25) + timedelta(hours=4)
        self.assertEqual(opener_due(opener, want)[0], "2026-W42")
        self.assertIsNone(opener_due(opener, want - timedelta(minutes=1))[0])

    def test_a_week_with_no_sunday_games_at_all_never_fires(self):
        opener, changed = plan_openers(
            [ev(et(2026, 10, 17, 13, 0)), ev(et(2026, 10, 19, 20, 15))], WED, {})
        self.assertEqual(opener, {})
        self.assertFalse(changed)

    def test_two_visible_sundays_are_planned_independently(self):
        two = WEEK7 + [ev(et(2026, 10, 25, 20, 20), gid="snf2")]
        opener, _ = plan_openers(two, WED, {})
        self.assertEqual(sorted(opener), ["2026-W42", "2026-W43"])


class TestDaylightSaving(unittest.TestCase):
    """zoneinfo, not a fixed offset."""

    def test_the_zone_is_a_named_zone_not_an_offset(self):
        self.assertEqual(ET_ZONE, "America/New_York")

    def test_autumn_transition_week(self):
        """2026-11-01 is the EDT->EST switch, at 02:00 ET that Sunday morning —
        before kickoff, so the games are EST."""
        games = [ev(et(2026, 11, 1, 13, 0)), ev(et(2026, 11, 1, 20, 20))]
        snf = et(2026, 11, 1, 20, 20)
        self.assertEqual(snf.strftime("%H:%MZ"), "01:20Z",
                         "EST puts SNF an hour later in UTC than EDT does")
        opener, _ = plan_openers(games, et(2026, 10, 28, 12, 0), {})
        self.assertEqual(opener_due(opener, snf + timedelta(hours=4))[0], "2026-W44")
        self.assertIsNone(
            opener_due(opener, snf + timedelta(hours=3, minutes=59))[0])

    def test_spring_transition(self):
        """NFL is out of season in March, but the code must not care."""
        games = [ev(et(2027, 3, 14, 20, 20))]
        snf = et(2027, 3, 14, 20, 20)
        opener, _ = plan_openers(games, et(2027, 3, 10, 12, 0), {})
        want = iso_week_key(snf.astimezone(ET).date())      # 2027-W10
        self.assertEqual(opener_due(opener, snf + timedelta(hours=4))[0], want)
        self.assertEqual(want, "2027-W10")

    def test_the_ET_LOCAL_answer_is_identical_across_both_offsets(self):
        """The property that matters: 20:20 ET + 4h is 00:20 ET, in either
        offset, even though the UTC instants differ by an hour."""
        for y, mo, d in ((2026, 10, 18), (2026, 11, 1)):
            snf = et(y, mo, d, 20, 20)
            fire = snf + timedelta(hours=4)
            self.assertEqual(f"{fire.astimezone(ET):%H:%M}", "00:20")

    def test_a_fixed_offset_misreads_the_local_hour_but_not_the_weekday(self):
        """Honest scope of what zoneinfo buys here.

        A fixed -4h offset reads the EST SNF instant as 21:20 rather than 20:20 —
        the wall-clock hour is wrong. It does NOT flip the weekday: 01:20Z Monday
        minus four hours is still Sunday. So for NFL's kickoff times a fixed
        offset would not have broken the Sunday grouping; zoneinfo is correct by
        construction rather than fixing an observed bug. It WOULD matter for a
        kickoff near ET midnight, which is why the code should stay on zoneinfo
        if other sports are ever added.
        """
        snf_est = et(2026, 11, 1, 20, 20)                 # 01:20Z Monday
        fixed = snf_est.astimezone(timezone(timedelta(hours=-4)))
        self.assertEqual(f"{fixed:%H:%M}", "21:20", "fixed offset: wrong hour")
        self.assertEqual(f"{snf_est.astimezone(ET):%H:%M}", "20:20", "zoneinfo: right")
        self.assertEqual(fixed.weekday(), 6)
        self.assertEqual(snf_est.astimezone(ET).weekday(), 6)


class TestStatePersistence(unittest.TestCase):

    def test_opener_round_trips_and_does_not_disturb_sniped(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sniped.json"
            sniped = {"g1": {"commence_time": _iso_soon(), "matchup": "A @ B"}}
            opener, _ = plan_openers(WEEK7, WED, {})
            save_state(p, sniped, WED, missed={}, opener=opener)
            doc = json.loads(p.read_text())
            self.assertEqual(sorted(doc["opener"]), ["2026-W42"])
            self.assertEqual(list(doc["sniped"]), ["g1"],
                             "recording an opener must not touch sniped games")
            self.assertEqual(sorted(load_opener(p)), ["2026-W42"])

    def test_saving_one_map_does_not_erase_the_others(self):
        """The bug this feature exposed. Every close snipe called
        save_state(path, sniped, now), which wiped BOTH the missed-close record
        and the opener's saved fire_at - so the opener could never fire."""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            opener, _ = plan_openers(WEEK7, WED, {})
            save_state(p, {"g1": {"commence_time": _iso_soon()}}, WED,
                       missed={"g9": {"commence_time": _iso_soon(),
                                      "matchup": "X @ Y"}},
                       opener=opener)
            # now save ONLY sniped, exactly as the --mark path does
            save_state(p, {"g1": {"commence_time": _iso_soon()},
                           "g2": {"commence_time": _iso_soon()}}, WED)
            doc = json.loads(p.read_text())
            self.assertEqual(sorted(doc["sniped"]), ["g1", "g2"])
            self.assertEqual(list(doc["missed"]), ["g9"],
                             "a close snipe must not erase a reported miss")
            self.assertEqual(sorted(doc["opener"]), ["2026-W42"],
                             "a close snipe must not erase the opener fire_at")

    def test_saving_only_missed_keeps_the_opener(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            opener, _ = plan_openers(WEEK7, WED, {})
            save_state(p, {}, WED, opener=opener)
            save_state(p, now=WED, missed={"g9": {"commence_time": _iso_soon()}})
            self.assertEqual(sorted(load_opener(p)), ["2026-W42"])

    def test_triggered_weeks_are_never_replanned(self):
        opener, _ = plan_openers(WEEK7, WED, {})
        opener["2026-W42"]["triggered_at"] = "x"
        opener2, changed = plan_openers(WEEK7, WED, opener)
        self.assertFalse(changed)

    def test_old_openers_are_pruned(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "s.json"
            old = {"2020-W01": {"fire_at": "2020-01-06T00:00:00Z"}}
            save_state(p, {}, WED, missed={}, opener=old)
            self.assertEqual(load_opener(p), {})

    def test_the_offset_constant(self):
        self.assertEqual(OPENER_AFTER_H, 4)


def _iso_soon():
    return (WED + timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    unittest.main(verbosity=2)
