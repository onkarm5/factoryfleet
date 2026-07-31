import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from factoryfleet_agent import wire
from factoryfleet_agent.config import AssetIdentity, SensorConfig
from factoryfleet_agent.runner import SamplingRunner
from factoryfleet_agent.sensors.base import Sensor, SensorCondition
from factoryfleet_agent.store import Store

START = datetime(2026, 7, 30, 4, 0, tzinfo=timezone.utc)

IDENTITY = AssetIdentity(
    site_id="PLANT-A",
    asset_id="PRESS-01",
    machine_type="HYDRAULIC_PRESS",
    firmware_version="2.4.1",
)


class MutableClock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)
        return self.now


class Fake(Sensor):
    def __init__(self, sensor_id, value=1.0, fail=False, clock=None):
        super().__init__(
            SensorConfig(
                sensor_id=sensor_id,
                type="fake",
                enabled=True,
                options={},
                source=Path(f"config/sensors/{sensor_id}.toml"),
            ),
            clock or MutableClock(),
        )
        self._value = value
        self._fail = fail
        self.reads = 0

    def read(self):
        self.reads += 1
        if self._fail:
            raise RuntimeError("sensor bus timeout")
        return {"value": self._value}


class Limited(Fake):
    def evaluate(self, metrics):
        return SensorCondition.CRITICAL


@pytest.fixture
def clock():
    return MutableClock()


@pytest.fixture
def store(tmp_path, clock):
    with Store(tmp_path / "agent.sqlite", clock=clock) as opened:
        yield opened


def runner(store, clock, *sensors):
    return SamplingRunner(IDENTITY, sensors, store, clock)


# --- the cycle ------------------------------------------------------------------------


def test_a_cycle_reads_every_sensor_once(store, clock):
    sensors = [Fake("vibration", clock=clock), Fake("temperature", clock=clock)]

    runner(store, clock, *sensors).sample_once()

    assert [sensor.reads for sensor in sensors] == [1, 1]


def test_a_cycle_records_state_for_every_sensor(store, clock):
    runner(store, clock, Fake("vibration", 4.2, clock=clock), Fake("temperature", 68.0, clock=clock)).sample_once()

    states = {state.sensor_id: state for state in store.latest()}

    assert set(states) == {"vibration", "temperature"}
    assert states["vibration"].metrics == {"value": 4.2}


def test_a_cycle_queues_exactly_one_batch_holding_all_readings(store, clock):
    """One message per cycle, not one per sensor: a batch is a round trip, not three."""
    runner(store, clock, Fake("vibration", clock=clock), Fake("temperature", clock=clock)).sample_once()

    pending = store.pending(10)

    assert len(pending) == 1
    assert [r["sensorId"] for r in pending[0].payload["readings"]] == ["vibration", "temperature"]


def test_the_batch_goes_to_the_assets_own_telemetry_topic(store, clock):
    runner(store, clock, Fake("vibration", clock=clock)).sample_once()

    assert store.pending(1)[0].topic == "factoryfleet/PLANT-A/PRESS-01/telemetry"


def test_the_batch_identifies_its_machine_in_the_body(store, clock):
    """Once routed through a queue the topic is gone; the payload must stand alone."""
    runner(store, clock, Fake("vibration", clock=clock)).sample_once()

    payload = store.pending(1)[0].payload

    assert payload["assetId"] == "PRESS-01"
    assert payload["siteId"] == "PLANT-A"


def test_the_batch_is_stamped_when_it_was_formed_not_when_it_is_sent(store, clock):
    """sentAt belongs to the publisher; a batch may wait hours in the outbox."""
    clock.advance(minutes=5)

    runner(store, clock, Fake("vibration", clock=clock)).sample_once()
    payload = store.pending(1)[0].payload

    assert payload["batchedAt"] == "2026-07-30T04:05:00.000Z"
    assert "sentAt" not in payload


def test_each_cycle_queues_another_batch(store, clock):
    subject = runner(store, clock, Fake("vibration", clock=clock))

    for _ in range(3):
        clock.advance(seconds=10)
        subject.sample_once()

    assert store.pending_count() == 3
    assert subject.cycles == 3


def test_state_is_overwritten_each_cycle_while_batches_accumulate(store, clock):
    """asset_state answers "how is it now"; the outbox carries the history to the cloud."""
    sensor = Fake("vibration", clock=clock)
    subject = runner(store, clock, sensor)

    subject.sample_once()
    sensor._value = 9.9
    clock.advance(seconds=10)
    subject.sample_once()

    assert len(store.latest()) == 1
    assert store.latest_for("vibration").metrics == {"value": 9.9}
    assert store.pending_count() == 2


# --- failure handling -----------------------------------------------------------------


def test_a_failing_sensor_does_not_stop_its_neighbours_being_reported(store, clock):
    subject = runner(
        store,
        clock,
        Fake("vibration", 4.2, clock=clock),
        Fake("temperature", fail=True, clock=clock),
        Fake("cycle_count", 120.0, clock=clock),
    )

    readings = subject.sample_once()
    payload = store.pending(1)[0].payload

    assert [r.condition for r in readings] == [
        SensorCondition.OK,
        SensorCondition.ERROR,
        SensorCondition.OK,
    ]
    assert len(payload["readings"]) == 3


