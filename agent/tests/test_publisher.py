from datetime import datetime, timedelta, timezone

import pytest

from factoryfleet_agent.store import Store
from factoryfleet_agent.transport.publisher import Publisher

START = datetime(2026, 7, 30, 4, 0, tzinfo=timezone.utc)


class MutableClock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)
        return self.now


class FakeBroker:
    """A broker that can be offline, can refuse specific messages, and records what it took."""

    def __init__(self, connected=True):
        self.connected = connected
        self.sent: list[tuple[str, dict]] = []
        self.refuse_after: int | None = None
        self.refuse_topics: set[str] = set()

    def is_connected(self):
        return self.connected

    def publish(self, topic, payload):
        if not self.connected:
            return False
        if topic in self.refuse_topics:
            return False
        if self.refuse_after is not None and len(self.sent) >= self.refuse_after:
            return False
        self.sent.append((topic, payload))
        return True


@pytest.fixture
def clock():
    return MutableClock()


@pytest.fixture
def store(tmp_path, clock):
    with Store(tmp_path / "agent.sqlite", clock=clock) as opened:
        yield opened


@pytest.fixture
def broker():
    return FakeBroker()


def publisher(store, broker, clock, **kwargs):
    return Publisher(store, broker, clock=clock, **kwargs)


def queue_batches(store, clock, count, topic="factoryfleet/PLANT-A/PRESS-01/telemetry"):
    ids = []
    for index in range(count):
        clock.advance(seconds=10)
        ids.append(store.enqueue(topic, {"assetId": "PRESS-01", "batchedAt": "x", "n": index}))
    return ids


# --- draining -------------------------------------------------------------------------


def test_draining_sends_pending_entries_and_confirms_them(store, broker, clock):
    queue_batches(store, clock, 3)

    confirmed = publisher(store, broker, clock).drain_once()

    assert confirmed == 3
    assert len(broker.sent) == 3
    assert store.pending_count() == 0


def test_entries_are_sent_oldest_first(store, broker, clock):
    """Readings must reach the cloud in the order the machine produced them."""
    queue_batches(store, clock, 5)

    publisher(store, broker, clock).drain_once()

    assert [payload["n"] for _topic, payload in broker.sent] == [0, 1, 2, 3, 4]


def test_each_entry_keeps_its_own_topic(store, broker, clock):
    store.enqueue("factoryfleet/PLANT-A/PRESS-01/registration", {"assetId": "PRESS-01"})
    store.enqueue("factoryfleet/PLANT-A/PRESS-01/telemetry", {"readings": []})

    publisher(store, broker, clock).drain_once()

    assert [topic for topic, _payload in broker.sent] == [
        "factoryfleet/PLANT-A/PRESS-01/registration",
        "factoryfleet/PLANT-A/PRESS-01/telemetry",
    ]


def test_a_drain_is_capped_at_the_batch_size(store, broker, clock):
    """Bounds message volume per cycle on a slow link."""
    queue_batches(store, clock, 10)

    confirmed = publisher(store, broker, clock, batch_size=4).drain_once()

    assert confirmed == 4
    assert store.pending_count() == 6


def test_draining_an_empty_outbox_is_a_no_op(store, broker, clock):
    assert publisher(store, broker, clock).drain_once() == 0
    assert broker.sent == []


def test_published_counter_accumulates_across_drains(store, broker, clock):
    subject = publisher(store, broker, clock)

    queue_batches(store, clock, 2)
    subject.drain_once()
    queue_batches(store, clock, 3)
    subject.drain_once()

    assert subject.published == 5


# --- the confirmation contract ---------------------------------------------------------


def test_nothing_is_sent_or_dropped_while_the_broker_is_unreachable(store, broker, clock):
    """The outage case the outbox exists for: telemetry is delayed, never lost."""
    queue_batches(store, clock, 3)
    broker.connected = False

    confirmed = publisher(store, broker, clock).drain_once()

    assert confirmed == 0
    assert broker.sent == []
    assert store.pending_count() == 3


def test_an_unconfirmed_entry_is_not_marked_published(store, broker, clock):
    """Marking on handoff would turn at-least-once into silent at-most-once."""
    queue_batches(store, clock, 1)
    broker.refuse_after = 0

    confirmed = publisher(store, broker, clock).drain_once()

    assert confirmed == 0
    assert store.pending_count() == 1


def test_a_failure_stops_the_drain_instead_of_skipping_ahead(store, broker, clock):
    """Skipping the failed entry would reorder telemetry and hide a gap."""
    queue_batches(store, clock, 5)
    broker.refuse_after = 2

    confirmed = publisher(store, broker, clock).drain_once()

    assert confirmed == 2
    assert [payload["n"] for _topic, payload in broker.sent] == [0, 1]
    # The failed entry stays at the head, so the next drain resumes exactly where this stopped.
    assert [entry.payload["n"] for entry in store.pending(10)] == [2, 3, 4]


