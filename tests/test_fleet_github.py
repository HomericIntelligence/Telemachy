"""Real httpx calls through a controlled GitHub transport, with no external requests."""

from collections.abc import Callable

import httpx
import pytest

from telemachy.fleet_github import GitHubIssueGateway


def make_github_client_for(
    handler: Callable[[httpx.Request], httpx.Response], *, max_pages: int = 100
) -> GitHubIssueGateway:
    return GitHubIssueGateway(
        "fixture-only", transport=httpx.MockTransport(handler), max_pages=max_pages
    )


async def test_changed_epic_body_stops_before_patch() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(200, json={"number": 42, "body": "Other writer changed this"})

    async with make_github_client_for(handler) as client:
        with pytest.raises(RuntimeError, match="body_changed"):
            await client.update_issue("Homeric/repo", 42, "Original", "Replacement")
    assert methods == ["GET"]


async def test_unconfirmed_patch_is_not_retried() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET":
            return httpx.Response(200, json={"number": 42, "body": "Original"})
        return httpx.Response(503, json={"message": "unavailable"})

    async with make_github_client_for(handler) as client:
        with pytest.raises(RuntimeError, match="github_write_unconfirmed"):
            await client.update_issue("Homeric/repo", 42, "Original", "Replacement")
    assert methods == ["GET", "PATCH"]


async def test_closed_marker_is_found_after_first_page() -> None:
    pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["state"] == "all"
        page = request.url.params["page"]
        pages.append(page)
        if page == "1":
            return httpx.Response(200, json=[{"number": n, "body": "other"} for n in range(1, 101)])
        return httpx.Response(
            200, json=[{"number": 123, "body": "exact-marker", "state": "closed"}]
        )

    async with make_github_client_for(handler) as client:
        assert (await client.find_issues("Homeric/repo", "exact-marker"))[0]["number"] == 123
    assert pages == ["1", "2"]


async def test_incomplete_pagination_cannot_prove_absence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"number": n, "body": "other"} for n in range(1, 101)])

    async with make_github_client_for(handler, max_pages=1) as client:
        with pytest.raises(RuntimeError, match="pagination_incomplete"):
            await client.find_issues("Homeric/repo", "absent-marker")