def test_a_failed_reading_travels_with_its_reason(store, clock):
    runner(store, clock, Fake("temperature", fail=True, clock=clock)).sample_once()

    reading = store.pending(1)[0].payload["readings"][0]

    assert reading["condition"] == "ERROR"
    assert reading["error"] == "RuntimeError: sensor bus timeout"


def test_a_batch_is_still_sent_when_every_sensor_failed(store, clock):
    """Total sensor failure must be reported; silence cannot distinguish it from health."""
    subject = runner(store, clock, Fake("a", fail=True, clock=clock), Fake("b", fail=True, clock=clock))

    subject.sample_once()

    assert store.pending_count() == 1
    assert len(store.pending(1)[0].payload["readings"]) == 2


def test_a_cycle_that_cannot_be_committed_leaves_no_partial_state(store, clock, monkeypatch):
    """State without a matching batch would misrepresent what the machine reported."""
    subject = runner(store, clock, Fake("vibration", clock=clock))

    def broken_enqueue(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(store, "enqueue", broken_enqueue)

    with pytest.raises(RuntimeError, match="disk full"):
        subject.sample_once()

    assert store.latest() == ()
    assert store.pending_count() == 0
    assert subject.cycles == 0


def test_a_runner_with_no_sensors_does_nothing_rather_than_sending_an_empty_batch(store, clock):
    subject = runner(store, clock)

    assert subject.sample_once() == ()
    assert store.pending_count() == 0


# --- logging --------------------------------------------------------------------------


def test_a_healthy_cycle_logs_at_info(store, clock, caplog):
    with caplog.at_level(logging.INFO, logger="factoryfleet_agent.runner"):
        runner(store, clock, Fake("vibration", clock=clock)).sample_once()

    assert "all within limits" in caplog.text
    assert caplog.records[-1].levelno == logging.INFO


def test_a_cycle_with_a_breached_limit_logs_a_warning_naming_the_sensor(store, clock, caplog):
    with caplog.at_level(logging.INFO, logger="factoryfleet_agent.runner"):
        runner(store, clock, Limited("vibration", clock=clock)).sample_once()

    assert caplog.records[-1].levelno == logging.WARNING
    assert "vibration" in caplog.text
    assert "hard limits" in caplog.text


def test_a_cycle_with_an_unreadable_sensor_logs_a_warning_naming_it(store, clock, caplog):
    with caplog.at_level(logging.INFO, logger="factoryfleet_agent.runner"):
        runner(store, clock, Fake("temperature", fail=True, clock=clock)).sample_once()

    assert caplog.records[-1].levelno == logging.WARNING
    assert "could not be read" in caplog.text


def test_a_breached_limit_outranks_an_unreadable_sensor_in_the_log(store, clock, caplog):
    """A machine past a hard limit is the more urgent of the two."""
    with caplog.at_level(logging.INFO, logger="factoryfleet_agent.runner"):
        runner(store, clock, Limited("vibration", clock=clock), Fake("temperature", fail=True, clock=clock)).sample_once()

    assert "hard limits" in caplog.text


# --- registration ---------------------------------------------------------------------


def test_registration_declares_the_machine_and_its_sensors(store, clock):
    subject = runner(store, clock, Fake("vibration", clock=clock), Fake("temperature", clock=clock))

    payload = subject.registration_payload("0.2.0")

    assert payload == {
        "assetId": "PRESS-01",
        "siteId": "PLANT-A",
        "machineType": "HYDRAULIC_PRESS",
        "firmwareVersion": "2.4.1",
        "agentVersion": "0.2.0",
        "sensors": ["vibration", "temperature"],
        "declaredAt": "2026-07-30T04:00:00.000Z",
    }


# --- wire helpers ---------------------------------------------------------------------


def test_topics_are_scoped_to_the_asset():
    assert wire.telemetry_topic(IDENTITY) == "factoryfleet/PLANT-A/PRESS-01/telemetry"
    assert wire.registration_topic(IDENTITY) == "factoryfleet/PLANT-A/PRESS-01/registration"


def test_stamping_sent_leaves_the_original_payload_untouched():
    """The outbox copy must not be mutated by an attempt that may yet fail."""
    payload = {"assetId": "PRESS-01", "readings": []}

    stamped = wire.stamp_sent(payload, START)

    assert stamped["sentAt"] == "2026-07-30T04:00:00.000Z"
    assert "sentAt" not in payload


def test_buffering_delay_is_recoverable_from_the_two_timestamps(store, clock):
    """sentAt minus batchedAt is how long the link was down, invisible from the cloud otherwise."""
    runner(store, clock, Fake("vibration", clock=clock)).sample_once()
    buffered = store.pending(1)[0].payload

    clock.advance(hours=3)
    sent = wire.stamp_sent(buffered, clock())

    assert sent["batchedAt"] == "2026-07-30T04:00:00.000Z"
    assert sent["sentAt"] == "2026-07-30T07:00:00.000Z"
