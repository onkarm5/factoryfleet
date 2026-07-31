# Asset agent

The Python process that runs on (or simulates) one plant-floor machine. It samples the
machine's sensors on an interval, commits every reading to a local SQLite outbox, and
publishes batches over MQTT.

```
Scheduler ─► SamplingRunner ─► sensor.sample()
                   │
                   ├─► asset_state   (SQLite — how the machine is now)
                   └─► outbox        (SQLite — what is owed to the cloud)
                             │
Scheduler ─► Publisher ──────┘─► MQTT publish, confirmed then forgotten
```

Two loops, one store, and nothing else joining them. That is what makes a network outage
uneventful: the sampler holds no connection, so it cannot be blocked by one. Readings
accumulate in the outbox and go out, in order, when the link returns.

## Quick start

Requires Python 3.11+ and Docker.

```bash
# from the repository root — the local stand-in for AWS IoT Core
docker compose up -d mosquitto
```

```bash
cd agent
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m factoryfleet_agent
```

The agent logs each sampling cycle and each publish. Stop it with Ctrl-C; it drains what it
can and closes the database cleanly.

To watch what actually reaches the broker:

```bash
docker exec factoryfleet-mosquitto mosquitto_sub -h localhost -t 'factoryfleet/#' -v
```

Run the tests with:

```bash
cd agent && .venv/bin/python -m pytest
```

## What it publishes

Topics are scoped to the machine, so an asset's IoT Core policy can permit exactly its own
prefix and nothing else.

| Topic | Payload |
|---|---|
| `factoryfleet/{siteId}/{assetId}/registration` | machine type, firmware, agent version, sensor list |
| `factoryfleet/{siteId}/{assetId}/telemetry` | one batch of sensor readings |

```json
{
  "assetId": "PRESS-01",
  "siteId": "PLANT-A",
  "batchedAt": "2026-07-31T06:50:39.168Z",
  "sentAt": "2026-07-31T06:50:44.052Z",
  "readings": [
    { "sensorId": "vibration",   "capturedAt": "2026-07-31T06:50:39.168Z", "condition": "OK",
      "metrics": { "rms_mm_s": 4.48, "peak_mm_s": 11.2 } },
    { "sensorId": "temperature", "capturedAt": "2026-07-31T06:50:39.168Z", "condition": "OK",
      "metrics": { "bearing_c": 66.21, "spindle_c": 54.1 } },
    { "sensorId": "cycle_count", "capturedAt": "2026-07-31T06:50:39.168Z", "condition": "OK",
      "metrics": { "cycles_total": 8, "cycles_per_minute": 47.38, "uptime_seconds": 10.0 } }
  ]
}
```

Three timestamps, and the distinction is the point. `capturedAt` is when the sensor was read,
`batchedAt` when the agent committed the batch, and `sentAt` when it actually left. `sentAt`
minus `batchedAt` is how long the agent could not reach the broker — otherwise invisible from
the cloud, where telemetry buffered through an outage looks identical to telemetry produced
late.

`condition` is the agent's own judgement of a reading, and is deliberately narrow:

| Condition | Meaning |
|---|---|
| `OK` | Within the sensor's configured limits. |
| `CRITICAL` | A hard limit was breached. Actionable with no further analysis. |
| `ERROR` | The sensor could not be read. Says nothing about the machine. |

There is no `DEGRADED` here. Detecting that a machine is drifting away from its own normal
range needs history across readings, and the agent only ever sees one at a time — that
judgement belongs to the backend.

## Configuration

Two levels. [`config/agent.toml`](config/agent.toml) holds the machine's identity, its broker
connection, and the two intervals. One file per sensor under
[`config/sensors/`](config/sensors) holds that sensor's tuning, so adding a sensor is a new
file rather than an edit to a shared list.

| Key | Meaning |
|---|---|
| `asset.site_id`, `asset.asset_id` | Address the machine: its topic prefix and registry key |
| `asset.machine_type` | Validated against the backend's enum at startup |
| `broker.host`, `broker.port`, `broker.tls` | Broker endpoint. Milestone 3 points this at IoT Core |
| `broker.client_id` | Empty means "use the asset id", which is what IoT Core policies match on |
| `sampling.interval_seconds` | How often every enabled sensor is read |
| `publishing.interval_seconds` | How often the outbox is drained — slower, so readings batch |
| `publishing.batch_size` | Most entries per drain, bounding volume on a slow link |
| `storage.database_path` | SQLite file. Relative paths resolve against `agent/` |

Configuration is validated once at load and reports the offending file and key. An agent that
starts with bad configuration and reports nonsense is worse than one that refuses to start.

```
$ .venv/bin/python -m factoryfleet_agent
configuration error: config/agent.toml: [asset] machine_type 'TELEPORTER' is not one of
CNC_MILL, CONVEYOR, HYDRAULIC_PRESS, INJECTION_MOLDER, ROBOT_ARM
```

Override the log level for one run with `--log-level DEBUG`, or point at a different
configuration directory with `--config`.

## Simulated sensors

