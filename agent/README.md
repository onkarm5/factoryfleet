# Asset agent

The Python process that runs on (or simulates) one plant-floor machine: it samples the
machine's sensors on an interval, writes every reading to a local SQLite outbox, publishes
batches over MQTT, and executes commands sent down from the backend.

**Not yet implemented** — this arrives in milestone 2. The design it will follow is in
[`docs/SPEC.md`](../docs/SPEC.md): sensor plugin base class and registry, scheduler, SQLite
`asset_state` and `outbox` tables, publisher draining the outbox, and a command dispatcher.

Planned layout:

```
agent/
├── requirements.txt
├── config/
│   ├── agent.toml                 identity, broker endpoint, intervals
│   └── sensors/                   one file per sensor, added without touching code
├── src/
│   ├── main.py                    entry point
│   ├── config.py                  configuration loading
│   ├── scheduler.py               interval ticker
│   ├── store.py                   SQLite asset_state + outbox
│   ├── runner.py                  sampling cycle
│   ├── sensors/                   base + vibration, temperature, cycle_count
│   ├── transport/                 MQTT publisher and subscriber
│   └── commands/                  dispatcher + built-in handlers
└── tests/
```
