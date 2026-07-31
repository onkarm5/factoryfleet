"""Local SQLite store: latest sensor state, and the outbox of everything owed to the cloud.

This is the agent's durability boundary. Plant network links drop, brokers restart, and the
machine itself loses power mid-shift; none of that may cost a reading. So a reading is
committed here first and only acknowledged as sent once the broker confirms it. The publisher
never holds telemetry in memory alone.

Two tables:

``asset_state``
    One row per sensor, overwritten each cycle — the machine's current condition, which is
    what a ``get_status`` command answers from without waiting for the next sample.

``outbox``
    Append-only queue of payloads owed to the broker, in order. A row is written before any
    publish is attempted and marked published only on confirmation, which makes delivery
    at-least-once: a crash between publish and confirmation resends rather than drops.

Concurrency: the sampling thread writes while the publishing thread reads and confirms, so
one connection is shared behind a re-entrant lock. Writes here are small and a few per
minute, so a lock costs nothing and avoids a connection pool that would only add ways for
the two threads to deadlock on SQLite's own locking.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Iterator, Mapping, Sequence

from factoryfleet_agent.sensors.base import Reading, SensorCondition
from factoryfleet_agent.timeutil import Clock, iso, parse_iso, utc_now

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS asset_state (
    sensor_id    TEXT PRIMARY KEY,
    condition    TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    error        TEXT,
    captured_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    topic        TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    published_at TEXT
);

-- The publisher's only query is "oldest unpublished first", and the pruner's is "confirmed
-- and old". Both are served by this one index.
CREATE INDEX IF NOT EXISTS outbox_published_at_id ON outbox (published_at, id);
"""


