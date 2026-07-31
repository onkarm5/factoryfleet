import random
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from factoryfleet_agent import config as config_module
from factoryfleet_agent.sensors import base, registered_types
from factoryfleet_agent.sensors.cycle_count import CycleCountSensor
from factoryfleet_agent.sensors.simulation import Waveform
from factoryfleet_agent.sensors.temperature import TemperatureSensor
from factoryfleet_agent.sensors.vibration import VibrationSensor

START = datetime(2026, 7, 30, 4, 0, tzinfo=timezone.utc)

VIBRATION_OPTIONS = {
    "baseline_rms_mm_s": 4.2,
    "noise_mm_s": 0.30,
    "drift_mm_s_per_hour": 0.0,
    "peak_factor": 2.5,
    "hard_limit_rms_mm_s": 15.0,
}

TEMPERATURE_OPTIONS = {
    "baseline_bearing_c": 68.0,
    "baseline_spindle_c": 55.0,
    "noise_c": 1.2,
    "drift_c_per_hour": 0.0,
    "hard_limit_bearing_c": 95.0,
}

CYCLE_OPTIONS = {"cycles_per_minute": 42.0, "jitter_cycles": 3.0}


class MutableClock:
    """A clock a test can advance, so drift over hours needs no waiting."""

    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)
        return self.now


def sensor_config(sensor_id, type_name, **options):
    return config_module.SensorConfig(
        sensor_id=sensor_id,
        type=type_name,
        enabled=True,
        options=options,
        source=Path(f"config/sensors/{sensor_id}.toml"),
    )


def vibration(clock=None, seed=7, **overrides):
    options = {**VIBRATION_OPTIONS, **overrides}
    return VibrationSensor(
        sensor_config("vibration", "vibration", **options),
        clock or MutableClock(),
        random.Random(seed),
    )


def temperature(clock=None, seed=7, **overrides):
    options = {**TEMPERATURE_OPTIONS, **overrides}
    return TemperatureSensor(
        sensor_config("temperature", "temperature", **options),
        clock or MutableClock(),
        random.Random(seed),
    )


def cycle_count(clock=None, seed=7, **overrides):
    options = {**CYCLE_OPTIONS, **overrides}
    return CycleCountSensor(
        sensor_config("cycle_count", "cycle_count", **options),
        clock or MutableClock(),
        random.Random(seed),
    )


# --- registration ---------------------------------------------------------------------


def test_importing_the_package_registers_all_three_builtins():
    assert {"vibration", "temperature", "cycle_count"} <= registered_types()


def test_configured_sensors_resolve_through_the_registry():
    """The path the runner actually uses: config file to live sensor."""
    loaded = config_module.load(Path(__file__).resolve().parents[1] / "config")

    sensors = base.create_all(loaded.enabled_sensors, MutableClock())

    assert [type(sensor).__name__ for sensor in sensors] == [
        "CycleCountSensor",
        "TemperatureSensor",
        "VibrationSensor",
    ]


# --- waveform -------------------------------------------------------------------------


def test_waveform_without_noise_or_drift_is_its_baseline():
    waveform = Waveform(baseline=4.2, noise=0.0)

    assert waveform.at(10.0, random.Random(1)) == 4.2


def test_waveform_drift_accumulates_with_runtime():
    waveform = Waveform(baseline=4.0, noise=0.0, drift_per_hour=0.5)

    assert waveform.at(0.0, random.Random(1)) == 4.0
    assert waveform.at(4.0, random.Random(1)) == 6.0


def test_waveform_floor_prevents_negative_physical_readings():
    """Noise around a low baseline must not produce a negative reading."""
    waveform = Waveform(baseline=0.05, noise=5.0, floor=0.0)
    rng = random.Random(3)

    assert all(waveform.at(0.0, rng) >= 0.0 for _ in range(200))


# --- vibration ------------------------------------------------------------------------


def test_vibration_reports_rms_and_peak():
    reading = vibration().sample()

    assert set(reading.metrics) == {"rms_mm_s", "peak_mm_s"}
    assert reading.condition is base.SensorCondition.OK


def test_vibration_peak_is_the_configured_multiple_of_rms():
    reading = vibration(noise_mm_s=0.0).sample()

    assert reading.metrics["rms_mm_s"] == 4.2
    assert reading.metrics["peak_mm_s"] == pytest.approx(4.2 * 2.5)


