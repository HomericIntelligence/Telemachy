"""Issue-backed Fleet registration; no local state can authorize issue creation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from telemachy.github_epic import EPIC_SUBJECT, epic_key
from telemachy.models import TaskSpec, WorkflowSpec

PREFIX = "<!-- telemachy:fleet-registration:v1 key="
END = "<!-- /telemachy:fleet-registration:v1 -->"


class RegistrationError(RuntimeError):
    """A registration is unconfirmed, conflicted, or requires reconciliation."""


class IssueGateway(Protocol):
    async def get_issue(self, repo: str, number: int) -> dict[str, Any]: ...
    async def update_issue(
        self, repo: str, number: int, expected_body: str, body: str
    ) -> dict[str, Any]: ...
    async def create_issue(self, repo: str, title: str, body: str) -> dict[str, Any]: ...
    async def find_issues(self, repo: str, marker: str) -> list[dict[str, Any]]: ...


class Receipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    stream: Literal["homeric-pipeline"]
    seq: int = Field(gt=0)
    duplicate: bool = False


class Child(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    identity: str
    team: str
    subject: str
    contentDigest: str
    phase: Literal["planned", "creating", "created"] = "planned"
    number: int | None = Field(default=None, gt=0)


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, populate_by_name=True)
    schema_name: Literal["hi/telemachy/fleet-registration/v1"] = Field(
        default="hi/telemachy/fleet-registration/v1", alias="schema"
    )
    registrationKey: str
    repo: str
    epic: int = Field(gt=0)
    workflowDigest: str
    createdAt: str
    phase: Literal["creating_children", "publish_pending", "published"] = "creating_children"
    children: list[Child]
    envelope: dict[str, Any] | None = None
    receipt: Receipt | None = None


Publisher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def canonical(value: Any) -> str:
    """One canonical encoding for workflow hashes, records, and wire bytes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def registration_marker(key: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", key):
        raise RegistrationError("invalid_registration_key")
    return f"{PREFIX}{key} -->"


def registration_template(key: str) -> str:
    """Return the explicit opt-in block for a separately created/reviewed epic."""
    return registration_marker(key) + "\n" + END


def _bounds(body: str, key: str) -> tuple[int, int]:
    marker = registration_marker(key)
    if body.count(PREFIX) != 1 or body.count(END) != 1 or body.count(marker) != 1:
        raise RegistrationError("registration_marker_missing_or_conflicting")
    start, end = body.index(marker), body.index(END)
    if end < start:
        raise RegistrationError("registration_marker_invalid_order")
    return start, end + len(END)


def _block(record: Registration) -> str:
    lines = [
        registration_marker(record.registrationKey),
        "```json",
        canonical(record.model_dump(by_alias=True, mode="json")),
        "```",
        "",
        "## Fleet task issues",
    ]
    lines.extend(f"- [ ] #{child.number}" for child in record.children if child.number is not None)
    lines.append(END)
    return "\n".join(lines)


def _read(body: str, key: str) -> Registration | None:
    start, end = _bounds(body, key)
    content = body[start + len(registration_marker(key)) : end - len(END)].strip()
    if not content:
        return None
    match = re.match(r"```json\n(.*?)\n```", content, re.DOTALL)
    if not match:
        raise RegistrationError("registration_record_invalid")
    try:
        record = Registration.model_validate(json.loads(match[1]))
    except (ValueError, ValidationError) as exc:
        raise RegistrationError("registration_record_invalid") from exc
    if body[start:end] != _block(record):
        raise RegistrationError("registration_record_modified")
    if record.phase == "creating_children":
        if record.envelope is not None or record.receipt is not None:
            raise RegistrationError("publication_record_invalid")
    elif (
        record.envelope is None
        or any(child.phase != "created" for child in record.children)
        or (record.phase == "published") != (record.receipt is not None)
    ):
        raise RegistrationError("publication_record_invalid")
    return record


def _issue(value: dict[str, Any], expected_number: int | None = None) -> dict[str, Any]:
    if (
        type(value.get("number")) is not int
        or value["number"] <= 0
        or not isinstance(value.get("body"), str)
        or "pull_request" in value
        or (expected_number is not None and value["number"] != expected_number)
    ):
        raise RegistrationError("issue_response_unconfirmed")
    return value


def _plan(spec: WorkflowSpec, repo: str, epic: int) -> list[tuple[Child, TaskSpec]]:
    result: list[tuple[Child, TaskSpec]] = []
    teams: set[str] = set()
    for team in sorted(spec.teams, key=lambda item: item.name):
        if team.name in teams:
            raise RegistrationError("ambiguous_team_identity")
        teams.add(team.name)
        remaining = {task.subject: task for task in team.tasks}
        while remaining:
            ready = sorted(
                name
                for name, task in remaining.items()
                if all(dep not in remaining for dep in task.blocked_by)
            )
            if not ready:
                raise RegistrationError("invalid_dependency_graph")
            for subject in ready:
                task = remaining.pop(subject)
                result.append(
                    (
                        Child(
                            identity=digest([repo, epic, team.name, subject]),
                            team=team.name,
                            subject=subject,
                            contentDigest=digest(task.model_dump(mode="json")),
                        ),
                        task,
                    )
                )
    return result


def _child_body(child: Child, task: TaskSpec, record: Registration) -> tuple[str, str]:
    marker = (
        f"<!-- telemachy:fleet-child:v1 {child.identity} "
        f"digest={child.contentDigest} epic={record.epic} -->"
    )
    numbers = {(item.team, item.subject): item.number for item in record.children}
    lines = [marker, "", task.description.strip(), "", f"_Assigned role: `{task.assign_to}`._"]
    for dependency in task.blocked_by:
        number = numbers[(child.team, dependency)]
        if number is None:
            raise RegistrationError("dependency_issue_unconfirmed")
        lines.append(f"Depends on #{number}")
    return marker, "\n".join(lines)


async def register_fleet_epic(
    spec: WorkflowSpec,
    *,
    repo: str,
    epic_issue: int,
    registration_key: str,
    github: IssueGateway,
    publish: Publisher,
) -> dict[str, Any]:
    """Register through one exclusive writer for an explicitly marked existing epic.

    GitHub issue PATCH has no atomic compare-and-swap contract. The caller must
    provide exclusive registration ownership; changed observed bodies are refused.
    A create-requested phase never permits another create on a later invocation.
    """
    if (
        not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)
        or type(epic_issue) is not int
        or epic_issue <= 0
    ):
        raise RegistrationError("invalid_epic_reference")
    repo = repo.lower()
    issue = _issue(await github.get_issue(repo, epic_issue), epic_issue)
    workflow_digest = digest(spec.model_dump(mode="json", by_alias=True))
    planned = _plan(spec, repo, epic_issue)
    record = _read(issue["body"], registration_key)

    async def save(updated: Registration) -> Registration:
        nonlocal issue
        start, end = _bounds(issue["body"], registration_key)
        body = issue["body"][:start] + _block(updated) + issue["body"][end:]
        if len(body.encode()) > 60000:
            raise RegistrationError("registration_body_capacity")
        response = _issue(
            await github.update_issue(repo, epic_issue, issue["body"], body), epic_issue
        )
        if response["body"] != body:
            raise RegistrationError("registration_write_unconfirmed")
        issue = response
        return updated

    if record is None:
        record = await save(
            Registration(
                registrationKey=registration_key,
                repo=repo,
                epic=epic_issue,
                workflowDigest=workflow_digest,
                createdAt=datetime.now(UTC).isoformat(),
                children=[child for child, _ in planned],
            )
        )
    if (
        record.repo != repo
        or record.epic != epic_issue
        or record.registrationKey != registration_key
        or record.workflowDigest != workflow_digest
    ):
        raise RegistrationError("workflow_conflict")
    expected = [child.model_dump(exclude={"phase", "number"}) for child, _ in planned]
    actual = [child.model_dump(exclude={"phase", "number"}) for child in record.children]
    if expected != actual:
        raise RegistrationError("workflow_conflict")

    for index, (_, task) in enumerate(planned):
        child = record.children[index]
        marker, body = _child_body(child, task, record)
        if child.phase == "created":
            if child.number is None:
                raise RegistrationError("child_record_invalid")
            confirmed = _issue(await github.get_issue(repo, child.number), child.number)
            if confirmed["body"] != body:
                raise RegistrationError("child_content_conflict")
            continue
        if child.number is not None or record.phase != "creating_children":
            raise RegistrationError("child_record_invalid")
        matches = await github.find_issues(repo, marker)
        if len(matches) > 1:
            raise RegistrationError("duplicate_child_identity")
        if matches:
            confirmed = _issue(matches[0])
            if confirmed["body"] != body or confirmed["number"] == epic_issue:
                raise RegistrationError("child_content_conflict")
        elif child.phase == "creating":
            raise RegistrationError("unresolved_create_requires_reconciliation")
        else:
            updated = record.model_copy(deep=True)
            updated.children[index].phase = "creating"
            record = await save(updated)
            confirmed = _issue(await github.create_issue(repo, task.subject, body))
            if confirmed["body"] != body or confirmed["number"] == epic_issue:
                raise RegistrationError("child_create_unconfirmed")
        updated = record.model_copy(deep=True)
        updated.children[index].number = confirmed["number"]
        updated.children[index].phase = "created"
        record = await save(updated)

    key = epic_key(repo, epic_issue)
    subject = EPIC_SUBJECT.format(epic_key=key)
    payload = {
        "schema": "hi/v1",
        "ts": record.createdAt,
        "msg_id": "telemachy-fleet-" + digest([repo, epic_issue, workflow_digest]),
        "epic": {"repo": repo, "issue": epic_issue, "key": key},
        "children": sorted(child.number for child in record.children if child.number is not None),
        "workflow": spec.name,
    }
    if record.envelope is not None and record.envelope != payload:
        raise RegistrationError("publication_envelope_conflict")
    if record.phase == "published":
        if record.receipt is None or record.envelope is None:
            raise RegistrationError("publication_receipt_missing")
    else:
        if record.receipt is not None:
            raise RegistrationError("publication_record_invalid")
        if record.phase != "publish_pending":
            updated = record.model_copy(deep=True)
            updated.phase = "publish_pending"
            updated.envelope = payload
            record = await save(updated)
        try:
            receipt = Receipt.model_validate(await publish(subject, payload))
        except ValidationError as exc:
            raise RegistrationError("jetstream_puback_unconfirmed") from exc
        updated = record.model_copy(deep=True)
        updated.phase = "published"
        updated.receipt = receipt
        record = await save(updated)
    return {
        "schema": "hi/telemachy/fleet-registration-result/v1",
        "epic": epic_issue,
        "repo": repo,
        "key": key,
        "subject": subject,
        "phase": record.phase,
        "children": {child.identity: child.number for child in record.children},
        "receipt": record.receipt.model_dump() if record.receipt else None,
    }