def test_a_later_drain_resumes_from_the_entry_that_failed(store, broker, clock):
    queue_batches(store, clock, 5)
    broker.refuse_after = 2
    subject = publisher(store, broker, clock)
    subject.drain_once()

    broker.refuse_after = None
    subject.drain_once()

    assert [payload["n"] for _topic, payload in broker.sent] == [0, 1, 2, 3, 4]
    assert store.pending_count() == 0


def test_queued_telemetry_survives_an_outage_and_goes_out_in_order_afterwards(
    store, broker, clock
):
    """The end-to-end property: an outage delays telemetry and reorders nothing."""
    subject = publisher(store, broker, clock)

    broker.connected = False
    queue_batches(store, clock, 4)
    assert subject.drain_once() == 0

    broker.connected = True
    assert subject.drain_once() == 4
    assert [payload["n"] for _topic, payload in broker.sent] == [0, 1, 2, 3]


def test_an_entry_confirmed_before_a_crash_is_not_resent(tmp_path, clock):
    """Reopening the store must not replay what the broker already acknowledged."""
    path = tmp_path / "agent.sqlite"
    broker = FakeBroker()

    with Store(path, clock=clock) as before:
        queue_batches(before, clock, 3)
        broker.refuse_after = 2
        Publisher(before, broker, clock=clock).drain_once()

    broker.refuse_after = None
    with Store(path, clock=clock) as after:
        Publisher(after, broker, clock=clock).drain_once()

    assert [payload["n"] for _topic, payload in broker.sent] == [0, 1, 2]


# --- sentAt ---------------------------------------------------------------------------


def test_sent_at_is_stamped_when_the_message_actually_leaves(store, broker, clock):
    queue_batches(store, clock, 1)
    clock.advance(hours=3)  # the link was down for three hours

    publisher(store, broker, clock).drain_once()
    _topic, payload = broker.sent[0]

    assert payload["sentAt"] == "2026-07-30T07:00:10.000Z"


def test_the_stored_payload_is_not_mutated_by_a_send_attempt(store, broker, clock):
    """A failed attempt must not leave a stale sentAt on the queued copy."""
    queue_batches(store, clock, 1)
    broker.refuse_after = 0

    publisher(store, broker, clock).drain_once()

    assert "sentAt" not in store.pending(1)[0].payload


def test_a_resent_entry_is_stamped_with_the_time_it_finally_left(store, broker, clock):
    queue_batches(store, clock, 1)
    broker.connected = False
    subject = publisher(store, broker, clock)
    subject.drain_once()

    clock.advance(hours=6)
    broker.connected = True
    subject.drain_once()

    assert broker.sent[0][1]["sentAt"] == "2026-07-30T10:00:10.000Z"


# --- retention ------------------------------------------------------------------------


def test_confirmed_entries_are_pruned_once_past_their_retention_window(store, broker, clock):
    subject = publisher(store, broker, clock, retain_published_seconds=1800)
    queue_batches(store, clock, 2)
    subject.drain_once()

    clock.advance(hours=1)
    subject.drain_once()  # a later cycle does the pruning

    assert store.outbox_size() == 0


def test_recently_confirmed_entries_are_kept_for_diagnosis(store, broker, clock):
    """"What did the agent actually send?" must stay answerable for a while."""
    subject = publisher(store, broker, clock, retain_published_seconds=3600)
    queue_batches(store, clock, 2)
    subject.drain_once()

    clock.advance(minutes=5)
    subject.drain_once()

    assert store.outbox_size() == 2


# --- registration ---------------------------------------------------------------------


def test_publish_now_queues_and_sends_immediately(store, broker, clock):
    sent = publisher(store, broker, clock).publish_now(
        "factoryfleet/PLANT-A/PRESS-01/registration", {"assetId": "PRESS-01"}
    )

    assert sent is True
    assert broker.sent[0][0] == "factoryfleet/PLANT-A/PRESS-01/registration"
    assert store.pending_count() == 0


def test_registration_survives_starting_up_with_no_network(store, broker, clock):
    """An agent that boots during an outage must still announce itself once the link returns."""
    broker.connected = False
    subject = publisher(store, broker, clock)

    assert subject.publish_now("factoryfleet/PLANT-A/PRESS-01/registration", {"a": 1}) is False
    assert store.pending_count() == 1

    broker.connected = True
    subject.drain_once()

    assert broker.sent[0][1]["a"] == 1


def test_registration_is_sent_before_telemetry_queued_after_it(store, broker, clock):
    """The backend should learn what the machine is before it starts receiving its readings."""
    broker.connected = False
    subject = publisher(store, broker, clock)
    subject.publish_now("factoryfleet/PLANT-A/PRESS-01/registration", {"kind": "registration"})
    store.enqueue("factoryfleet/PLANT-A/PRESS-01/telemetry", {"kind": "telemetry"})

    broker.connected = True
    subject.drain_once()

    assert [payload["kind"] for _topic, payload in broker.sent] == ["registration", "telemetry"]
