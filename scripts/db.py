"""
db.py — the Postgres side of the odds ledger.

ONE ingestion path, used by both the live dual-write in fetch_lines.py and the
one-off JSONL migration. That is deliberate: if the migration had its own writer,
parity between the two would be a coincidence rather than a property.

CONNECTIONS
-----------
Always the DIRECT (non-pooled) Neon URL — the hostname WITHOUT `-pooler`. Every
use here is on the direct side of Neon's own guidance: schema migrations,
pg_dump, and short-lived single-transaction writes. Pooling buys nothing for one
connection held for a second, and PgBouncer's transaction mode would break
psycopg3's prepared statements (`prepared statement "_pg3_0" already exists`) and
silently drop session state. connect() warns if it is handed a pooled URL.

IDEMPOTENCY
-----------
A whole pull is one transaction: the `pulls` row, every `outcomes` row it needed
and every `observations` row commit together or not at all. There is no partial
state to repair, which is what the JSONL replay machinery existed for.

On top of that, every insert is conflict-safe:
  pulls         UNIQUE (pull_key)              -- a retried step is a no-op
  events        UNIQUE (sport_id, source_event_id)
  outcomes      UNIQUE (event_id, market_id, book_id, outcome_name)
  observations  PRIMARY KEY (outcome_id, pull_id)
So re-running an identical write changes nothing, and a retry with a NEW
pull_key is honestly a new observation rather than a duplicate.
"""

import os
import sys
from datetime import datetime, timezone

DEFAULT_SPORT = "americanfootball_nfl"
EXCHANGE_BOOKS = frozenset({"kalshi", "novig"})

# Kept in lockstep with db/schema.sql. scripts/db_backup.py refuses to publish a
# dump containing any table outside this list, because the backups land on a
# PUBLIC repo: it is the mechanical guard against a future `bets` table leaking.
TABLES = ("sports", "books", "markets", "events", "outcomes", "pulls",
          "observations")


def _psycopg():
    try:
        import psycopg
        return psycopg
    except ImportError:
        sys.exit("psycopg is not installed — add 'psycopg[binary]' to requirements.txt")


def database_url(url=None):
    u = url or os.getenv("DATABASE_URL")
    if not u:
        return None
    if "-pooler." in u:
        print("::warning::DATABASE_URL points at a POOLED endpoint (-pooler). "
              "Migrations, pg_dump and session state need the direct URL; "
              "prepared statements can fail under PgBouncer transaction mode.",
              file=sys.stderr)
    return u


def connect(url=None, autocommit=False):
    """A direct connection, or None when no DATABASE_URL is configured."""
    u = database_url(url)
    if not u:
        return None
    return _psycopg().connect(u, autocommit=autocommit)


def apply_schema(conn, path=None):
    """Apply db/schema.sql. Idempotent — every statement is IF NOT EXISTS."""
    from pathlib import Path
    p = Path(path or Path(__file__).resolve().parent.parent / "db" / "schema.sql")
    with conn.cursor() as cur:
        cur.execute(p.read_text())
    conn.commit()


# The ONE declared transformation the migration performs. 596 live rows predate
# the `writer` field entirely and carry no writer information, so they cannot be
# tagged faithfully — inventing "manual" for them would be fabrication. They
# become 'unknown'. Backfill rows also predate the field but carry
# source='git_backfill', from which the writer IS recoverable, so they are
# recovered rather than lumped in.
#
# The migration asserts the 'unknown' count separately, so this mapping is a
# checked claim rather than a normalization that hides itself.
UNKNOWN_WRITER = "unknown"


def pull_key(writer, fetched_at):
    """The identity of a pull: who pulled, and when.

    Derived from the pull itself rather than from the run that wrote it, so the
    live dual-write and the migration produce the SAME key for the same pull and
    whichever arrives second is a no-op. A run-scoped key (run id + attempt)
    looks like better retry protection but is not: a retried step re-fetches, so
    it gets a new fetched_at and is honestly a new observation either way — while
    the run-scoped name would let the migration insert the same moment twice
    under a different key.
    """
    ts = fetched_at
    if hasattr(ts, "strftime"):
        ts = ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{writer}:{ts}"


def resolve_writer(row):
    """A row's writer, recovered from `source` where possible, else 'unknown'."""
    w = row.get("writer")
    if w:
        return w
    src = row.get("source")
    if src and src != "live":
        return src          # 'git_backfill' rows identify themselves
    return UNKNOWN_WRITER


