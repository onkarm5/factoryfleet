from datetime import datetime, timezone
from pathlib import Path

import pytest

from factoryfleet_agent.config import SensorConfig
from factoryfleet_agent.sensors import base

FIXED_TIME = datetime(2026, 7, 30, 4, 15, tzinfo=timezone.utc)


def sensor_config(sensor_id="probe", type_name="probe", **options) -> SensorConfig:
    return SensorConfig(
        sensor_id=sensor_id,
        type=type_name,
        enabled=True,
        options=options,
        source=Path(f"config/sensors/{sensor_id}.toml"),
    )


def fixed_clock(moment=FIXED_TIME):
    return lambda: moment


class Constant(base.Sensor):
    """Reports whatever it was configured to report."""

    def read(self):
        return {"value": self.config.option("value", 1.0)}


class Exploding(base.Sensor):
    def read(self):
        raise RuntimeError("bus fault on channel 2")


class Limited(base.Sensor):
    def read(self):
        return {"value": self.config.option("value", 1.0)}

    def evaluate(self, metrics):
        if metrics["value"] > 10:
            return base.SensorCondition.CRITICAL
        return base.SensorCondition.OK


# --- sampling -------------------------------------------------------------------------


def test_sample_stamps_and_judges_a_reading():
    reading = Constant(sensor_config(value=4.2), fixed_clock()).sample()

    assert reading.sensor_id == "probe"
    assert reading.captured_at == FIXED_TIME
    assert reading.metrics == {"value": 4.2}
    assert reading.condition is base.SensorCondition.OK
    assert reading.error is None


def test_condition_defaults_to_ok_without_an_evaluate_override():
    reading = Constant(sensor_config(value=9999.0), fixed_clock()).sample()

    assert reading.condition is base.SensorCondition.OK


def test_evaluate_override_can_raise_critical():
    reading = Limited(sensor_config(value=11.0), fixed_clock()).sample()

    assert reading.condition is base.SensorCondition.CRITICAL


def test_a_failing_read_becomes_an_error_reading_instead_of_raising():
    """The whole point: one broken sensor must not end the sampling cycle."""
    reading = Exploding(sensor_config(), fixed_clock()).sample()

    assert reading.condition is base.SensorCondition.ERROR
    assert reading.error == "RuntimeError: bus fault on channel 2"
    assert reading.metrics == {}
    # Still stamped, so the failure itself is visible on a timeline.
    assert reading.captured_at == FIXED_TIME


def test_an_error_reading_names_the_exception_type_when_there_is_no_message():
    class Silent(base.Sensor):
        def read(self):
            raise TimeoutError()

    reading = Silent(sensor_config(), fixed_clock()).sample()

    assert reading.error == "TimeoutError"


def test_a_failing_evaluate_keeps_the_metrics_and_flags_the_judgement():
    class BadJudge(base.Sensor):
        def read(self):
            return {"value": 3.0}

        def evaluate(self, metrics):
            raise ValueError("threshold missing")

    reading = BadJudge(sensor_config(), fixed_clock()).sample()

    assert reading.condition is base.SensorCondition.ERROR
    assert reading.metrics == {"value": 3.0}
    assert "evaluate failed" in reading.error


@pytest.mark.parametrize(
    "bad_value", ["seven", True, None, float("nan"), float("inf")]
)
def test_non_numeric_and_non_finite_metrics_are_rejected(bad_value):
    """These would serialise to something the backend cannot store as a number."""

    class BadMetric(base.Sensor):
        def read(self):
            return {"value": bad_value}

    reading = BadMetric(sensor_config(), fixed_clock()).sample()

    assert reading.condition is base.SensorCondition.ERROR


def test_read_returning_something_other_than_a_mapping_is_rejected():
    class NotAMapping(base.Sensor):
        def read(self):
            return [1, 2, 3]

    reading = NotAMapping(sensor_config(), fixed_clock()).sample()

    assert reading.condition is base.SensorCondition.ERROR
    assert "must return a mapping" in reading.error


# --- wire format ----------------------------------------------------------------------


def test_payload_uses_the_wire_field_names():
    reading = Constant(sensor_config(value=4.2), fixed_clock()).sample()

    assert reading.to_payload() == {
        "sensorId": "probe",
        "capturedAt": "2026-07-30T04:15:00.000Z",
        "condition": "OK",
        "metrics": {"value": 4.2},
    }


