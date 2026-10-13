-- better-bettor odds ledger
--
-- ODDS DATA ONLY. This database is backed up weekly to GitHub Release assets on
-- a PUBLIC repository, so every row here is effectively world-readable. Never
-- add bets, stakes, bankroll, account identifiers or anything else that would be
-- harmful in public. scripts/db_backup.py enforces this with a table allowlist
-- and refuses to publish a dump containing anything outside it.
--
-- Multi-sport by construction (sports table, sport_id on events). Note that
-- INGESTING other sports is blocked by Odds API credits, not by this schema:
-- measured NFL burn is ~159 credits/month, so three sports at the same cadence
-- is ~476 against a 500/month free cap.

CREATE TABLE IF NOT EXISTS sports (
  id     smallserial PRIMARY KEY,
  key    text NOT NULL UNIQUE,            -- 'americanfootball_nfl'
  title  text
);

CREATE TABLE IF NOT EXISTS books (
  id          smallserial PRIMARY KEY,
  key         text NOT NULL UNIQUE,       -- 'pinnacle', 'draftkings', ...
  title       text,
  -- kalshi/novig are peer-to-peer exchanges whose prices almost certainly
  -- exclude platform fees, so they are not directly comparable to a book's
  -- vig-inclusive line. A FLAG ONLY: nothing adjusts a price anywhere.
  is_exchange boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS markets (
  id  smallserial PRIMARY KEY,
  key text NOT NULL UNIQUE                -- h2h | spreads | totals
);

CREATE TABLE IF NOT EXISTS events (
  id              serial PRIMARY KEY,
  sport_id        smallint NOT NULL REFERENCES sports(id),
  source_event_id text     NOT NULL,      -- The Odds API event id
  commence_time   timestamptz NOT NULL,
  home_team       text NOT NULL,
  away_team       text NOT NULL,
  UNIQUE (sport_id, source_event_id)
);
CREATE INDEX IF NOT EXISTS events_commence ON events (commence_time);

-- One row per quote series. The surrogate key keeps `observations` narrow,
-- which matters because that is the only table that grows without bound.
CREATE TABLE IF NOT EXISTS outcomes (
  id           serial PRIMARY KEY,
  event_id     int      NOT NULL REFERENCES events(id),
  market_id    smallint NOT NULL REFERENCES markets(id),
  book_id      smallint NOT NULL REFERENCES books(id),
  outcome_name text     NOT NULL,         -- team name, or 'Over' / 'Under'
  UNIQUE (event_id, market_id, book_id, outcome_name)
);

CREATE TABLE IF NOT EXISTS pulls (
  id                serial PRIMARY KEY,
  -- Stable across retries: "<run_id>:<attempt>:<iteration>:<writer>". A retried
  -- step is then a no-op instead of a second observation of the same moment.
  pull_key          text NOT NULL UNIQUE,
  fetched_at        timestamptz NOT NULL,
  writer            text NOT NULL,        -- pull-lines | snipe-closes | snipe-test | git_backfill
  sport_id          smallint REFERENCES sports(id),
  markets_requested text[] NOT NULL DEFAULT '{}',
  source            text NOT NULL DEFAULT 'live'
);
CREATE INDEX IF NOT EXISTS pulls_fetched ON pulls (sport_id, fetched_at DESC);
CREATE INDEX IF NOT EXISTS pulls_writer  ON pulls (writer, fetched_at DESC);

-- EVERY observation from EVERY pull, not only the ones where the price moved.
--
-- Change-only storage was designed first and rejected for two reasons that have
-- nothing to do with size. (1) Parity against the JSONL ledger can never pass,
-- because a book restamping `last_update` without moving its price produces no
-- row at all. (2) Change detection has to read "the previous row in this
-- series", and pull-lines and snipe-closes write concurrently: a transaction for
-- t1 committing after one for t2 compares against the wrong predecessor and
-- silently corrupts the reconstructed series. Storing every observation has
-- neither problem, and measured only ~26% more bytes.
--
-- A row existing for a pull IS the record that the book offered that market on
-- that pull. Absence means not offered. That is why there is no coverage table.
CREATE TABLE IF NOT EXISTS observations (
  outcome_id  int NOT NULL REFERENCES outcomes(id),
  pull_id     int NOT NULL REFERENCES pulls(id),
  -- denormalized from pulls.fetched_at: 8 bytes to avoid a join on every
  -- as-of query, which is the hottest read path there is here
  observed_at timestamptz NOT NULL,
  price       int NOT NULL,               -- American odds
  point       numeric(5,1),               -- NULL for h2h
  last_update timestamptz,                -- the book's own market-level stamp
  PRIMARY KEY (outcome_id, pull_id)
);
-- The PK is (outcome_id, pull_id) because pull_id is identity by construction,
-- whereas (outcome_id, observed_at) would break if two pulls ever shared a
-- fetched_at. That means as-of reads need their own index:
CREATE INDEX IF NOT EXISTS observations_asof
  ON observations (outcome_id, observed_at DESC);
