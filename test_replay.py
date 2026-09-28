"""
test_replay.py — replaying one run's outputs onto a moved origin.

    python -m unittest test_replay -v

The rule that matters: `nfl.json` and `usage.json` are NEWEST-WINS, not
ours-wins. A slow run replaying onto a newer origin must not roll the file back —
while its append-only outputs (ledger rows, sniped ids) still have to land,
because those are observations nothing supersedes.
"""

import json
import tempfile
import unittest
from pathlib import Path

from scripts.replay_outputs import (
    _merge_last_pull,
    replay_ids,
    replay_newest,
    replay_rows,
)


def row(fetched_at, writer, book="draftkings", gid="g1"):
    return json.dumps({"fetched_at": fetched_at, "last_update": "x",
                       "game_id": gid, "commence_time": "c", "home": "H",
                       "away": "A", "book": book, "market": "h2h",
                       "outcome": "A", "price": -110, "point": None,
                       "source": "live", "writer": writer})


class TestRows(unittest.TestCase):

    def test_rows_are_appended_to_what_is_already_there(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "history.jsonl"
            dest.write_text(row("2026-09-28T09:00:00Z", "pull-lines") + "\n")
            ours = Path(td) / "new.jsonl"
            ours.write_text(row("2026-09-28T10:00:00Z", "snipe-closes") + "\n")
            n, _ = replay_rows(ours, dest)
            self.assertEqual(n, 1)
            lines = dest.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            writers = {json.loads(l)["writer"] for l in lines}
            self.assertEqual(writers, {"pull-lines", "snipe-closes"})

    def test_already_present_batch_is_not_duplicated(self):
        """The push landed but reported failure."""
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "history.jsonl"
            mine = row("2026-09-28T10:00:00Z", "snipe-closes")
            dest.write_text(mine + "\n")
            ours = Path(td) / "new.jsonl"
            ours.write_text(mine + "\n")
            n, msg = replay_rows(ours, dest)
            self.assertEqual(n, 0)
            self.assertEqual(len(dest.read_text().splitlines()), 1)

    def test_same_timestamp_different_writer_is_NOT_a_duplicate(self):
        """Two writers can pull in the same second; fetched_at alone is not identity."""
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "history.jsonl"
            dest.write_text(row("2026-09-28T10:00:00Z", "pull-lines") + "\n")
            ours = Path(td) / "new.jsonl"
            ours.write_text(row("2026-09-28T10:00:00Z", "snipe-closes") + "\n")
            n, _ = replay_rows(ours, dest)
            self.assertEqual(n, 1, "a different writer must still be appended")
            self.assertEqual(len(dest.read_text().splitlines()), 2)

    def test_missing_destination_is_created(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "sub" / "history.jsonl"
            ours = Path(td) / "new.jsonl"
            ours.write_text(row("t", "w") + "\n")
            n, _ = replay_rows(ours, dest)
            self.assertEqual(n, 1)


class TestNewestWins(unittest.TestCase):

    def _write(self, path, fetched_at, games, last_pull):
        Path(path).write_text(json.dumps({
            "_meta": {"fetched_at": fetched_at, "last_pull": last_pull},
            "games": games}))

    def test_ours_newer_ours_survives(self):
        with tempfile.TemporaryDirectory() as td:
            ours, dest = Path(td) / "ours.json", Path(td) / "nfl.json"
            self._write(ours, "2026-09-28T10:00:00Z", [{"game_id": "mine"}],
                        {"snipe-closes": "2026-09-28T10:00:00Z"})
            self._write(dest, "2026-09-28T09:00:00Z", [{"game_id": "theirs"}],
                        {"pull-lines": "2026-09-28T09:00:00Z"})
            replay_newest(ours, dest, "fetched_at")
            got = json.loads(dest.read_text())
            self.assertEqual(got["_meta"]["fetched_at"], "2026-09-28T10:00:00Z")
            self.assertEqual([g["game_id"] for g in got["games"]], ["mine"])

    def test_origin_newer_origin_survives_no_rollback(self):
        """The bug this rule exists for."""
        with tempfile.TemporaryDirectory() as td:
            ours, dest = Path(td) / "ours.json", Path(td) / "nfl.json"
            self._write(ours, "2026-09-28T09:00:00Z", [{"game_id": "stale"}],
                        {"snipe-closes": "2026-09-28T09:00:00Z"})
            self._write(dest, "2026-09-28T11:00:00Z", [{"game_id": "fresh"}],
                        {"pull-lines": "2026-09-28T11:00:00Z"})
            replay_newest(ours, dest, "fetched_at")
            got = json.loads(dest.read_text())
            self.assertEqual(got["_meta"]["fetched_at"], "2026-09-28T11:00:00Z")
            self.assertEqual([g["game_id"] for g in got["games"]], ["fresh"],
                             "origin's newer games must not be rolled back")

    def test_the_losing_file_still_contributes_its_last_pull_entry(self):
        with tempfile.TemporaryDirectory() as td:
            ours, dest = Path(td) / "ours.json", Path(td) / "nfl.json"
            self._write(ours, "2026-09-28T09:00:00Z", [],
                        {"snipe-closes": "2026-09-28T09:00:00Z"})
            self._write(dest, "2026-09-28T11:00:00Z", [],
                        {"pull-lines": "2026-09-28T11:00:00Z"})
            replay_newest(ours, dest, "fetched_at")
            lp = json.loads(dest.read_text())["_meta"]["last_pull"]
            self.assertEqual(lp, {"pull-lines": "2026-09-28T11:00:00Z",
                                  "snipe-closes": "2026-09-28T09:00:00Z"},
                             "a losing file must still record that its writer ran")

    def test_missing_destination_takes_ours(self):
        with tempfile.TemporaryDirectory() as td:
            ours, dest = Path(td) / "ours.json", Path(td) / "nfl.json"
            self._write(ours, "2026-09-28T09:00:00Z", [{"game_id": "x"}], {})
            replay_newest(ours, dest, "fetched_at")
            self.assertEqual(json.loads(dest.read_text())["_meta"]["fetched_at"],
                             "2026-09-28T09:00:00Z")


class TestMergeLastPull(unittest.TestCase):

    def test_max_per_key(self):
        self.assertEqual(
            _merge_last_pull({"a": "2026-01-02", "b": "2026-01-01"},
                             {"a": "2026-01-01", "c": "2026-01-03"}),
            {"a": "2026-01-02", "b": "2026-01-01", "c": "2026-01-03"})

    def test_handles_missing_and_malformed(self):
        self.assertEqual(_merge_last_pull(None, {"a": "t"}), {"a": "t"})
        self.assertEqual(_merge_last_pull({"a": 5}, {"a": "t"}), {"a": "t"})
        self.assertEqual(_merge_last_pull({}, {}), {})


class TestIds(unittest.TestCase):

    def test_union_not_replace(self):
        with tempfile.TemporaryDirectory() as td:
            ours, dest = Path(td) / "ids.json", Path(td) / "sniped.json"
            ours.write_text(json.dumps({"g1": {"commence_time": "c",
                                               "sniped_at": "2026-09-28T10:00:00Z"}}))
            dest.write_text(json.dumps({"_meta": {}, "sniped": {
                "g9": {"commence_time": "c", "sniped_at": "2026-09-28T09:00:00Z"}}}))
            added, _ = replay_ids(ours, dest)
            self.assertEqual(added, 1)
            self.assertEqual(set(json.loads(dest.read_text())["sniped"]), {"g1", "g9"})

    def test_earlier_capture_wins_on_collision(self):
        with tempfile.TemporaryDirectory() as td:
            ours, dest = Path(td) / "ids.json", Path(td) / "sniped.json"
            ours.write_text(json.dumps({"g1": {"sniped_at": "2026-09-28T08:00:00Z"}}))
            dest.write_text(json.dumps({"_meta": {}, "sniped": {
                "g1": {"sniped_at": "2026-09-28T10:00:00Z"}}}))
            replay_ids(ours, dest)
            self.assertEqual(json.loads(dest.read_text())["sniped"]["g1"]["sniped_at"],
                             "2026-09-28T08:00:00Z")

    def test_missing_destination_is_created(self):
        with tempfile.TemporaryDirectory() as td:
            ours, dest = Path(td) / "ids.json", Path(td) / "sniped.json"
            ours.write_text(json.dumps({"g1": {"sniped_at": "t"}}))
            added, _ = replay_ids(ours, dest)
            self.assertEqual(added, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