def test_vibration_stays_near_baseline_without_drift():
    sensor = vibration()
    clock = sensor._clock

    values = []
    for _ in range(50):
        clock.advance(minutes=1)
        values.append(sensor.sample().metrics["rms_mm_s"])

    assert all(3.0 < value < 5.5 for value in values), values


def test_vibration_drift_climbs_out_of_baseline_while_staying_under_the_hard_limit():
    """The predictive-maintenance case: this is what the backend's baseline must catch.

    A machine drifting from 4.2 to roughly 8 mm/s is in trouble, yet every reading is still
    far below the 15 mm/s absolute limit, so a fleet-wide threshold would say nothing.
    """
    clock = MutableClock()
    sensor = vibration(clock, drift_mm_s_per_hour=0.5)

    first = sensor.sample()
    clock.advance(hours=8)
    later = sensor.sample()

    assert first.metrics["rms_mm_s"] < 5.5
    assert later.metrics["rms_mm_s"] > 7.5
    # Never trips the hard limit, so only baseline analysis can find it.
    assert first.condition is base.SensorCondition.OK
    assert later.condition is base.SensorCondition.OK


def test_vibration_past_the_hard_limit_is_critical_without_any_history():
    clock = MutableClock()
    sensor = vibration(clock, drift_mm_s_per_hour=2.0, noise_mm_s=0.0)

    clock.advance(hours=6)  # 4.2 + 12.0 = 16.2, past the 15.0 limit

    assert sensor.sample().condition is base.SensorCondition.CRITICAL


def test_vibration_rejects_configuration_missing_a_hard_limit_at_construction():
    """Fail at startup, not with an ERROR reading every interval afterwards."""
    options = {key: value for key, value in VIBRATION_OPTIONS.items() if key != "hard_limit_rms_mm_s"}

    with pytest.raises(config_module.ConfigError, match="hard_limit_rms_mm_s"):
        VibrationSensor(sensor_config("vibration", "vibration", **options), MutableClock())


# --- temperature ----------------------------------------------------------------------


def test_temperature_reports_bearing_and_spindle():
    reading = temperature().sample()

    assert set(reading.metrics) == {"bearing_c", "spindle_c"}
    assert reading.condition is base.SensorCondition.OK


def test_temperature_sits_at_its_baselines_without_noise():
    reading = temperature(noise_c=0.0).sample()

    assert reading.metrics["bearing_c"] == 68.0
    assert reading.metrics["spindle_c"] == 55.0


def test_only_the_bearing_drifts_so_the_spindle_stays_a_control_reading():
    """Bearing rising while the spindle holds steady localises the fault to the bearing."""
    clock = MutableClock()
    sensor = temperature(clock, noise_c=0.0, drift_c_per_hour=2.0)

    clock.advance(hours=5)
    reading = sensor.sample()

    assert reading.metrics["bearing_c"] == 78.0
    assert reading.metrics["spindle_c"] == 55.0


def test_bearing_past_its_hard_limit_is_critical():
    clock = MutableClock()
    sensor = temperature(clock, noise_c=0.0, drift_c_per_hour=10.0)

    clock.advance(hours=3)  # 68 + 30 = 98, past the 95 limit

    assert sensor.sample().condition is base.SensorCondition.CRITICAL


# --- cycle count ----------------------------------------------------------------------


def test_cycle_count_reports_total_rate_and_uptime():
    reading = cycle_count().sample()

    assert set(reading.metrics) == {"cycles_total", "cycles_per_minute", "uptime_seconds"}


def test_cycle_total_advances_at_the_configured_rate():
    clock = MutableClock()
    sensor = cycle_count(clock, jitter_cycles=0.0)

    clock.advance(minutes=10)

    assert sensor.sample().metrics["cycles_total"] == 420


def test_cycle_total_is_monotonic_across_many_samples():
    """A physical counter never counts down, whatever the jitter does."""
    clock = MutableClock()
    sensor = cycle_count(clock, jitter_cycles=50.0)

    totals = []
    for _ in range(40):
        clock.advance(seconds=5)
        totals.append(sensor.sample().metrics["cycles_total"])

    assert totals == sorted(totals)


