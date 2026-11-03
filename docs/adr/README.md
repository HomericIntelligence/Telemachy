# Architecture Decision Records

This directory holds Architecture Decision Records (ADRs) for
Telemachy. ADRs are append-only documents capturing **why** a
significant architectural choice was made.

## Convention

- ADRs are numbered sequentially: `001-<slug>.md`, `002-<slug>.md`, …
- Use `template.md` as the starting point.
- New ADRs are *Proposed* until merged; once accepted they are never
  edited. Superseding decisions get a new ADR that references the old.
- The format mirrors the Odysseus meta-repo's ADR convention.

## Index

| Number | Title | Status |
| --- | --- | --- |
| 001 | Monitoring via HTTP polling instead of NATS events | Superseded by event-driven monitoring |
| 002 | Decouple from ai-maestro (use Agamemnon exclusively) | Accepted |
| 003 | `docs/ROADMAP.md` as the canonical planning artefact | Accepted |
| [004](004-pii-handling-stance.md) | PII handling stance for workflow text | Accepted |

## See also

- `docs/backwards-compat.md`
- Odysseus `docs/adr/` (ecosystem-wide ADRs)
