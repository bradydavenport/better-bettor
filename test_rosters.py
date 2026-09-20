"""
test_rosters.py — team-abbreviation normalization and the null-safety rules.

stdlib unittest only, so it runs anywhere the fetcher does and adds no deps:

    python -m unittest test_rosters -v

The point of the abbreviation tests is not that norm_team() works in the
abstract — it is that data/rosters.json and data/nfl.json keep joining. Both
upstreams disagree with nfl.json in exactly one place (nflverse writes LA for
the Rams, ESPN writes WSH for Washington), and those are the two rows that would
silently blank a team if the map regressed.
"""

import unittest

from fetch_rosters import (
    CANON_TEAMS,
    TEAM_ALIASES,
    build_teams,
    carry_updated_at,
    norm_team,
    week_for,
)


class TestTeamNormalization(unittest.TestCase):

    def test_canonical_abbrs_are_identity(self):
        for abbr in CANON_TEAMS:
            self.assertEqual(norm_team(abbr), abbr)

    def test_there_are_thirty_two_teams(self):
        self.assertEqual(len(CANON_TEAMS), 32)

    def test_live_mismatches(self):
        """The two that actually differ in today's feeds."""
        self.assertEqual(norm_team("LA"), "LAR")    # nflverse -> Rams
        self.assertEqual(norm_team("WSH"), "WAS")   # ESPN -> Washington

    def test_known_variant_spellings(self):
        cases = {
            "JAC": "JAX", "AZ": "ARI", "ARZ": "ARI", "STL": "LAR", "WFT": "WAS",
            "GNB": "GB", "KAN": "KC", "NWE": "NE", "NOR": "NO", "SFO": "SF",
            "TAM": "TB", "LVR": "LV", "OAK": "LV", "SD": "LAC", "CLV": "CLE",
            "BLT": "BAL", "HST": "HOU",
        }
        for raw, want in cases.items():
            self.assertEqual(norm_team(raw), want, f"{raw} should map to {want}")

    def test_case_and_whitespace_insensitive(self):
        self.assertEqual(norm_team(" wsh "), "WAS")
        self.assertEqual(norm_team("lar"), "LAR")

    def test_unknown_returns_none_rather_than_guessing(self):
        for junk in ("", None, "XXX", "Philadelphia Eagles", "PH"):
            self.assertIsNone(norm_team(junk), f"{junk!r} should not map")

    def test_every_alias_resolves_to_a_canonical_team(self):
        for raw, mapped in TEAM_ALIASES.items():
            self.assertIn(mapped, CANON_TEAMS, f"alias {raw} -> {mapped} is not canonical")

    def test_no_alias_shadows_a_canonical_abbr(self):
        """A canonical key must never be rewritten to a different team."""
        for raw in TEAM_ALIASES:
            if raw in CANON_TEAMS:
                self.assertEqual(TEAM_ALIASES[raw], raw, f"{raw} is canonical but remapped")

    def test_matches_the_abbreviations_in_nfl_json(self):
        """The join guarantee: same set fetch_lines.py writes into data/nfl.json."""
        try:
            from fetch_lines import NFL_ABBR
        except ImportError as e:      # missing optional dep in a bare checkout
            self.skipTest(f"fetch_lines not importable: {e}")
        self.assertEqual(set(NFL_ABBR.values()), set(CANON_TEAMS))


