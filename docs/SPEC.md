# FactoryFleet — Functional and Technical Specification

## 1. Problem

A manufacturing operator runs multiple plants, each with dozens to hundreds of unattended
machines. Today the failure signal is the failure itself: a machine seizes, the line stops,
and a maintenance engineer walks the floor to diagnose it. There is no single place to see
machine health across sites, no history of how a machine's readings trended before it broke,
and no way to run a diagnostic or reset a machine without physically visiting it.

FactoryFleet addresses three needs:

1. **Visibility** — one view of every machine's live health across every site.
2. **Early warning** — detect a machine drifting away from its own normal operating
   baseline before it crosses a hard failure threshold.
3. **Remote action** — run diagnostics, reset, or reconfigure a machine from the dashboard.

## 2. Domain model

| Term | Meaning |
|---|---|
| **Site** | A plant or facility. Groups assets and scopes API access. |
| **Asset** | One monitored machine (a CNC mill, hydraulic press, conveyor, robot arm, injection molder). Runs one agent. |
| **Sensor** | A pluggable reading collected from an asset — vibration RMS, spindle/bearing temperature, cycle count, uptime. One asset has many sensors. |
| **Reading** | A single timestamped sensor measurement with a derived condition. |
| **Command** | An instruction sent from the backend to a specific asset, acknowledged and completed with a result. |
| **Alert** | Raised when an asset goes offline, or when a sensor deviates from its learned baseline. |

### Asset health states

| State | Meaning |
|---|---|
| `HEALTHY` | All sensors within their normal operating baseline. |
| `DEGRADED` | At least one sensor is drifting — anomaly detected, not yet critical. Investigate at next planned maintenance. |
| `CRITICAL` | A sensor has breached a hard safety/operational limit. Immediate attention. |
| `OFFLINE` | No telemetry received within the staleness threshold. |
| `UNKNOWN` | Registered but has not yet reported. |

## 3. Functional requirements

### Asset lifecycle
- **FR-1** An agent registers its asset on startup, declaring machine type, firmware version,
  and the sensors it will report. Registration is idempotent — a restart updates the existing
  record rather than creating a duplicate.
- **FR-2** An operator can list all assets at a site with current health state and last-seen
  time, and can fetch one asset's detail.

### Telemetry
- **FR-3** An agent samples every enabled sensor on a fixed interval and publishes readings
  as a batch.
- **FR-4** Readings are written to a local store before publish, and are only removed once the
  broker confirms receipt. A network outage delays telemetry; it must not lose it.
- **FR-5** The backend persists every reading and updates the asset's health state and
  last-seen time.

### Remote commands
- **FR-6** An operator can issue a command to one asset, targeting either the whole asset or a
  single sensor.
- **FR-7** An asset acknowledges a command on receipt, then reports completion with a result
  payload or an error. Every command is recorded with its outcome and timing.
- **FR-8** A command not acknowledged within a timeout is recorded as timed out rather than
  left pending forever.
- **FR-9** Built-in commands: `get_status`, `run_diagnostic`, `reload_config`, `restart_agent`.
  A sensor may declare additional commands (for example `recalibrate`).

### Health and alerting
- **FR-10** An asset with no telemetry for longer than the staleness threshold is marked
  `OFFLINE` and an alert is raised.
- **FR-11** Each sensor maintains a rolling baseline (mean and standard deviation) of its
  recent readings. A reading beyond a configured number of standard deviations marks the asset
  `DEGRADED` and raises an anomaly alert. This is the predictive-maintenance signal: it catches
  a bearing heating up or vibration climbing while both are still inside their absolute limits.
- **FR-12** A reading beyond a configured hard limit marks the asset `CRITICAL` immediately,
  independent of its baseline.

### Extensibility
- **FR-13** Adding a sensor type requires one new sensor class and one config file. No changes
  to the scheduler, transport, or backend.

### Out of scope for v1
Multi-tenant RBAC (a single API credential per site), firmware binary distribution, historical
trend charts beyond the raw reading API, and horizontal scaling of the backend beyond one task.

## 4. Architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Asset Agent (Python) — one process per machine                │
│                                                               │
│  Scheduler ─► SensorRunner ─► sensor.read()                   │
│                    │                                           │
│                    ├─► asset_state   (SQLite, latest reading)  │
│                    └─► outbox        (SQLite, pending publish) │
│                              │                                 │
│  Publisher ◄─── drains outbox ─► MQTT publish (mutual TLS)     │
│  Subscriber ──► CommandDispatcher ─► sensor.on_command()       │
│                                    └─► global command handler  │
└──────────────────────────────────────────────────────────────┘
            ▲  MQTT over TLS 8883, X.509 client certificate
            ▼
