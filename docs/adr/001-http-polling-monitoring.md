# ADR-001: Monitor workflow completion via HTTP polling

- **Status:** Superseded by event-driven NATS monitoring (#251, #3)
- **Date:** 2026-05-16
- **Deciders:** Telemachy maintainers
- **Context tags:** monitoring, NATS, executor

## Context

Telemachy's `WorkflowExecutor` needs to detect when assigned tasks
complete, so it can release dependent tasks and ultimately tear the
workflow down per the declared teardown policy.

Two approaches were available:

1. **NATS event subscription** — subscribe to completion events emitted by
   ProjectAgamemnon. Real-time, low overhead, but requires a NATS broker
   reachable from the Telemachy host and adds an asynchronous dependency
   on a separate transport.
2. **HTTP polling** — repeatedly call ProjectAgamemnon's REST API to query
   task state. Higher latency and overhead, but uses the same transport
   already required for provisioning, so no new infrastructure or library
   needs to be wired up.

`CLAUDE.md` describes NATS as "planned but not yet wired up"; `nats-py`
is declared as a dependency but not imported. The initial implementation
chose HTTP polling.

## Decision

Telemachy monitors task completion by polling
ProjectAgamemnon's REST API at the interval defined by
`MONITOR_TIMEOUT_SECONDS` / `MONITOR_MAX_POLLS`. The NATS-based monitor
path remains aspirational and is tracked by repository issues; this ADR
will be superseded when a NATS-based design is accepted.

## Consequences

Positive:

- One transport (HTTP) for all Agamemnon interactions; simpler
  deployment.
- No NATS broker required on the Telemachy host.
- Easier testing — the executor is straightforward to mock against
  `httpx.AsyncClient`.

Negative:

- Completion latency is bounded below by the poll interval.
- Wasted requests when nothing has changed.
- Adds load on ProjectAgamemnon proportional to the number of in-flight
  workflows.

## Alternatives considered

- Subscribe directly to NATS via `nats-py`. Rejected for the initial
  release because the broker contract was not stable across the
  ecosystem at the time. Revisit when ADR-005 in Odysseus (subject
  schema) is fully adopted by Agamemnon.

## References

- `src/telemachy/executor.py` (event-driven monitor)
- `src/telemachy/nats_monitor.py`
- `src/telemachy/agamemnon_client.py`
- Odysseus `docs/adr/005-nats-subject-schema.md`
