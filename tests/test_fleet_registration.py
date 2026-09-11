"""Durable producer authority tests; all GitHub writes are controlled fixtures."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from telemachy.fleet_registration import Publisher as PublishFunction
from telemachy.fleet_registration import register_fleet_epic
from telemachy.models import WorkflowSpec
from tests.conftest import make_two_task_dep_dict

START = "<!-- telemachy:fleet-registration:v1 key=reviewed-idea -->"
END = "<!-- /telemachy:fleet-registration:v1 -->"
BODY = (
    "Existing reviewed requirements.\n\n" + START + "\n" + END + "\n\nHuman discussion stays here."
)


class FakeGitHub:
    def __init__(self) -> None:
        self.issues: dict[int, dict[str, Any]] = {
            42: {"number": 42, "title": "Reviewed epic", "body": BODY, "state": "open"}
        }
        self.creates = 0
        self.writes = 0
        self.fail_write = 0
        self.lose_write_response = 0
        self.lose_create_response = False
        self.hide_created = False

    async def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        return copy.deepcopy(self.issues[number])

    async def update_issue(
        self, repo: str, number: int, expected_body: str, body: str
    ) -> dict[str, Any]:
        self.writes += 1
        if self.writes == self.fail_write:
            raise RuntimeError("write unconfirmed")
        assert self.issues[number]["body"] == expected_body
        self.issues[number]["body"] = body
        if self.writes == self.lose_write_response:
            raise RuntimeError("write response lost")
        return copy.deepcopy(self.issues[number])

    async def create_issue(self, repo: str, title: str, body: str) -> dict[str, Any]:
        assert '"phase":"creating"' in self.issues[42]["body"]
        self.creates += 1
        number = 100 + self.creates
        self.issues[number] = {"number": number, "title": title, "body": body, "state": "open"}
        if self.lose_create_response:
            self.lose_create_response = False
            raise RuntimeError("create response lost")
        return copy.deepcopy(self.issues[number])

    async def find_issues(self, repo: str, marker: str) -> list[dict[str, Any]]:
        if self.hide_created:
            return []
        return [copy.deepcopy(issue) for issue in self.issues.values() if marker in issue["body"]]


class Publisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = False
        self.receipt: Any = {"stream": "homeric-pipeline", "seq": 1, "duplicate": False}

    async def __call__(self, subject: str, payload: dict[str, Any]) -> Any:
        self.calls.append((subject, copy.deepcopy(payload)))
        if self.fail:
            raise RuntimeError("PubAck lost")
        return copy.deepcopy(self.receipt)


async def run(
    gh: FakeGitHub, publisher: PublishFunction, spec: WorkflowSpec | None = None
) -> dict[str, Any]:
    return await register_fleet_epic(
        spec or WorkflowSpec.model_validate(make_two_task_dep_dict()),
        repo="Homeric/repo",
        epic_issue=42,
        registration_key="reviewed-idea",
        github=gh,
        publish=publisher,
    )


async def test_success_and_restart_reuse_existing_epic_and_children() -> None:
    gh, publisher = FakeGitHub(), Publisher()
    first = await run(gh, publisher)
    assert first["epic"] == 42
    assert first["phase"] == "published"
    assert gh.creates == 2
    assert len(publisher.calls) == 1
    assert publisher.calls[0][1]["children"] == [101, 102]
    assert gh.issues[42]["body"].startswith("Existing reviewed requirements.")
    assert gh.issues[42]["body"].endswith("Human discussion stays here.")
    assert await run(gh, publisher) == first
    assert gh.creates == 2
    assert len(publisher.calls) == 1


async def test_failed_intent_write_cannot_create_or_publish() -> None:
    gh, publisher = FakeGitHub(), Publisher()
    gh.fail_write = 2
    with pytest.raises(RuntimeError):
        await run(gh, publisher)
    assert gh.writes == 2
    assert gh.creates == 0
    assert publisher.calls == []


async def test_lost_create_response_reconciles_closed_child_without_recreating() -> None:
    gh, publisher = FakeGitHub(), Publisher()
    gh.lose_create_response = True
    with pytest.raises(RuntimeError):
        await run(gh, publisher)
    assert gh.creates == 1
    gh.issues[101]["state"] = "closed"
    await run(gh, publisher)
    assert gh.creates == 2
    assert publisher.calls[0][1]["children"] == [101, 102]


async def test_unresolved_creation_is_never_reissued_from_empty_listing() -> None:
    gh, publisher = FakeGitHub(), Publisher()
    gh.lose_create_response = True
    with pytest.raises(RuntimeError):
        await run(gh, publisher)
    gh.hide_created = True
    with pytest.raises(RuntimeError, match="unresolved_create"):
        await run(gh, publisher)
    assert gh.creates == 1
    assert publisher.calls == []


async def test_puback_loss_retries_exact_envelope_without_recreating() -> None:
    gh, publisher = FakeGitHub(), Publisher()
    publisher.fail = True
    with pytest.raises(RuntimeError):
        await run(gh, publisher)
    assert gh.creates == 2
    publisher.fail = False
    await run(gh, publisher)
    assert gh.creates == 2
    assert len(publisher.calls) == 2
    assert publisher.calls[0] == publisher.calls[1]


async def test_unrelated_body_and_changed_frozen_workflow_are_rejected() -> None:
    gh, publisher = FakeGitHub(), Publisher()
    gh.issues[42]["body"] = "Unrelated human issue."
    with pytest.raises(RuntimeError, match="registration_marker"):
        await run(gh, publisher)
    assert gh.writes == 0
    gh.issues[42]["body"] = BODY
    await run(gh, publisher)
    changed = make_two_task_dep_dict()
    changed["metadata"]["description"] = "Changed research requirements"
    with pytest.raises(RuntimeError, match="workflow_conflict"):
        await run(gh, publisher, WorkflowSpec.model_validate(changed))
    assert gh.creates == 2
    assert len(publisher.calls) == 1


@pytest.mark.parametrize("change", ["missing_envelope", "creating_with_receipt"])
async def test_inconsistent_persisted_phase_cannot_publish(change: str) -> None:
    gh, publisher = FakeGitHub(), Publisher()
    await run(gh, publisher)
    body = gh.issues[42]["body"]
    prefix, tail = body.split("```json\n", 1)
    encoded, suffix = tail.split("\n```", 1)
    record = json.loads(encoded)
    if change == "missing_envelope":
        record.update(phase="publish_pending", envelope=None, receipt=None)
    else:
        record.update(phase="creating_children", envelope=None)
    gh.issues[42]["body"] = (
        prefix
        + "```json\n"
        + json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n```"
        + suffix
    )
    publisher.calls.clear()
    with pytest.raises(RuntimeError, match="publication_record_invalid"):
        await run(gh, publisher)
    assert publisher.calls == []
    assert gh.creates == 2


async def test_core_publish_without_puback_cannot_mark_registration_published() -> None:
    gh, publisher = FakeGitHub(), Publisher()
    publisher.receipt = None
    with pytest.raises(RuntimeError, match="jetstream_puback_unconfirmed"):
        await run(gh, publisher)
    assert '"phase":"publish_pending"' in gh.issues[42]["body"]
    assert '"phase":"published"' not in gh.issues[42]["body"]


async def test_lost_intent_write_response_cannot_authorize_another_create() -> None:
    gh, publisher = FakeGitHub(), Publisher()
    gh.lose_write_response = 2
    with pytest.raises(RuntimeError, match="write response lost"):
        await run(gh, publisher)
    with pytest.raises(RuntimeError, match="unresolved_create"):
        await run(gh, publisher)
    assert gh.creates == 0
    assert publisher.calls == []


async def test_lost_child_receipt_write_rehydrates_before_next_create() -> None:
    gh, publisher = FakeGitHub(), Publisher()
    gh.lose_write_response = 3
    with pytest.raises(RuntimeError, match="write response lost"):
        await run(gh, publisher)
    assert gh.creates == 1
    await run(gh, publisher)
    assert gh.creates == 2
    assert publisher.calls[0][1]["children"] == [101, 102]