def _parse(s):
    if not s:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Writer:
    """Caches reference ids so a pull of ~1400 rows does not re-query per row."""

    def __init__(self, conn):
        self.conn = conn
        self._sports, self._books, self._markets = {}, {}, {}
        self._events, self._outcomes = {}, {}

    # ON CONFLICT DO UPDATE rather than DO NOTHING: DO NOTHING returns no row,
    # so RETURNING id would come back empty on the second call.
    def sport_id(self, key):
        if key not in self._sports:
            with self.conn.cursor() as cur:
                cur.execute("INSERT INTO sports (key) VALUES (%s) "
                            "ON CONFLICT (key) DO UPDATE SET key = EXCLUDED.key "
                            "RETURNING id", (key,))
                self._sports[key] = cur.fetchone()[0]
        return self._sports[key]

    def book_id(self, key):
        if key not in self._books:
            with self.conn.cursor() as cur:
                cur.execute("INSERT INTO books (key, is_exchange) VALUES (%s, %s) "
                            "ON CONFLICT (key) DO UPDATE SET "
                            "is_exchange = EXCLUDED.is_exchange RETURNING id",
                            (key, key in EXCHANGE_BOOKS))
                self._books[key] = cur.fetchone()[0]
        return self._books[key]

    def market_id(self, key):
        if key not in self._markets:
            with self.conn.cursor() as cur:
                cur.execute("INSERT INTO markets (key) VALUES (%s) "
                            "ON CONFLICT (key) DO UPDATE SET key = EXCLUDED.key "
                            "RETURNING id", (key,))
                self._markets[key] = cur.fetchone()[0]
        return self._markets[key]

    def event_id(self, sport_key, source_event_id, commence_time, home, away):
        k = (sport_key, source_event_id)
        if k not in self._events:
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO events (sport_id, source_event_id, commence_time,"
                    " home_team, away_team) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT (sport_id, source_event_id) DO UPDATE SET "
                    " commence_time = EXCLUDED.commence_time "
                    "RETURNING id",
                    (self.sport_id(sport_key), source_event_id,
                     _parse(commence_time), home, away))
                self._events[k] = cur.fetchone()[0]
        return self._events[k]

    def outcome_id(self, event_id, market_key, book_key, outcome_name):
        k = (event_id, market_key, book_key, outcome_name)
        if k not in self._outcomes:
            with self.conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO outcomes (event_id, market_id, book_id, outcome_name)"
                    " VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (event_id, market_id, book_id, outcome_name) "
                    "DO UPDATE SET outcome_name = EXCLUDED.outcome_name "
                    "RETURNING id",
                    (event_id, self.market_id(market_key),
                     self.book_id(book_key), outcome_name))
                self._outcomes[k] = cur.fetchone()[0]
        return self._outcomes[k]

    def write_pull(self, rows, pull_key, sport_key=DEFAULT_SPORT,
                   markets_requested=None, commit=True):
        """Write one pull's worth of history rows in a single transaction.

        `rows` are history-ledger dicts — exactly the shape fetch_lines.py
        appends to data/history.jsonl — so the live writer and the migration
        share this code rather than agreeing by accident.

        Returns (pull_id, observations_inserted). An already-written pull_key
        returns its existing id and 0.
        """
        if not rows:
            return None, 0
        fetched_at = _parse(rows[0].get("fetched_at"))
        writer = resolve_writer(rows[0])
        source = rows[0].get("source") or "live"
        mk = markets_requested or sorted({r.get("market") for r in rows if r.get("market")})

        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pulls (pull_key, fetched_at, writer, sport_id,"
                " markets_requested, source) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (pull_key) DO NOTHING RETURNING id",
                (pull_key, fetched_at, writer, self.sport_id(sport_key),
                 list(mk), source))
            got = cur.fetchone()
            if got is None:
                cur.execute("SELECT id FROM pulls WHERE pull_key = %s", (pull_key,))
                return cur.fetchone()[0], 0
            pull_id = got[0]

            batch = []
            for r in rows:
                eid = self.event_id(sport_key, r.get("game_id"),
                                    r.get("commence_time"), r.get("home"), r.get("away"))
                oid = self.outcome_id(eid, r.get("market"), r.get("book"),
                                      r.get("outcome"))
                batch.append((oid, pull_id, fetched_at, r.get("price"),
                              r.get("point"), _parse(r.get("last_update"))))
            cur.executemany(
                "INSERT INTO observations (outcome_id, pull_id, observed_at,"
                " price, point, last_update) VALUES (%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (outcome_id, pull_id) DO NOTHING", batch)
        if commit:
            self.conn.commit()
        return pull_id, len(batch)


def reconstruct_rows(conn, writer=None):
    """Every observation back out in history-ledger shape.

    This is what migration verification compares against the source JSONL. It
    reads through the same joins a consumer would, so a verification pass is
    also a smoke test of the schema's readability.
    """
    sql = """
      SELECT p.fetched_at, o_ev.source_event_id, b.key, m.key, oc.outcome_name,
             ob.price, ob.point, ob.last_update, p.writer, p.source,
             o_ev.home_team, o_ev.away_team, o_ev.commence_time
      FROM observations ob
      JOIN outcomes oc ON oc.id = ob.outcome_id
      JOIN events o_ev ON o_ev.id = oc.event_id
      JOIN books   b   ON b.id  = oc.book_id
      JOIN markets m   ON m.id  = oc.market_id
      JOIN pulls   p   ON p.id  = ob.pull_id
    """
    params = ()
    if writer:
        sql += " WHERE p.writer = %s"
        params = (writer,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        for (fetched_at, gid, book, market, outcome, price, point,
             last_update, wr, src, home, away, commence) in cur:
            yield {
                "fetched_at": fetched_at, "game_id": gid, "book": book,
                "market": market, "outcome": outcome, "price": price,
                "point": None if point is None else float(point),
                "last_update": last_update, "writer": wr, "source": src,
                "home": home, "away": away, "commence_time": commence,
            }