class TestQbChangeSemantics(unittest.TestCase):
    """null and false are different answers; only one of them is safe to guess."""

    SNAP = "2026-09-19T11:56:08Z"

    def _build(self, qbs, prior, injuries=None):
        return build_teams(qbs, self.SNAP, prior, injuries or {})

    def test_change_detected(self):
        teams = self._build({"ATL": "Michael Penix Jr."}, {"ATL": "Tua Tagovailoa"})
        qb = teams["ATL"]["qb"]
        self.assertTrue(qb["changed_from_last_week"])
        self.assertEqual(qb["previous_name"], "Tua Tagovailoa")

    def test_no_change(self):
        teams = self._build({"PHI": "Jalen Hurts"}, {"PHI": "Jalen Hurts"})
        qb = teams["PHI"]["qb"]
        self.assertFalse(qb["changed_from_last_week"])
        self.assertIsNone(qb["previous_name"])

    def test_no_prior_snapshot_is_null_not_false(self):
        teams = self._build({"PHI": "Jalen Hurts"}, {})
        self.assertIsNone(teams["PHI"]["qb"]["changed_from_last_week"])

    def test_team_absent_from_prior_snapshot_is_not_a_change(self):
        """An expansion/rename gap must not read as a QB switch."""
        teams = self._build({"PHI": "Jalen Hurts"}, {"DAL": "Dak Prescott"})
        self.assertFalse(teams["PHI"]["qb"]["changed_from_last_week"])

    def test_missing_qb_is_null_not_omitted(self):
        teams = self._build({}, {})
        self.assertIsNone(teams["PHI"]["qb"])
        self.assertIn("PHI", teams)

    def test_all_teams_always_present(self):
        teams = self._build({"PHI": "Jalen Hurts"}, {})
        self.assertEqual(set(teams), set(CANON_TEAMS))

    def test_qb_status_comes_from_the_injury_report(self):
        injuries = {"ATL": {"out": [], "doubtful": [], "ir": [],
                            "status": {"Tua Tagovailoa": "Doubtful"}}}
        teams = self._build({"ATL": "Tua Tagovailoa"}, {"ATL": "Tua Tagovailoa"}, injuries)
        self.assertEqual(teams["ATL"]["qb"]["status"], "Doubtful")

    def test_qb_status_null_when_not_on_the_report(self):
        teams = self._build({"PHI": "Jalen Hurts"}, {"PHI": "Jalen Hurts"})
        self.assertIsNone(teams["PHI"]["qb"]["status"])


class TestUpdatedAtCarryForward(unittest.TestCase):
    """updated_at must mean 'when this team last moved', not 'when we last ran'."""

    OLD, NOW = "2026-09-16T12:00:00Z", "2026-09-19T12:00:00Z"

    def _block(self, qb_name):
        return {"qb": {"name": qb_name, "changed_from_last_week": False,
                       "previous_name": None, "status": None, "as_of": "x"},
                "out": [], "doubtful": [], "ir": []}

    def test_unchanged_team_keeps_its_old_stamp(self):
        old = {"PHI": dict(self._block("Jalen Hurts"), updated_at=self.OLD)}
        new = {"PHI": self._block("Jalen Hurts")}
        teams, changed = carry_updated_at(old, new, self.NOW)
        self.assertEqual(teams["PHI"]["updated_at"], self.OLD)
        self.assertEqual(changed, [])

    def test_changed_team_gets_a_new_stamp(self):
        old = {"ATL": dict(self._block("Tua Tagovailoa"), updated_at=self.OLD)}
        new = {"ATL": self._block("Michael Penix Jr.")}
        teams, changed = carry_updated_at(old, new, self.NOW)
        self.assertEqual(teams["ATL"]["updated_at"], self.NOW)
        self.assertEqual(changed, ["ATL"])

    def test_a_new_absence_counts_as_a_change(self):
        old = {"PHI": dict(self._block("Jalen Hurts"), updated_at=self.OLD)}
        new = {"PHI": self._block("Jalen Hurts")}
        new["PHI"]["out"] = [{"name": "A Player", "pos": "CB",
                              "reason": "Hamstring", "as_of": self.NOW}]
        teams, changed = carry_updated_at(old, new, self.NOW)
        self.assertEqual(changed, ["PHI"])

    def test_first_write_stamps_everything_without_reporting_churn(self):
        new = {"PHI": self._block("Jalen Hurts")}
        teams, changed = carry_updated_at({}, new, self.NOW)
        self.assertEqual(teams["PHI"]["updated_at"], self.NOW)
        self.assertEqual(changed, [])


class TestWeekLookup(unittest.TestCase):

    def setUp(self):
        from datetime import datetime, timezone

        def d(s):
            return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)

        self.d = d
        self.weeks = [
            (1, d("2026-09-06T07:00:00"), d("2026-09-16T06:59:00")),
            (2, d("2026-09-16T07:00:00"), d("2026-09-23T06:59:00")),
            (3, d("2026-09-23T07:00:00"), d("2026-09-30T06:59:00")),
        ]

    def test_date_inside_a_week(self):
        self.assertEqual(week_for(self.d("2026-09-20T17:00:00"), self.weeks), 2)

    def test_boundary_belongs_to_the_later_week(self):
        self.assertEqual(week_for(self.d("2026-09-23T07:00:00"), self.weeks), 3)

    def test_outside_the_season_is_none(self):
        self.assertIsNone(week_for(self.d("2026-08-01T00:00:00"), self.weeks))

    def test_none_input_is_none(self):
        self.assertIsNone(week_for(None, self.weeks))


if __name__ == "__main__":
    unittest.main(verbosity=2)