def test_uptime_tracks_runtime_not_sample_count():
    clock = MutableClock()
    sensor = cycle_count(clock)

    clock.advance(minutes=90)

    assert sensor.sample().metrics["uptime_seconds"] == pytest.approx(5400.0)


def test_observed_rate_is_reported_per_reading():
    clock = MutableClock()
    sensor = cycle_count(clock, jitter_cycles=0.0)

    clock.advance(minutes=2)

    assert sensor.sample().metrics["cycles_per_minute"] == pytest.approx(42.0)


def sampled_rates(interval_seconds, samples=200, seed=4):
    clock = MutableClock()
    sensor = cycle_count(clock, seed=seed)
    rates = []
    for _ in range(samples):
        clock.advance(seconds=interval_seconds)
        rates.append(sensor.sample().metrics["cycles_per_minute"])
    return rates


def test_a_longer_sampling_window_gives_a_tighter_rate_estimate():
    """Counting statistics: observing more cycles estimates the rate more precisely.

    Jitter accumulates as a random walk, so its spread grows with the square root of the
    interval while the interval itself grows linearly — a longer window therefore averages
    the wobble out. Applied flat instead, the reported rate would be equally noisy at every
    interval, which is not how a physical counter behaves.
    """
    short = statistics.stdev(sampled_rates(10))
    long = statistics.stdev(sampled_rates(300))

    assert long < short / 2


def test_the_cycle_total_stays_unbiased_regardless_of_sampling_interval():
    """Jitter must add noise, not drift the counter away from the true production figure."""
    for interval_seconds in (10, 60, 300):
        clock = MutableClock()
        sensor = cycle_count(clock)
        for _ in range(int(3600 / interval_seconds)):
            clock.advance(seconds=interval_seconds)
        total = sensor.sample().metrics["cycles_total"]

        # One hour at 42 cycles/min is 2520 cycles.
        assert total == pytest.approx(2520, rel=0.02), interval_seconds


def test_the_first_reading_does_not_report_an_absurd_rate():
    """Dividing a jittered count by a near-zero window used to report 470 cycles/min at 42.

    The first sample of every agent start hits this, so it is the reading most likely to be
    seen and the one that would make the whole simulation look broken.
    """
    reading = cycle_count().sample()

    assert reading.metrics["uptime_seconds"] == 0.0
    assert reading.metrics["cycles_total"] == 0
    assert reading.metrics["cycles_per_minute"] == 0.0


def test_a_window_too_short_to_estimate_from_falls_back_to_the_long_run_average():
    clock = MutableClock()
    sensor = cycle_count(clock, jitter_cycles=0.0)

    clock.advance(minutes=10)
    sensor.sample()  # 420 cycles over 10 minutes

    clock.advance(milliseconds=1)  # far too short a window to divide by
    rate = sensor.sample().metrics["cycles_per_minute"]

    assert rate == pytest.approx(42.0, rel=0.01)


def test_a_sample_with_no_elapsed_time_reports_a_zero_rate_and_no_new_cycles():
    """Two samples in the same instant must not invent production."""
    sensor = cycle_count(jitter_cycles=0.0)

    first = sensor.sample()
    second = sensor.sample()

    assert second.metrics["cycles_per_minute"] == 0.0
    assert second.metrics["cycles_total"] == first.metrics["cycles_total"]


def test_a_counter_has_no_hard_limit_so_it_never_reports_critical():
    clock = MutableClock()
    sensor = cycle_count(clock)

    clock.advance(hours=200)

    assert sensor.sample().condition is base.SensorCondition.OK


# --- shared simulation behaviour ------------------------------------------------------


def test_a_clock_stepping_backwards_does_not_produce_negative_runtime():
    """NTP correcting the machine clock must not yield negative uptime or reverse drift."""
    clock = MutableClock()
    sensor = cycle_count(clock, jitter_cycles=0.0)

    clock.advance(minutes=-30)
    reading = sensor.sample()

    assert reading.metrics["uptime_seconds"] == 0.0
    assert reading.metrics["cycles_total"] == 0


def test_seeded_sensors_produce_identical_reading_streams():
    """Determinism under a seed is what makes the simulation testable."""
    first = [vibration(seed=99).sample().metrics for _ in range(5)]
    second = [vibration(seed=99).sample().metrics for _ in range(5)]

    assert first == second