The built-in sensors model a machine rather than reading hardware, so the fleet is
demonstrable without a plant floor. What they simulate matters: flat random numbers would make
the backend's baseline detection appear to work when it does not. Each reading is a
machine-specific baseline, plus cycle-to-cycle noise, plus optional drift.

Drift is the interesting knob. Set `drift_mm_s_per_hour` in
[`config/sensors/vibration.toml`](config/sensors/vibration.toml) above zero and the machine
climbs out of its own normal range while staying far below the hard limit:

| Runtime | Vibration RMS | Condition |
|---|---|---|
| +0h | 3.85 mm/s | `OK` |
| +4h | 5.66 mm/s | `OK` |
| +8h | 8.00 mm/s | `OK` |
| +12h | 8.91 mm/s | `OK` |
| +26h | 15.22 mm/s | `CRITICAL` |

Everything up to +12h is a machine in trouble that a fleet-wide 15 mm/s threshold says nothing
about. That window is what the anomaly detector in milestone 6 exists to catch. Pair it with
`drift_c_per_hour` on the temperature sensor for a realistic failure: only the bearing drifts,
leaving the spindle as a control reading, so the fault localises to the bearing rather than
just "the machine is hot".

## Adding a sensor

One class and one config file. Nothing in the scheduler, the store, or the transport changes.

```python
# factoryfleet_agent/sensors/spindle_load.py
from typing import Mapping

from factoryfleet_agent.sensors.base import SensorCondition, register
from factoryfleet_agent.sensors.simulation import SimulatedSensor


@register("spindle_load")
class SpindleLoadSensor(SimulatedSensor):
    def __init__(self, config, clock=None, rng=None):
        super().__init__(config, clock, rng)
        # Read required tuning here, so a misconfigured sensor fails at startup rather
        # than emitting an ERROR reading every interval for the rest of its life.
        self._hard_limit = config.required_float("hard_limit_pct")

    def read(self) -> Mapping[str, float]:
        # May raise freely: the base class turns a failure into an ERROR reading, so one
        # seized sensor cannot end the cycle and take its healthy neighbours with it.
        return {"load_pct": 62.5}

    def evaluate(self, metrics: Mapping[str, float]) -> SensorCondition:
        if metrics["load_pct"] > self._hard_limit:
            return SensorCondition.CRITICAL
        return SensorCondition.OK
```

```toml
# config/sensors/spindle_load.toml
sensor_id = "spindle_load"
type = "spindle_load"
enabled = true

[options]
hard_limit_pct = 95.0
```

Add the module to the imports in
[`factoryfleet_agent/sensors/__init__.py`](factoryfleet_agent/sensors/__init__.py) so its
registration runs, and restart. A sensor reading real hardware subclasses `Sensor` directly
instead of `SimulatedSensor`.

## Layout

```
agent/
├── config/
│   ├── agent.toml                 identity, broker, intervals
│   └── sensors/                   one file per sensor
├── factoryfleet_agent/
│   ├── __main__.py                python -m factoryfleet_agent
│   ├── main.py                    CLI, logging, signal handling
│   ├── agent.py                   wires the subsystems, owns their lifecycle
│   ├── config.py                  configuration loading and validation
│   ├── scheduler.py               fixed-interval ticker
│   ├── runner.py                  the sampling cycle
│   ├── store.py                   SQLite asset_state + outbox
│   ├── wire.py                    topics and payload shapes
│   ├── timeutil.py                UTC formatting shared by store and wire
│   ├── sensors/
│   │   ├── base.py                Sensor contract, @register, Reading
│   │   ├── simulation.py          baseline + noise + drift
│   │   └── vibration.py, temperature.py, cycle_count.py
│   └── transport/
│       ├── broker.py              BrokerClient protocol + paho MQTT client
│       └── publisher.py           drains the outbox, confirmed then forgotten
└── tests/
```

## Durability

The store is the boundary that has to survive, and the guarantee is at-least-once:

- A reading is committed to SQLite before any publish is attempted.
- An entry is marked published only once the broker acknowledges it. Marking on local handoff
  would make delivery at-most-once, and the loss would be silent — a drained outbox looks
  identical whether or not anything arrived.
- A failed send ends the drain rather than skipping ahead, so telemetry keeps the order the
  machine produced it in.
- `synchronous=FULL`, because an unannounced power cut is a normal event on a plant floor.
- The outbox is capped. Past the cap the oldest unpublished entries are dropped, since an
  operator restoring a link after two days wants the machine's state now, not Tuesday's.
  Drops are counted rather than silent.

Restarting the agent resumes from the outbox: entries the broker never confirmed are resent,
and ones it did are not.

## Not yet implemented

Commands arrive in milestone 5 — `get_status`, `run_diagnostic`, `reload_config`,
`restart_agent`, plus per-sensor commands such as `recalibrate`. The sensor base class already
carries the `COMMANDS` contract and the `asset_state` table already answers "how is this
machine now" without waiting for the next sample; what is missing is the subscriber and the
dispatcher. Milestone 3 replaces `transport/broker.py`'s Mosquitto connection with AWS IoT
Core and per-asset X.509 certificates.
