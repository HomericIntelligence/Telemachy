# Telemachy Roadmap

This roadmap is the authoritative planning artefact for Telemachy.
It captures **what is shipping now, what is shipping next, and what we
know is not yet built**. Every row links to an issue, an ADR, or both.

Conventions:

- Release lines follow `docs/backwards-compat.md` (SemVer; v0.x is pre-1.0).
- Each row links to a tracking issue (or notes _no issue yet_) and to
  any relevant ADR under `docs/adr/`.
- This file is regression-tested by `tests/test_roadmap.py`.

## Current Release (v0.1.x — Unreleased)

| Work | Status | Issue | ADR |
| --- | --- | --- | --- |
| HTTP-polling task monitoring | Superseded by event-driven monitoring | [#92](https://github.com/HomericIntelligence/Telemachy/issues/92) | [ADR-001](adr/001-http-polling-monitoring.md) |
| NATS subscriber for task-lifecycle events | Shipped | [#92](https://github.com/HomericIntelligence/Telemachy/issues/92) | ADR-001 superseded |
| Durable Fleet epic registration | Shipped | [#339](https://github.com/HomericIntelligence/Telemachy/issues/339) | — |
| Agamemnon-exclusive backend | Shipped | — | [ADR-002](adr/002-decouple-from-maestro.md) |
| Secure TLS default (`REQUIRE_TLS=true`) | Shipped | [#158](https://github.com/HomericIntelligence/Telemachy/issues/158) | — |
| Release workflow (PyPI + GH Release) | Shipped | [#153](https://github.com/HomericIntelligence/Telemachy/issues/153) | — |
| 75% coverage gate | Shipped | [#152](https://github.com/HomericIntelligence/Telemachy/issues/152) | — |

## Next Release (v0.2.0)

| Work | Status | Issue | ADR |
| --- | --- | --- | --- |
| Roadmap & milestones (this artefact) | In progress | [#167](https://github.com/HomericIntelligence/Telemachy/issues/167) | [ADR-003](adr/003-roadmap-artifact.md) |
| Retire the polling-era settings retained for compatibility | Planned | _no issue yet — file before starting work_ | — |

## Future (v1.0.0 and beyond)

| Work | Status | Issue | ADR |
| --- | --- | --- | --- |
| Stabilise workflow YAML to `apiVersion: telemachy/v1` (drop pre-1.0 caveats) | Pending v0.2.0 | _no issue yet_ | — |
| First-epic automated creation for Fleet intake (currently an unresolved gate) | Design not started | [#339](https://github.com/HomericIntelligence/Telemachy/issues/339) | — |

## Known Limitations

- Task-completion detection requires a reachable NATS broker; the executor
  fails closed (`NatsUnavailableError`) when the event bus is unavailable.
- Fleet registration requires an externally enforced single writer; GitHub
  issue PATCH is not an atomic compare-and-swap.
- `MONITOR_MAX_POLLS` and the `HEALTHCHECK_*` settings no longer affect
  behaviour; they are retained so existing environments keep parsing.

## How to propose a roadmap change

1. Open an issue with label `roadmap`.
2. If the change implies an architectural decision, draft an ADR under
   `docs/adr/` per `docs/adr/template.md`.
3. Reference the issue + ADR from the relevant row in this file.
