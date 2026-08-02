# FactoryFleet

Fleet health monitoring and remote diagnostics for plant-floor industrial equipment.

Manufacturing sites run hundreds of unattended machines — CNC mills, hydraulic presses,
conveyors, robot arms. When one starts to fail, the usual signal is a line stoppage: the
machine is already down and production is already lost. FactoryFleet gives maintenance teams
a single view of every machine across every site, streams vibration/temperature/cycle
telemetry off the plant floor, flags machines whose readings are drifting *before* they
fail, and lets an engineer run diagnostics or reset a machine remotely instead of walking
the floor.

> **Status:** in active development. Built in public, one milestone at a time — see the
> [roadmap](#roadmap) for what's landed and what's next.

## Why this exists

This is a portfolio project exploring an event-driven, cloud-native take on device fleet
management: a managed MQTT broker (AWS IoT Core) and queue-decoupled ingestion instead of a
hand-rolled WebSocket protocol, plus statistical anomaly detection on the telemetry stream
rather than static red-line thresholds.

## Architecture

```
Asset Agent (Python) — one per machine
  Sensors (vibration / temperature / cycle-count)
        │  scheduler polls each sensor
        ▼
  local SQLite  ──►  outbox  ──►  MQTT publish (X.509 mutual TLS)
  (survives network loss; nothing is dropped when the plant link drops)
        │
        ▼
AWS IoT Core  — managed MQTT broker + Rules Engine
  telemetry topic       ──► rule ──► SQS telemetry queue
  command-result topic  ──► rule ──► SQS result queue
  commands topic        ◄── backend publishes to a specific asset
        │
        ▼
Backend (Spring Boot on ECS Fargate)
  SQS consumers  ──►  RDS Postgres (asset registry, telemetry, command log)
  REST API       ──►  fleet status, asset detail, issue command
  Anomaly detector ──► rolling baseline per sensor → DEGRADED / CRITICAL
  Staleness job    ──► no telemetry in N minutes → OFFLINE → SNS alert
```

Why IoT Core and SQS rather than a direct connection: the broker owns per-device identity,
authentication and reconnection, and the queues decouple ingestion from the backend, so a
backend deploy or restart never drops plant-floor telemetry.

## Repository layout

| Path | Contents |
|---|---|
| `agent/` | Python agent that runs on (or simulates) a machine and publishes telemetry |
| `backend/` | Spring Boot service — asset registry, telemetry ingestion, command dispatch |
| `infra/terraform/` | Terraform for IoT Core, RDS, ECS, SQS, SNS |
| `infra/mosquitto/` | Local broker config, standing in for IoT Core during development |
| `docker-compose.yml` | Local development dependencies |
| `docs/` | [Functional and technical specification](docs/SPEC.md) |

## Tech stack

**Agent** — Python 3.11, `paho-mqtt`, SQLite
**Backend** — Java 21, Spring Boot 3.5, Spring Data JPA, PostgreSQL
**Infrastructure** — AWS IoT Core, SQS, RDS Postgres, ECS Fargate, SNS, Terraform
**Tooling** — Docker Compose for local development, GitHub Actions for CI

## Getting started

### Agent

Requires Python 3.11+ and Docker. Mosquitto stands in for AWS IoT Core, so this runs the whole
agent pipeline with no AWS account and no cost.

```bash
docker compose up -d mosquitto
```

```bash
cd agent
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m factoryfleet_agent
```

Watch what reaches the broker:

```bash
docker exec factoryfleet-mosquitto mosquitto_sub -h localhost -t 'factoryfleet/#' -v
```

Kill the broker while the agent runs and the readings queue in SQLite instead of being lost;
bring it back and they publish in order. See [`agent/README.md`](agent/README.md) for
configuration, the payload format, how to add a sensor, and how to simulate a degrading
machine.

### Backend

Requires Java 21+ and Maven.

```bash
cd backend
mvn spring-boot:run
```

The service starts on port 8080.

```bash
# register a machine
curl -X POST http://localhost:8080/api/v1/sites/PLANT-A/assets \
  -H 'Content-Type: application/json' \
  -d '{
        "assetId": "PRESS-01",
        "machineType": "HYDRAULIC_PRESS",
        "firmwareVersion": "2.4.1",
        "sensors": ["vibration", "temperature", "cycle-count"]
      }'

# list every machine at a site
curl http://localhost:8080/api/v1/sites/PLANT-A/assets

# service health
curl http://localhost:8080/actuator/health
```

The asset registry is in-memory at this milestone; PostgreSQL persistence lands with the
telemetry pipeline (see the roadmap).

### Infrastructure

```bash
cd infra/terraform
terraform init
terraform validate
```

Applying provisions real AWS resources — IoT Core things, certificates and policies, the
ingestion queues and rules. Costs are small (IoT Core bills per message, SQS requests sit
inside the free tier, and nothing here runs a server) but not zero, and `terraform destroy`
between sessions is the cheapest way to work. Read
[`infra/terraform/README.md`](infra/terraform/README.md) first — it covers the apply steps,
how to point an agent at IoT Core, the cost breakdown, and the caveat that certificate private
keys land in Terraform state.

## Roadmap

| # | Milestone | Status |
|---|---|---|
| 1 | Repository scaffold, specification, backend skeleton, asset registry API, Terraform baseline | ✅ done |
| 2 | Python agent — sensor plugins, scheduler, local SQLite outbox, MQTT publish to a local broker | ✅ done |
| 3 | AWS IoT Core — X.509 provisioning, Rules Engine, SQS telemetry queue in Terraform | ✅ done |
| 4 | Backend telemetry ingestion — SQS consumer, RDS Postgres persistence | planned |
| 5 | Remote commands — REST → IoT publish → agent executes → result → backend command log | planned |
| 6 | Predictive maintenance — rolling-baseline anomaly detection, offline detection, SNS alerts | planned |
| 7 | Deployment — ECS Fargate, GitHub Actions CI, end-to-end demo | planned |

## License

[MIT](LICENSE)