@dataclass(frozen=True)
class OutboxEntry:
    """One payload owed to the broker."""

    id: int
    topic: str
    payload: Mapping[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class StoredState:
    """A sensor's last known reading, as recovered from disk."""

    sensor_id: str
    condition: SensorCondition
    metrics: Mapping[str, float]
    captured_at: datetime
    error: str | None = None


class Store:
    """SQLite-backed state and outbox.

    ``max_outbox_entries`` bounds how much undelivered telemetry is kept while the broker is
    unreachable. Past the cap the oldest unpublished entries are dropped, because for
    telemetry the newest readings are the ones worth having: an operator restoring a link
    after two days wants to know how the machine is now, not what it was doing on Tuesday.
    Dropping is counted and logged rather than silent — silent loss here would look exactly
    like a healthy quiet machine.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        max_outbox_entries: int = 10_000,
        clock: Clock = utc_now,
    ) -> None:
        self._path = Path(path)
        self._max_outbox_entries = max_outbox_entries
        self._clock = clock
        self._lock = RLock()
        self._dropped_entries = 0

        if self._path.parent and str(self._path) != ":memory:":
            self._path.parent.mkdir(parents=True, exist_ok=True)

        self._connection = sqlite3.connect(
            self._path,
            # The publisher and sampler threads share this connection behind _lock.
            check_same_thread=False,
            isolation_level=None,  # explicit transactions, see _transaction
        )
        self._connection.row_factory = sqlite3.Row
        self._configure()

    # --- lifecycle --------------------------------------------------------------------

    def _configure(self) -> None:
        with self._lock:
            # WAL lets the publisher read while the sampler writes, instead of the two
            # blocking each other every cycle.
            self._connection.execute("PRAGMA journal_mode=WAL")
            # A plant machine can lose power without warning. FULL costs one fsync per
            # commit, which at a few commits a minute is irrelevant next to losing the
            # readings that justify the agent existing.
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.executescript(SCHEMA)
            self._connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @property
    def path(self) -> Path:
        return self._path

    @property
    def dropped_entries(self) -> int:
        """Unpublished entries discarded because the outbox hit its cap."""
        return self._dropped_entries

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Groups writes so a sampling cycle lands whole or not at all.

        The runner records state and enqueues the batch inside one of these: state claiming
        a reading was taken while the outbox has no record of it would be a lie told to
        whoever debugs the machine later.
        """
        with self._lock:
            # IMMEDIATE takes the write lock up front rather than discovering the conflict
            # partway through and having to unwind.
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
            self._connection.execute("COMMIT")

    # --- asset state ------------------------------------------------------------------

    def record(self, reading: Reading) -> None:
        """Overwrites a sensor's current state."""
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO asset_state (sensor_id, condition, metrics_json, error, captured_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(sensor_id) DO UPDATE SET
                    condition    = excluded.condition,
                    metrics_json = excluded.metrics_json,
                    error        = excluded.error,
                    captured_at  = excluded.captured_at
                """,
                (
                    reading.sensor_id,
                    reading.condition.value,
                    json.dumps(dict(reading.metrics), separators=(",", ":")),
                    reading.error,
                    iso(reading.captured_at),
                ),
            )

    def record_all(self, readings: Iterable[Reading]) -> None:
        with self._lock:
            for reading in readings:
                self.record(reading)

    def latest(self) -> tuple[StoredState, ...]:
        """Every sensor's last known state, ordered by sensor id."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT sensor_id, condition, metrics_json, error, captured_at "
                "FROM asset_state ORDER BY sensor_id"
            ).fetchall()
        return tuple(_to_state(row) for row in rows)

    def latest_for(self, sensor_id: str) -> StoredState | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT sensor_id, condition, metrics_json, error, captured_at "
                "FROM asset_state WHERE sensor_id = ?",
                (sensor_id,),
            ).fetchone()
        return _to_state(row) if row is not None else None

    # --- outbox -----------------------------------------------------------------------

    def enqueue(self, topic: str, payload: Mapping[str, Any]) -> int:
        """Queues a payload for publication, returning its outbox id."""
        with self._lock:
            cursor = self._connection.execute(
                "INSERT INTO outbox (topic, payload_json, created_at) VALUES (?, ?, ?)",
                (
                    topic,
                    json.dumps(payload, separators=(",", ":")),
                    iso(self._clock()),
                ),
            )
            self._enforce_cap()
            return int(cursor.lastrowid)

    def pending(self, limit: int) -> tuple[OutboxEntry, ...]:
        """Oldest unpublished entries first, so telemetry arrives in the order it happened."""
        if limit <= 0:
            return ()
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, topic, payload_json, created_at FROM outbox "
                "WHERE published_at IS NULL ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(
            OutboxEntry(
                id=row["id"],
                topic=row["topic"],
                payload=json.loads(row["payload_json"]),
                created_at=parse_iso(row["created_at"]),
            )
            for row in rows
        )

    def pending_count(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS pending FROM outbox WHERE published_at IS NULL"
            ).fetchone()
        return int(row["pending"])

    def outbox_size(self) -> int:
        """All outbox rows, including confirmed ones not yet pruned — what is on disk."""
        with self._lock:
            row = self._connection.execute("SELECT COUNT(*) AS total FROM outbox").fetchone()
        return int(row["total"])

    def mark_published(self, entry_ids: Sequence[int]) -> int:
        """Confirms delivery. Only ever called once the broker has acknowledged."""
        if not entry_ids:
            return 0
        placeholders = ",".join("?" for _ in entry_ids)
        with self._lock:
            cursor = self._connection.execute(
                f"UPDATE outbox SET published_at = ? "  # noqa: S608 — placeholders are counted, not interpolated
                f"WHERE id IN ({placeholders}) AND published_at IS NULL",
                (iso(self._clock()), *entry_ids),
            )
            return cursor.rowcount

    def prune_published(self, retain_seconds: float = 3600.0) -> int:
        """Deletes confirmed entries older than the retention window.

        Confirmed rows are kept briefly rather than deleted on the spot: when telemetry looks
        wrong in the cloud, the first question is what the agent actually sent, and an empty
        table cannot answer it. The window bounds the file size.
        """
        cutoff = self._clock().timestamp() - retain_seconds
        with self._lock:
            rows = self._connection.execute(
                "SELECT id, published_at FROM outbox WHERE published_at IS NOT NULL"
            ).fetchall()
            stale = [
                row["id"]
                for row in rows
                if parse_iso(row["published_at"]).timestamp() <= cutoff
            ]
            if not stale:
                return 0
            placeholders = ",".join("?" for _ in stale)
            cursor = self._connection.execute(
                f"DELETE FROM outbox WHERE id IN ({placeholders})",  # noqa: S608
                stale,
            )
            return cursor.rowcount

    def _enforce_cap(self) -> None:
        """Drops the oldest unpublished entries once the outbox exceeds its cap."""
        row = self._connection.execute(
            "SELECT COUNT(*) AS pending FROM outbox WHERE published_at IS NULL"
        ).fetchone()
        excess = int(row["pending"]) - self._max_outbox_entries
        if excess <= 0:
            return
        cursor = self._connection.execute(
            "DELETE FROM outbox WHERE id IN ("
            "  SELECT id FROM outbox WHERE published_at IS NULL ORDER BY id LIMIT ?"
            ")",
            (excess,),
        )
        self._dropped_entries += cursor.rowcount


def _to_state(row: sqlite3.Row) -> StoredState:
    return StoredState(
        sensor_id=row["sensor_id"],
        condition=SensorCondition(row["condition"]),
        metrics=json.loads(row["metrics_json"]),
        captured_at=parse_iso(row["captured_at"]),
        error=row["error"],
    )
