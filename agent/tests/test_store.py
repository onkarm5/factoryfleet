import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from factoryfleet_agent.sensors.base import Reading, SensorCondition
from factoryfleet_agent.store import SCHEMA_VERSION, Store

START = datetime(2026, 7, 30, 4, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)
        return self.now


@pytest.fixture
def clock():
    return MutableClock()


@pytest.fixture
def store(tmp_path, clock):
    with Store(tmp_path / "agent.sqlite", clock=clock) as opened:
        yield opened


def reading(sensor_id="vibration", condition=SensorCondition.OK, captured_at=START, **metrics):
    return Reading(
        sensor_id=sensor_id,
        captured_at=captured_at,
        metrics=metrics or {"rms_mm_s": 4.2},
        condition=condition,
    )


# --- schema ---------------------------------------------------------------------------


def test_opening_a_store_creates_the_schema_and_stamps_its_version(tmp_path):
    path = tmp_path / "nested" / "agent.sqlite"

    with Store(path):
        pass

    assert path.exists(), "the parent directory should be created on demand"
    connection = sqlite3.connect(path)
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    connection.close()

    assert {"asset_state", "outbox"} <= tables
    assert version == SCHEMA_VERSION


def test_opening_an_existing_store_is_not_destructive(tmp_path, clock):
    path = tmp_path / "agent.sqlite"
    with Store(path, clock=clock) as first:
        first.enqueue("topic", {"n": 1})

    with Store(path, clock=clock) as second:
        assert second.pending_count() == 1


# --- asset state ----------------------------------------------------------------------


def test_recording_a_reading_makes_it_readable_back(store):
    store.record(reading(rms_mm_s=4.7, peak_mm_s=11.2))

    states = store.latest()

    assert len(states) == 1
    assert states[0].sensor_id == "vibration"
    assert states[0].metrics == {"rms_mm_s": 4.7, "peak_mm_s": 11.2}
    assert states[0].condition is SensorCondition.OK
    assert states[0].captured_at == START


def test_a_new_reading_overwrites_the_previous_state_rather_than_appending(store):
    """asset_state is the machine's condition now, not its history."""
    store.record(reading(rms_mm_s=4.2))
    store.record(reading(captured_at=START + timedelta(minutes=1), rms_mm_s=9.9))

    states = store.latest()

    assert len(states) == 1
    assert states[0].metrics == {"rms_mm_s": 9.9}
    assert states[0].captured_at == START + timedelta(minutes=1)


def test_state_is_kept_per_sensor_and_ordered_by_sensor_id(store):
    store.record_all(
        [
            reading("vibration"),
            reading("temperature", bearing_c=68.0),
            reading("cycle_count", cycles_total=120),
        ]
    )

    assert [state.sensor_id for state in store.latest()] == [
        "cycle_count",
        "temperature",
        "vibration",
    ]


def test_a_failed_sensors_error_survives_the_round_trip(store):
    """Recovering only that a sensor failed, without why, would be useless on a call-out."""
    store.record(
        Reading(
            sensor_id="printer",
            captured_at=START,
            condition=SensorCondition.ERROR,
            error="RuntimeError: bus fault on channel 2",
        )
    )

    state = store.latest_for("printer")

    assert state.condition is SensorCondition.ERROR
    assert state.error == "RuntimeError: bus fault on channel 2"
    assert state.metrics == {}


def test_looking_up_an_unknown_sensor_returns_nothing(store):
    assert store.latest_for("nope") is None


def test_a_critical_condition_round_trips_as_itself(store):
    store.record(reading(condition=SensorCondition.CRITICAL, rms_mm_s=16.4))

    assert store.latest_for("vibration").condition is SensorCondition.CRITICAL


# --- outbox ---------------------------------------------------------------------------


def test_enqueue_returns_an_id_and_the_entry_comes_back_intact(store):
    entry_id = store.enqueue("factoryfleet/PLANT-A/PRESS-01/telemetry", {"readings": [1, 2]})

    pending = store.pending(10)

    assert len(pending) == 1
    assert pending[0].id == entry_id
    assert pending[0].topic == "factoryfleet/PLANT-A/PRESS-01/telemetry"
    assert pending[0].payload == {"readings": [1, 2]}
    assert pending[0].created_at == START


def test_pending_returns_oldest_first(store, clock):
    """Telemetry has to reach the cloud in the order the machine produced it."""
    for index in range(5):
        clock.advance(seconds=10)
        store.enqueue("topic", {"n": index})

    assert [entry.payload["n"] for entry in store.pending(10)] == [0, 1, 2, 3, 4]


def test_pending_respects_its_limit(store):
    for index in range(10):
        store.enqueue("topic", {"n": index})

    assert len(store.pending(3)) == 3


def test_pending_with_a_non_positive_limit_returns_nothing(store):
    store.enqueue("topic", {"n": 1})

    assert store.pending(0) == ()
    assert store.pending(-1) == ()


def test_confirmed_entries_leave_the_pending_queue(store):
    first = store.enqueue("topic", {"n": 1})
    store.enqueue("topic", {"n": 2})

    store.mark_published([first])

    assert [entry.payload["n"] for entry in store.pending(10)] == [2]
    assert store.pending_count() == 1


