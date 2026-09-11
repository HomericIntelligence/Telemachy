"""GitHub REST interface for issue-backed Fleet registration."""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from telemachy.fleet_registration import RegistrationError, _issue


class GitHubIssueGateway:
    def __init__(
        self, token: str, *, transport: httpx.AsyncBaseTransport | None = None, max_pages: int = 100
    ) -> None:
        if not token:
            raise RegistrationError("github_authentication_required")
        if not 1 <= max_pages <= 1000:
            raise RegistrationError("invalid_pagination_bound")
        self.max_pages = max_pages
        self.client = httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=transport,
            timeout=15,
            follow_redirects=False,
            trust_env=False,
            headers={
                "Authorization": "Bearer " + token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    async def __aenter__(self) -> GitHubIssueGateway:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.client.aclose()

    @staticmethod
    def _path(repo: str, number: int | None = None) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise RegistrationError("invalid_repository")
        if number is not None and (type(number) is not int or number <= 0):
            raise RegistrationError("invalid_issue_number")
        path = f"/repos/{repo}/issues"
        return path if number is None else path + f"/{number}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        code = "github_read_unconfirmed" if method == "GET" else "github_write_unconfirmed"
        try:
            response = await self.client.request(method, path, **kwargs)
            if response.status_code != (201 if method == "POST" else 200):
                raise RegistrationError(code)
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Never retry mutations here: the durable caller owns reconciliation.
            raise RegistrationError(code) from exc

    async def get_issue(self, repo: str, number: int) -> dict[str, Any]:
        return _issue(await self._request("GET", self._path(repo, number)), number)

    async def update_issue(
        self, repo: str, number: int, expected_body: str, body: str
    ) -> dict[str, Any]:
        current = await self.get_issue(repo, number)
        if current["body"] != expected_body:
            raise RegistrationError("epic_body_changed")
        # This read check is deliberately not advertised as atomic GitHub CAS.
        # The registration API requires an externally exclusive writer.
        response = _issue(
            await self._request("PATCH", self._path(repo, number), json={"body": body}), number
        )
        if response["body"] != body:
            raise RegistrationError("github_write_unconfirmed")
        return response

    async def create_issue(self, repo: str, title: str, body: str) -> dict[str, Any]:
        # Hephaestus owns state:* labels; this producer never changes them.
        return _issue(
            await self._request("POST", self._path(repo), json={"title": title, "body": body})
        )

    async def find_issues(self, repo: str, marker: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        deadline = time.monotonic() + 30
        for page in range(1, self.max_pages + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RegistrationError("pagination_incomplete")
            values = await self._request(
                "GET",
                self._path(repo),
                params={"state": "all", "per_page": 100, "page": page},
                timeout=min(15, remaining),
            )
            if not isinstance(values, list):
                raise RegistrationError("github_read_unconfirmed")
            for value in values:
                if not isinstance(value, dict):
                    raise RegistrationError("github_read_unconfirmed")
                if "pull_request" not in value and marker in (value.get("body") or ""):
                    matches.append(_issue(value))
            if len(values) < 100:
                return matches
        raise RegistrationError("pagination_incomplete")
