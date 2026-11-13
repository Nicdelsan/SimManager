# ADR 0002 — SQLite through aiosqlite and hand-written SQL

- Status: accepted (M0)

## Context

SimManager needs a local durable store for an append-only event log, jobs, runs and, from
M2, long time series (one CONVERGE `.out` file can hold ~7 M values; an OpenFOAM log
~350k timesteps). The maintainer is not a database specialist and wants readable code.

## Decision

- SQLite file in the user data directory, WAL mode, `foreign_keys=ON`.
- `aiosqlite` (async wrapper over the stdlib driver) and plain SQL; no ORM.
- Numbered migration files `NNNN_name.sql`, applied once each inside a transaction and
  recorded in `schema_migrations`.
- One connection per process; writes serialized by an `asyncio.Lock`.
- The `events` table is append-only, enforced by `BEFORE UPDATE/DELETE` triggers.
- Time series (M2) will be stored in chunks (packed `array('d')` blobs) with downsampled
  levels, not one row per sample.

## Alternatives

SQLAlchemy 2 (Core or ORM) would mainly help a future Postgres backend; the `storage`
contract already isolates consumers from the implementation, so a `storage-postgres` plugin
can be written later without touching them.

## Consequences

- Schema and queries are visible in plain SQL files.
- Adding a column means adding a migration file; there is no autogeneration.
