"""Tests for the shared epic key helpers (Odysseus ADR-013 §6)."""

from __future__ import annotations

from telemachy.github_epic import EPIC_SUBJECT, epic_key, slugify


def test_slugify_repo() -> None:
    assert slugify("HomericIntelligence/Odysseus") == "homericintelligence-odysseus"


def test_epic_key() -> None:
    assert epic_key("Homeric/Repo", 12) == "homeric-repo-12"


def test_epic_subject_round_trip() -> None:
    subject = EPIC_SUBJECT.format(epic_key=epic_key("Homeric/Repo", 12))
    assert subject == "hi.pipeline.epic.homeric-repo-12.registered"