┌──────────────────────────────────────────────────────────────┐
│ AWS IoT Core                                                  │
│   Thing per asset, policy scoped to that asset's own topics    │
│   Rules Engine:  telemetry topic → SQS telemetry queue         │
│                  result topic    → SQS result queue            │
└──────────────────────────────────────────────────────────────┘
            ▲                              │
            │ IoT Data Plane publish       ▼ SQS
┌──────────────────────────────────────────────────────────────┐
│ Backend (Spring Boot, ECS Fargate)                            │
│   REST API ─────────► asset registry, fleet status, commands   │
│   SQS consumers ────► persist readings, update health          │
│   AnomalyDetector ──► rolling baseline per sensor              │
│   StalenessJob ─────► mark OFFLINE, publish SNS alert          │
│                     │                                          │
│                     ▼                                          │
│              RDS PostgreSQL          SNS → email / webhook     │
└──────────────────────────────────────────────────────────────┘
```

### Why this shape

- **Managed broker over a custom protocol.** IoT Core owns per-device identity, mutual-TLS
  authentication, and reconnection. Each asset gets its own certificate and a policy scoped to
  its own topics, so a compromised machine cannot read or spoof another machine's traffic.
- **Queues between broker and backend.** IoT rules land messages in SQS rather than calling the
  backend directly, so a backend deploy, restart, or slow database never drops plant-floor
  telemetry, and ingestion can be retried independently.
- **Outbox on the agent.** Plant network links are unreliable. The agent's local SQLite outbox
  is the durability boundary: a reading is committed locally before it is published and removed
  only once the broker confirms it.

## 5. MQTT topic design

| Topic | Direction | Payload |
|---|---|---|
| `factoryfleet/{siteId}/{assetId}/telemetry` | agent → cloud | batch of sensor readings |
| `factoryfleet/{siteId}/{assetId}/registration` | agent → cloud | asset identity, machine type, firmware, sensor list |
| `factoryfleet/{siteId}/{assetId}/commands` | cloud → agent | one command |
| `factoryfleet/{siteId}/{assetId}/commands/ack` | agent → cloud | receipt acknowledgement |
| `factoryfleet/{siteId}/{assetId}/commands/result` | agent → cloud | completion or error |

An asset's IoT policy permits publish and subscribe only on topics containing its own
`assetId`.

### Telemetry payload

```json
{
  "assetId": "PRESS-01",
  "siteId": "PLANT-A",
  "batchedAt": "2026-07-30T04:15:00.168Z",
  "sentAt": "2026-07-30T04:15:02.052Z",
  "readings": [
    { "sensorId": "vibration",   "capturedAt": "2026-07-30T04:15:00.168Z", "condition": "OK",
      "metrics": { "rms_mm_s": 4.7, "peak_mm_s": 11.2 } },
    { "sensorId": "temperature", "capturedAt": "2026-07-30T04:15:00.168Z", "condition": "OK",
      "metrics": { "bearing_c": 71.5, "spindle_c": 55.2 } }
  ]
}
```

A reading that could not be taken carries `"condition": "ERROR"` and an `error` field
describing the failure, with no metrics.

Asset and site are repeated in the body although they are already in the topic: once messages
are routed through a queue the topic is no longer attached to them, and a reading that cannot
say which machine produced it is useless.

#### The three timestamps

| Field | Set by | Meaning |
|---|---|---|
| `capturedAt` | the sensor | when the measurement was taken |
| `batchedAt` | the agent's sampling cycle | when the batch was committed to the outbox |
| `sentAt` | the publisher, at publish time | when the batch actually left the agent |

`sentAt` is stamped when the message is sent rather than when it is queued, because a batch may
sit in the outbox for hours while a link is down. Its difference from `batchedAt` is therefore
how long the agent could not reach the broker — a figure otherwise invisible from the cloud,
where telemetry buffered through an outage is indistinguishable from telemetry produced late.

### Reading conditions

The agent judges each reading locally, and its vocabulary is deliberately narrower than the
asset health states in §2:

| Condition | Meaning |
|---|---|
| `OK` | Within the sensor's configured limits. |
| `CRITICAL` | A hard limit was breached (FR-12). Actionable with no further analysis. |
| `ERROR` | The sensor could not be read. Says nothing about the machine's condition. |

There is no agent-side `DEGRADED`. The agent sees one reading at a time, so it can judge a
fixed limit but not drift away from a machine's learned baseline; that requires history across
readings and belongs to the backend (FR-11).

### Command payload

```json
{ "commandId": "uuid", "name": "run_diagnostic", "target": "sensor:vibration", "args": {}, "issuedAt": "..." }
```

### Command result payload

```json
{ "commandId": "uuid", "status": "SUCCEEDED", "result": {}, "error": null,
  "startedAt": "...", "completedAt": "..." }
