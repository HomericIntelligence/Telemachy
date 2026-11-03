# ADR-002: Decouple from ai-maestro; use ProjectAgamemnon exclusively

- **Status:** Accepted (the `maestro_client.py` stub was removed in 0.2.0; the decision is unchanged)
- **Date:** 2026-05-16
- **Deciders:** Telemachy maintainers
- **Context tags:** orchestration, Agamemnon, maestro

## Context

Telemachy originally supported two backends:

1. ai-maestro — the previous orchestrator in the HomericIntelligence
   ecosystem (see Odysseus `docs/adr/006-decouple-from-ai-maestro.md`).
2. ProjectAgamemnon — the current planning + HMAS orchestration service.

Maintaining two backends doubled the surface area of
`agamemnon_client.py` / `maestro_client.py` and pushed schema decisions
upstream into the workflow YAML. Per the Odysseus ecosystem-level ADR-006,
ai-maestro has been removed from the mesh.

## Decision

Telemachy targets **ProjectAgamemnon exclusively** as its execution
backend. The `maestro_client.py` module was retained only as a thin
deprecation stub and was removed in 0.2.0.

All new code paths must go through `agamemnon_client.py`. New features
that imply a backend change must add a new ADR superseding this one.

## Consequences

Positive:

- Single client to test and document.
- Clear handoff contract documented in `AGENTS.md`.
- Aligns with Odysseus ADR-006.

Negative:

- No fallback if Agamemnon is unreachable; Telemachy fails closed.
- Migration path required for any caller still depending on the
  maestro-shaped contract (handled via deprecation warnings during the
  pre-1.0 window).

## Alternatives considered

- Keep both backends behind a feature flag. Rejected — increases the
  testing matrix and conflicts with Odysseus ADR-006.

## References

- `CLAUDE.md` — describes Agamemnon-exclusive execution.
- Odysseus `docs/adr/006-decouple-from-ai-maestro.md`.
- `src/telemachy/agamemnon_client.py` (single backend client).
