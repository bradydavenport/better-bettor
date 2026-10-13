"""
test_plan_pull.py — market selection and the double-pull guard.

    python -m unittest test_plan_pull -v

The case that matters most is a DISPATCHED Tuesday run. The old rule compared
`github.event.schedule` against a cron string; that field is empty on
workflow_dispatch, so once the doorbell took over, Tuesday would have quietly
stopped pulling totals on every run while still reporting success.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.plan_pull import (
    DRIFT_MARKETS,
    OPENER_MARKETS,
    choose_markets,
    last_pull_at,
    should_skip,
)

# 2026-10-13 is a Tuesday; 2026-10-14 a Wednesday.
TUE = datetime(2026, 10, 13, 13, 7, 0, tzinfo=timezone.utc)
WED = datetime(2026, 10, 14, 13, 7, 0, tzinfo=timezone.utc)


class TestMarketSelection(unittest.TestCase):

    def test_DISPATCHED_tuesday_gets_all_three_markets(self):
        """The regression. A dispatch carries no schedule, only a clock."""
        markets, why = choose_markets("", TUE)
        self.assertEqual(markets, OPENER_MARKETS)
        self.assertIn("opener", why)

    def test_dispatched_tuesday_with_the_auto_default(self):
        """A bare {"ref":"main"} dispatch receives the declared default, `auto`."""
        self.assertEqual(choose_markets("auto", TUE)[0], OPENER_MARKETS)
        self.assertEqual(choose_markets("AUTO", TUE)[0], OPENER_MARKETS)

    def test_dispatched_non_tuesday_gets_the_drift_pull(self):
        self.assertEqual(choose_markets("", WED)[0], DRIFT_MARKETS)
        self.assertEqual(choose_markets("auto", WED)[0], DRIFT_MARKETS)

    def test_every_other_day_of_the_week_is_a_drift_pull(self):
        for d in range(7):
            day = TUE + timedelta(days=d)
            want = OPENER_MARKETS if day.isoweekday() == 2 else DRIFT_MARKETS
            self.assertEqual(choose_markets("auto", day)[0], want,
                             f"{day:%A} should be {want}")

    def test_exactly_one_day_a_week_is_the_opener(self):
        got = [choose_markets("auto", TUE + timedelta(days=d))[0] for d in range(7)]
        self.assertEqual(got.count(OPENER_MARKETS), 1)

    def test_explicit_override_wins_on_any_day(self):
        for day in (TUE, WED):
            self.assertEqual(choose_markets("total", day)[0], "total")
            self.assertEqual(choose_markets("ml", day)[0], "ml")
            self.assertEqual(choose_markets("ml,spread", day)[0], "ml,spread")

    def test_override_is_whitespace_tolerant(self):
        self.assertEqual(choose_markets("  spread  ", WED)[0], "spread")

    def test_a_schedule_run_behaves_identically_to_a_dispatch(self):
        """Same clock, same answer, regardless of how the run was triggered —
        which is the entire point of dropping github.event.schedule."""
        self.assertEqual(choose_markets("", TUE), choose_markets("auto", TUE))


class TestDoublePullGuard(unittest.TestCase):
    """The doorbell and the backup schedule both firing must not double-spend."""

    def _nfl(self, td, last=None):
        p = Path(td) / "nfl.json"
        meta = {"fetched_at": "x"}
        if last:
            meta["last_pull"] = {"pull-lines": last.strftime("%Y-%m-%dT%H:%M:%SZ")}
        p.write_text(json.dumps({"_meta": meta, "games": []}))
        return p

    def test_skips_when_already_pulled_an_hour_ago(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._nfl(td, TUE - timedelta(hours=1))
            skip, why = should_skip(p, TUE, 12, "")
            self.assertTrue(skip)
            self.assertIn("already pulled", why)

    def test_runs_when_the_last_pull_is_old(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._nfl(td, TUE - timedelta(hours=20))
            self.assertFalse(should_skip(p, TUE, 12, "")[0])

    def test_boundary(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(should_skip(self._nfl(td, TUE - timedelta(hours=11.9)),
                                        TUE, 12, "")[0])
            self.assertFalse(should_skip(self._nfl(td, TUE - timedelta(hours=12.1)),
                                         TUE, 12, "")[0])

    def test_an_explicit_override_is_never_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._nfl(td, TUE - timedelta(minutes=1))
            skip, why = should_skip(p, TUE, 12, "all")
            self.assertFalse(skip, "a deliberate manual pull must always run")
            self.assertIn("override", why)

    def test_auto_is_not_an_override(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._nfl(td, TUE - timedelta(minutes=1))
            self.assertTrue(should_skip(p, TUE, 12, "auto")[0])

    def test_no_previous_pull_recorded_means_run(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(should_skip(self._nfl(td), TUE, 12, "")[0])

    def test_missing_file_means_run_rather_than_skip(self):
        """Failing open is right here: skipping on a read error would silently
        stop pulling altogether."""
        self.assertFalse(should_skip("/nonexistent/nfl.json", TUE, 12, "")[0])

    def test_unparseable_file_means_run(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "nfl.json"
            p.write_text("{not json")
            self.assertFalse(should_skip(p, TUE, 12, "")[0])

    def test_guard_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._nfl(td, TUE - timedelta(minutes=1))
            self.assertFalse(should_skip(p, TUE, 0, "")[0])

    def test_last_pull_at_reads_the_per_writer_map(self):
        with tempfile.TemporaryDirectory() as td:
            p = self._nfl(td, TUE - timedelta(hours=3))
            self.assertEqual(last_pull_at(p), TUE - timedelta(hours=3))
            self.assertIsNone(last_pull_at(p, writer="snipe-closes"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