```

## 6. REST API

Base path `/api/v1`. Operator endpoints are scoped by site.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/sites/{siteId}/assets` | Register or update an asset (idempotent upsert) |
| `GET` | `/sites/{siteId}/assets` | List assets at a site with health and last-seen |
| `GET` | `/sites/{siteId}/assets/{assetId}` | One asset's detail |
| `GET` | `/sites/{siteId}/assets/{assetId}/readings` | Recent readings *(milestone 4)* |
| `POST` | `/sites/{siteId}/assets/{assetId}/commands` | Issue a command *(milestone 5)* |
| `GET` | `/sites/{siteId}/assets/{assetId}/commands` | Command history and outcomes *(milestone 5)* |
| `GET` | `/sites/{siteId}/alerts` | Open alerts *(milestone 6)* |
| `GET` | `/actuator/health` | Service health |

## 7. Data model

### Agent — local SQLite

```sql
asset_state(sensor_id TEXT PRIMARY KEY, condition TEXT, metrics_json TEXT, error TEXT,
            captured_at TEXT)
outbox(id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT, payload_json TEXT, created_at TEXT,
       published_at TEXT)
```

`asset_state` holds one row per sensor, overwritten each cycle: the machine's condition now,
which is what a `get_status` command answers from without waiting for the next sample.

`outbox` is the durability boundary. A row is written before any publish is attempted and
`published_at` is set only on broker confirmation, which makes delivery at-least-once — a crash
between publishing and confirming resends rather than drops. Confirmed rows are pruned on a
retention window rather than deleted immediately, because when telemetry looks wrong in the
cloud the first question is what the agent actually sent. The table is capped; past the cap the
oldest unpublished rows are dropped and counted, since an operator restoring a link after two
days wants the machine's state now, and silent loss would be indistinguishable from a healthy
quiet machine.

### Backend — PostgreSQL *(milestone 4)*

```sql
asset(id, site_id, asset_id, machine_type, firmware_version, health, registered_at, last_seen_at)
sensor(id, asset_id_fk, sensor_id, unit, hard_limit, enabled)
reading(id, asset_id_fk, sensor_id, metrics_jsonb, captured_at, received_at)
command(id, asset_id_fk, name, target, args_jsonb, status, issued_at, acked_at, completed_at,
        result_jsonb, error)
alert(id, asset_id_fk, kind, severity, detail, opened_at, closed_at)
```

## 8. Agent design

Implemented under `agent/factoryfleet_agent/`.

| Module | Responsibility |
|---|---|
| `main.py` | CLI, logging, signal handling |
| `agent.py` | Wires the subsystems and owns their lifecycle |
| `config.py` | Load and validate agent and sensor configuration files |
| `scheduler.py` | Interval ticker driving each sampling cycle |
| `sensors/base.py` | `Sensor` abstract base, `@register("type")` decorator, `Reading` value object |
| `sensors/simulation.py` | Baseline, noise, and drift shared by the simulated sensors |
| `sensors/vibration.py` | Vibration RMS and peak |
| `sensors/temperature.py` | Bearing and spindle temperature |
| `sensors/cycle_count.py` | Production cycles, rate, and machine uptime |
| `runner.py` | Run the sampling cycle, write state and outbox in one transaction |
| `store.py` | SQLite `asset_state` and `outbox` access |
| `wire.py` | Topic names and payload shapes |
| `timeutil.py` | UTC formatting shared by the store and the wire format |
| `transport/broker.py` | `BrokerClient` protocol and its MQTT implementation |
| `transport/publisher.py` | Drain outbox to MQTT, confirm before forgetting |
| `transport/subscriber.py` | Receive commands, hand to dispatcher *(milestone 5)* |
| `commands/dispatcher.py` | Route `sensor:<id>` to a sensor, bare names to global handlers *(milestone 5)* |
| `commands/handlers/` | `get_status`, `run_diagnostic`, `reload_config`, `restart_agent` *(milestone 5)* |

