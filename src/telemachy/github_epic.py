"""Shared epic identity for Odysseus ADR-013 §6 registration.

``epic_key`` and ``EPIC_SUBJECT`` are the single source of truth for the
subject token and trigger subject. Registration itself lives in
:mod:`telemachy.fleet_registration`.
"""

from __future__ import annotations

import re

EPIC_SUBJECT = "hi.pipeline.epic.{epic_key}.registered"

_TOKEN_RE = re.compile(r"[^a-z0-9-]+")


def slugify(value: str) -> str:
    """Slugify *value* into a single NATS subject token (ADR-005 rules)."""
    return _TOKEN_RE.sub("-", value.lower()).strip("-")


def epic_key(repo: str, issue_number: int) -> str:
    """Build the ADR-013 §6 epic key ``{repo_slug}-{issue_number}``."""
    return f"{slugify(repo)}-{issue_number}"
