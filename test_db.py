"""
test_db.py — the pure logic around the Postgres ledger.

    python -m unittest test_db -v

Everything that needs a live server is verified separately against a real local
Postgres (see the branch report); these cover the parts that can go wrong without
one — the writer resolution the migration depends on, and the identity projection
that makes verification meaningful.
"""

import unittest
from datetime import datetime, timezone

from scripts.db import EXCHANGE_BOOKS, TABLES, UNKNOWN_WRITER, database_url, resolve_writer
from scripts.migrate_jsonl_to_db import _iso, group_into_pulls, identity


def row(**kw):
    base = {"fetched_at": "2026-10-13T19:50:15Z", "game_id": "g1", "book": "pinnacle",
            "market": "h2h", "outcome": "Chicago Bears", "price": -110, "point": None,
            "last_update": "2026-10-13T19:49:00Z", "source": "live",
            "writer": "pull-lines"}
    base.update(kw)
    return base


class TestWriterResolution(unittest.TestCase):
    """The one transformation the migration performs, so it gets its own tests."""

    def test_a_tagged_row_keeps_its_writer(self):
        self.assertEqual(resolve_writer(row(writer="snipe-closes")), "snipe-closes")

    def test_backfill_recovers_its_writer_from_source(self):
        """1,056 committed backfill rows predate the field but say what they are."""
        r = row(source="git_backfill")
        del r["writer"]
        self.assertEqual(resolve_writer(r), "git_backfill")

    def test_legacy_live_rows_are_unknown_not_invented(self):
        """596 live rows carry no writer information at all. Calling them
        'manual' would be fabrication."""
        r = row(source="live")
        del r["writer"]
        self.assertEqual(resolve_writer(r), UNKNOWN_WRITER)

    def test_a_null_writer_is_treated_as_absent(self):
        self.assertEqual(resolve_writer(row(writer=None, source="live")),
                         UNKNOWN_WRITER)

    def test_snipe_test_is_preserved_so_it_stays_out_of_clv(self):
        self.assertEqual(resolve_writer(row(writer="snipe-test")), "snipe-test")


class TestIdentity(unittest.TestCase):
    """What verification compares. A field missing here is a field that could
    drift unnoticed."""

    def test_identity_includes_last_update(self):
        a = identity(row(last_update="2026-10-13T19:49:00Z"))
        b = identity(row(last_update="2026-10-13T19:49:30Z"))
        self.assertNotEqual(a, b, "last_update must be compared — it is the field "
                                  "change-only storage could never reproduce")

    def test_identity_distinguishes_price(self):
        self.assertNotEqual(identity(row(price=-110)), identity(row(price=-111)))

    def test_identity_distinguishes_point(self):
        self.assertNotEqual(identity(row(point=-3.5)), identity(row(point=3.5)))

    def test_point_compares_equal_across_float_and_string(self):
        """Postgres returns numeric; the JSONL holds a float."""
        self.assertEqual(identity(row(point=-3.5)), identity(row(point="-3.5")))

    def test_datetime_and_string_stamps_compare_equal(self):
        dt = datetime(2026, 10, 13, 19, 50, 15, tzinfo=timezone.utc)
        self.assertEqual(identity(row(fetched_at=dt)),
                         identity(row(fetched_at="2026-10-13T19:50:15Z")))

    def test_a_non_utc_datetime_normalizes_before_formatting(self):
        """The bug that made the parity check unable to fail: a Postgres
        datetime in the server's local zone formatted as-is never matched."""
        from datetime import timedelta
        local = datetime(2026, 10, 13, 13, 50, 15,
                         tzinfo=timezone(timedelta(hours=-6)))
        self.assertEqual(_iso(local), "2026-10-13T19:50:15Z")

    def test_identity_includes_writer(self):
        self.assertNotEqual(identity(row(writer="pull-lines")),
                            identity(row(writer="snipe-closes")))


class TestGrouping(unittest.TestCase):

    def test_rows_group_into_one_pull_per_writer_and_stamp(self):
        rows = [row(), row(outcome="Green Bay Packers"),
                row(fetched_at="2026-10-13T20:00:00Z"),
                row(writer="snipe-closes")]
        g = group_into_pulls(rows)
        self.assertEqual(len(g), 3)
        self.assertEqual(len(g[("pull-lines", "2026-10-13T19:50:15Z")]), 2)

    def test_untagged_rows_group_under_their_resolved_writer(self):
        r = row(source="git_backfill")
        del r["writer"]
        g = group_into_pulls([r])
        self.assertEqual(list(g)[0][0], "git_backfill")


class TestGuards(unittest.TestCase):

    def test_exchange_books(self):
        self.assertEqual(EXCHANGE_BOOKS, frozenset({"kalshi", "novig"}))

    def test_the_backup_allowlist_matches_the_schema(self):
        """db.TABLES is what the public-release guard checks against, so it must
        not drift from db/schema.sql."""
        import re
        from pathlib import Path
        sql = (Path(__file__).parent / "db" / "schema.sql").read_text()
        declared = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sql))
        self.assertEqual(declared, set(TABLES))

    def test_no_database_url_returns_none_rather_than_raising(self):
        import os
        saved = os.environ.pop("DATABASE_URL", None)
        try:
            self.assertIsNone(database_url())
        finally:
            if saved:
                os.environ["DATABASE_URL"] = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