Two schedulers drive the agent — one sampling, one publishing — sharing nothing but the store.
That decoupling is what lets a broker outage stall publishing without stopping measurement: the
sampler holds no connection, so it cannot be blocked by one.

Sensors are simulated by default so the fleet can be demonstrated without physical hardware.
Each simulated reading is a machine-specific baseline plus cycle-to-cycle noise plus optional
drift, rather than a flat random value — a distinction that matters, because random values
would make the anomaly detector appear to work when it does not. With drift enabled a machine
climbs from 4.2 to roughly 8.9 mm/s over twelve hours while every reading stays far below the
15 mm/s hard limit, which is precisely the window §10 exists to catch.

The base class owns timestamping and error handling rather than each sensor: `read()` may raise
freely and the base turns the failure into an `ERROR` reading, so one seized sensor cannot end a
cycle and take its healthy neighbours with it.

## 9. Backend design

| Component | Responsibility |
|---|---|
| `asset` | Registry — registration upsert, fleet queries, health state transitions |
| `telemetry` | SQS consumer, reading persistence, last-seen updates |
| `command` | Command issue via IoT Data Plane, ack/result correlation, timeout sweep |
| `health` | Anomaly detection, staleness detection, alert lifecycle |
| `notification` | SNS publication for raised alerts |

## 10. Anomaly detection

For each `(asset, sensor, metric)` the backend maintains a rolling window of recent values and
computes mean and standard deviation. A new value scoring beyond `z_threshold` standard
deviations from that baseline raises an anomaly and moves the asset to `DEGRADED`; a value
beyond the sensor's configured hard limit moves it to `CRITICAL`.

This matters because a per-machine baseline catches problems an absolute threshold misses. A
press that normally runs at 4 mm/s vibration and climbs to 8 mm/s is in trouble, even though
8 mm/s is well inside a fleet-wide 15 mm/s red line. The baseline is per machine, so a
naturally rougher machine does not alert constantly and a naturally smooth one is not ignored.

Tunables: window size, `z_threshold`, minimum samples before the baseline is trusted, and a
per-sensor hard limit.

## 11. Security

- Per-asset X.509 certificate; IoT policy scoped to that asset's own topic prefix.
- Certificates are generated per asset and never committed — `.gitignore` excludes `*.pem`,
  `*.key`, `*.crt`, and `certs/`.
- Backend runs in private subnets; RDS is not publicly accessible; secrets come from AWS
  Secrets Manager rather than environment files.
- IAM roles are least-privilege per component: the backend may publish commands and read its
  own queues, and nothing more.
- Terraform state and `*.tfvars` are excluded from version control.

## 12. Local development

Milestone 2 runs the agent without AWS: a Mosquitto broker in Docker Compose stands in for IoT
Core. Both speak MQTT, so only addressing and authentication differ — milestone 3 switches the
transport to IoT Core by replacing `transport/broker.py`, and the agent's own logic does not
change.

The development broker runs with `persistence false` and anonymous access. Anonymous access
would be indefensible anywhere else, but this broker is bound to a developer's machine and holds
nothing; the real access model is §11. Persistence is off deliberately: the durability that
matters is the agent's outbox, and a broker that forgets on restart is a useful test of it.

## 13. Delivery milestones

| # | Milestone | Deliverable |
|---|---|---|
| 1 | Scaffold | Repository structure, this specification, backend skeleton, asset registry API, Terraform baseline |
| 2 | Agent | Sensor plugin framework, scheduler, SQLite outbox, publish to local Mosquitto |
| 3 | IoT Core | Terraform for Things, policies, certificates, Rules Engine to SQS; agent switched to IoT Core |
| 4 | Ingestion | SQS consumer, RDS Postgres persistence, readings API |
| 5 | Commands | Issue via REST, dispatch through IoT, agent execution, ack/result, timeout handling |
| 6 | Predictive maintenance | Rolling-baseline anomaly detection, staleness detection, SNS alerts |
| 7 | Deployment | ECS Fargate, GitHub Actions CI, end-to-end demonstration |