def test_confirming_the_same_entry_twice_changes_nothing(store):
    """A resend racing a confirmation must not corrupt the queue."""
    entry_id = store.enqueue("topic", {"n": 1})

    assert store.mark_published([entry_id]) == 1
    assert store.mark_published([entry_id]) == 0
    assert store.pending_count() == 0


def test_confirming_nothing_is_a_no_op(store):
    assert store.mark_published([]) == 0


def test_an_unconfirmed_entry_survives_a_restart(tmp_path, clock):
    """FR-4, the reason this store exists: a crash before confirmation must not lose data."""
    path = tmp_path / "agent.sqlite"
    with Store(path, clock=clock) as before:
        before.enqueue("topic", {"rms_mm_s": 4.2})
        # No mark_published: the broker never confirmed, so this is still owed.

    with Store(path, clock=clock) as after:
        pending = after.pending(10)

    assert len(pending) == 1
    assert pending[0].payload == {"rms_mm_s": 4.2}


def test_a_confirmed_entry_is_not_resent_after_a_restart(tmp_path, clock):
    path = tmp_path / "agent.sqlite"
    with Store(path, clock=clock) as before:
        entry_id = before.enqueue("topic", {"n": 1})
        before.mark_published([entry_id])

    with Store(path, clock=clock) as after:
        assert after.pending(10) == ()


# --- retention and backpressure -------------------------------------------------------


def test_confirmed_entries_are_kept_briefly_then_pruned(store, clock):
    entry_id = store.enqueue("topic", {"n": 1})
    store.mark_published([entry_id])

    # Inside the retention window: still on disk, so "what did the agent actually send?"
    # remains answerable.
    assert store.prune_published(retain_seconds=3600) == 0

    clock.advance(hours=2)

    assert store.prune_published(retain_seconds=3600) == 1


def test_pruning_never_touches_unpublished_entries(store, clock):
    store.enqueue("topic", {"n": 1})
    clock.advance(days=30)

    assert store.prune_published(retain_seconds=0) == 0
    assert store.pending_count() == 1


def test_the_outbox_drops_the_oldest_when_it_hits_its_cap(tmp_path, clock):
    """A broker down for days must not fill the machine's disk."""
    with Store(tmp_path / "agent.sqlite", max_outbox_entries=5, clock=clock) as store:
        for index in range(8):
            store.enqueue("topic", {"n": index})

        pending = store.pending(10)

        assert store.pending_count() == 5
        # The newest survive: an operator restoring a link wants the machine's state now.
        assert [entry.payload["n"] for entry in pending] == [3, 4, 5, 6, 7]
        # Loss is counted, because silent loss looks identical to a quiet healthy machine.
        assert store.dropped_entries == 3


def test_confirmed_entries_do_not_count_against_the_cap(tmp_path, clock):
    with Store(tmp_path / "agent.sqlite", max_outbox_entries=3, clock=clock) as store:
        for index in range(3):
            store.mark_published([store.enqueue("topic", {"n": index})])

        store.enqueue("topic", {"n": 99})

        assert store.dropped_entries == 0
        assert [entry.payload["n"] for entry in store.pending(10)] == [99]


# --- transactions ---------------------------------------------------------------------


def test_a_transaction_commits_state_and_outbox_together(store):
    with store.transaction():
        store.record(reading())
        store.enqueue("topic", {"n": 1})

    assert len(store.latest()) == 1
    assert store.pending_count() == 1


def test_a_failed_cycle_leaves_neither_state_nor_outbox_entry(store):
    """State claiming a reading the outbox has no record of would mislead whoever debugs it."""
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.record(reading())
            store.enqueue("topic", {"n": 1})
            raise RuntimeError("publish batch could not be built")

    assert store.latest() == ()
    assert store.pending_count() == 0


# --- serialisation and concurrency ----------------------------------------------------

def test_payloads_are_stored_compactly(store, tmp_path):
    """Whitespace in stored JSON is pure disk cost on a machine that may buffer for days."""
    store.enqueue("topic", {"readings": [{"sensorId": "vibration"}]})

    raw = sqlite3.connect(store.path).execute(
        "SELECT payload_json FROM outbox"
    ).fetchone()[0]

    assert " " not in raw
    assert json.loads(raw) == {"readings": [{"sensorId": "vibration"}]}


def test_the_sampling_and_publishing_threads_can_use_the_store_at_once(store):
    """The real access pattern: one thread enqueueing while another confirms."""
    errors: list[BaseException] = []
    confirmed: list[int] = []

    def sample():
        try:
            for index in range(100):
                with store.transaction():
                    store.record(reading("vibration", rms_mm_s=float(index)))
                    store.enqueue("topic", {"n": index})
        except BaseException as error:  # noqa: BLE001 — surfaced through `errors`
            errors.append(error)

    def publish():
        try:
            # Bounded so a failure in the sampling thread cannot leave this one spinning.
            for _ in range(100_000):
                if len(confirmed) >= 100:
                    return
                for entry in store.pending(10):
                    store.mark_published([entry.id])
                    confirmed.append(entry.id)
        except BaseException as error:  # noqa: BLE001 — surfaced through `errors`
            errors.append(error)

    threads = [
        threading.Thread(target=sample, daemon=True),
        threading.Thread(target=publish, daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert store.pending_count() == 0
    assert len(confirmed) == 100
    assert len(set(confirmed)) == 100, "an entry must not be confirmed twice"