def test_payload_omits_error_when_there_is_none():
    payload = Constant(sensor_config(), fixed_clock()).sample().to_payload()

    assert "error" not in payload


def test_payload_includes_error_when_the_sensor_failed():
    payload = Exploding(sensor_config(), fixed_clock()).sample().to_payload()

    assert payload["error"] == "RuntimeError: bus fault on channel 2"
    assert payload["condition"] == "ERROR"


def test_timestamps_are_normalised_to_utc():
    """A reading captured in local time must still travel as UTC."""
    from datetime import timedelta

    ist = timezone(timedelta(hours=5, minutes=30))
    local = datetime(2026, 7, 30, 9, 45, tzinfo=ist)

    payload = Constant(sensor_config(), fixed_clock(local)).sample().to_payload()

    assert payload["capturedAt"] == "2026-07-30T04:15:00.000Z"


# --- registry -------------------------------------------------------------------------


def test_register_makes_a_type_resolvable_by_config(monkeypatch):
    """FR-13: a new sensor is one class plus one config file."""
    monkeypatch.setattr(base, "_REGISTRY", {})

    @base.register("thermocouple")
    class Thermocouple(base.Sensor):
        def read(self):
            return {"c": 21.0}

    sensor = base.create(sensor_config("t1", "thermocouple"), fixed_clock())

    assert isinstance(sensor, Thermocouple)
    assert sensor.sensor_id == "t1"
    assert sensor.type_name == "thermocouple"
    assert base.registered_types() == {"thermocouple"}


def test_unknown_type_names_the_file_and_the_known_types(monkeypatch):
    monkeypatch.setattr(base, "_REGISTRY", {})
    base.register("thermocouple")(Constant)

    with pytest.raises(base.UnknownSensorType) as caught:
        base.create(sensor_config("x", "flux_capacitor"))

    message = str(caught.value)
    assert "flux_capacitor" in message
    assert "config/sensors/x.toml" in message
    assert "thermocouple" in message


def test_registering_two_classes_under_one_type_is_rejected(monkeypatch):
    monkeypatch.setattr(base, "_REGISTRY", {})
    base.register("probe")(Constant)

    with pytest.raises(ValueError, match="already registered"):
        base.register("probe")(Limited)


def test_re_registering_the_same_class_is_harmless(monkeypatch):
    """Module re-import must not be fatal."""
    monkeypatch.setattr(base, "_REGISTRY", {})
    base.register("probe")(Constant)
    base.register("probe")(Constant)

    assert base.registered_types() == {"probe"}


def test_registering_a_non_sensor_is_rejected(monkeypatch):
    monkeypatch.setattr(base, "_REGISTRY", {})

    with pytest.raises(TypeError, match="Sensor subclass"):

        @base.register("bogus")
        class NotASensor:
            pass


def test_create_all_builds_sensors_in_the_order_given(monkeypatch):
    monkeypatch.setattr(base, "_REGISTRY", {})
    base.register("probe")(Constant)

    sensors = base.create_all(
        [sensor_config("a"), sensor_config("b"), sensor_config("c")], fixed_clock()
    )

    assert [sensor.sensor_id for sensor in sensors] == ["a", "b", "c"]


def test_builtin_sensor_types_are_registered_by_importing_the_package():
    import factoryfleet_agent.sensors  # noqa: F401

    # Populated in the following commit; the import path itself must work now.
    assert isinstance(base.registered_types(), frozenset)


# --- commands -------------------------------------------------------------------------


def test_a_sensor_rejects_a_command_it_does_not_declare():
    sensor = Constant(sensor_config(), fixed_clock())

    with pytest.raises(base.UnknownCommand, match="does not accept"):
        sensor.execute("recalibrate", {})


def test_a_declared_command_reaches_the_handler():
    class Recalibrating(base.Sensor):
        COMMANDS = frozenset({"recalibrate"})

        def read(self):
            return {"offset": 0.0}

        def handle_command(self, name, args):
            return {"applied": args["offset"]}

    sensor = Recalibrating(sensor_config(), fixed_clock())

    assert sensor.execute("recalibrate", {"offset": 0.25}) == {"applied": 0.25}


def test_declaring_a_command_without_a_handler_is_reported():
    class Forgetful(base.Sensor):
        COMMANDS = frozenset({"recalibrate"})

        def read(self):
            return {"value": 1.0}

    sensor = Forgetful(sensor_config(), fixed_clock())

    with pytest.raises(base.UnknownCommand, match="no handler"):
        sensor.execute("recalibrate", {})
