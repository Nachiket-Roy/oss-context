"""GitHub API client logic for oss-context.

This module handles REST and GraphQL requests, retries, rate-limit handling,
pagination, and translation of GitHub pull-request, review, and issue data into
local Pydantic models used by the sync pipeline.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

import httpx

from oss_context.models import (
    IssueData,
    PullRequestData,
    RepoRef,
    ReviewCommentData,
    ReviewThreadData,
    IssueCommentData,
)
from oss_context.settings import Settings


class GitHubApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        response_text: str | None = None,
        operation: str | None = None,
        repo: str | None = None,
    ):
        super().__init__(message)
        self.http_status = http_status
        self.response_text = response_text
        self.operation = operation
        self.repo = repo


GraphQLPayload = dict[str, Any]


def parse_github_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class GitHubClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "oss-context/0.1.0",
        }
        if settings.github_token:
            headers["Authorization"] = f"Bearer {settings.github_token}"
        self.client = httpx.AsyncClient(headers=headers, timeout=settings.request_timeout_seconds)
        self.pr_total_estimate: int | None = None
        self.issue_total_estimate: int | None = None

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.client.aclose()

    async def _request(
        self,
        method: str,
        url: str,
        *,
        operation: str | None = None,
        repo: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        target = url if url.startswith("http") else f"{self.settings.github_api_url}{url}"
        backoff_seconds = 1.0
        last_error: Exception | None = None

        for attempt in range(3):
            try:
                response = await self.client.request(method, target, **kwargs)
                if (
                    response.status_code == 403
                    and response.headers.get("x-ratelimit-remaining") == "0"
                    and attempt < 2
                ):
                    reset_at = response.headers.get("x-ratelimit-reset")
                    if reset_at:
                        sleep_for = max(0, int(reset_at) - int(datetime.now(UTC).timestamp()))
                        await asyncio.sleep(min(sleep_for, 60))
                        continue

                if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    await asyncio.sleep(backoff_seconds)
                    backoff_seconds *= 2
                    continue

                response.raise_for_status()
                return response
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    if status not in {429, 500, 502, 503, 504} or attempt == 2:
                        detail = exc.response.text
                        is_graphql = "graphql" in str(target).lower()
                        api_type = "GraphQL" if is_graphql else "API"
                        message = f"GitHub {api_type} request failed ({status})"
                        if detail:
                            message += f": {detail}"
                        raise GitHubApiError(
                            message,
                            http_status=status,
                            response_text=detail,
                            operation=operation,
                            repo=repo,
                        ) from exc
                if attempt < 2:
                    await asyncio.sleep(backoff_seconds)
                    backoff_seconds *= 2

        is_graphql = "graphql" in str(target).lower()
        api_type = "GraphQL" if is_graphql else "API"
        status_code = None
        response_text = str(last_error)
        if last_error and isinstance(last_error, httpx.HTTPStatusError):
            status_code = last_error.response.status_code
            response_text = last_error.response.text
        message = f"GitHub {api_type} request failed"
        if status_code:
            message += f" ({status_code})"
        if response_text:
            message += f": {response_text}"
        raise GitHubApiError(
            message,
            http_status=status_code,
            response_text=response_text,
            operation=operation,
            repo=repo,
        ) from last_error

    async def _graphql(
        self,
        query: str,
        variables: GraphQLPayload,
        *,
        operation: str | None = None,
        repo: str | None = None,
    ) -> GraphQLPayload:
        response = await self._request(
            "POST",
            self.settings.github_graphql_url,
            headers={"Content-Type": "application/json"},
            json={"query": query, "variables": variables},
            operation=operation,
            repo=repo,
        )
        payload = response.json()
        if payload.get("errors"):
            raise GitHubApiError(
                f"GitHub GraphQL error: {payload['errors']}",
                http_status=response.status_code,
                response_text=response.text,
                operation=operation,
                repo=repo,
            )
        return payload["data"]

    def _parse_review_comment(self, comment: GraphQLPayload) -> ReviewCommentData | None:
        database_id = comment.get("databaseId")
        if database_id is None:
            return None

        reaction_groups = comment.get("reactionGroups") or []
        reaction_count = sum(
            ((group.get("users") or {}).get("totalCount") or 0) for group in reaction_groups
        )
        body = comment.get("body") or ""
        return ReviewCommentData(
            github_comment_id=int(database_id),
            author=(comment.get("author") or {}).get("login"),
            body=body,
            created_at=parse_github_datetime(comment.get("createdAt")),
            updated_at=parse_github_datetime(comment.get("updatedAt")),
            reaction_count=reaction_count,
            is_suggestion="```suggestion" in body,
            suggestion_applied=False,
        )

    async def _fetch_remaining_thread_comments(
        self,
        thread_id: str,
        start_cursor: str,
        *,
        repo: RepoRef | None = None,
    ) -> list[ReviewCommentData]:
        query = """
        query ReviewThreadComments($threadId: ID!, $cursor: String) {
          node(id: $threadId) {
            ... on PullRequestReviewThread {
              comments(first: 100, after: $cursor) {
                pageInfo {
                  hasNextPage
                  endCursor
                }
                nodes {
                  databaseId
                  author {
                    login
                  }
                  body
                  createdAt
                  updatedAt
                  reactionGroups {
                    users {
                      totalCount
                    }
                  }
                }
              }
            }
          }
        }
        """

        comments: list[ReviewCommentData] = []
        cursor: str | None = start_cursor

        while True:
            data = await self._graphql(
                query,
                {"threadId": thread_id, "cursor": cursor},
                operation="fetch_review_threads",
                repo=repo.slug if repo else None,
            )
            node = data.get("node") or {}
            comments_connection = node.get("comments") or {}
            for comment_node in comments_connection.get("nodes") or []:
                parsed = self._parse_review_comment(comment_node)
                if parsed is not None:
                    comments.append(parsed)

            page_info = comments_connection.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")

        return comments

    async def get_repo(self, repo: RepoRef) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}",
            operation="get_repo",
            repo=repo.slug,
        )
        return response.json()

    async def iter_pull_requests(
        self,
        repo: RepoRef,
        since: datetime | None = None,
    ) -> AsyncIterator[PullRequestData]:
        next_url = (
            f"/repos/{repo.owner}/{repo.name}/pulls"
            "?state=all&sort=updated&direction=desc&per_page=100"
        )
        self.pr_total_estimate = None
        is_first = True

        while next_url:
            response = await self._request(
                "GET",
                next_url,
                operation="iter_pull_requests",
                repo=repo.slug,
            )
            if is_first:
                last_url = response.links.get("last", {}).get("url")
                if last_url:
                    from urllib.parse import parse_qs, urlparse
                    parsed = urlparse(last_url)
                    pages = parse_qs(parsed.query).get("page")
                    if pages:
                        try:
                            self.pr_total_estimate = int(pages[0]) * 100
                        except ValueError:
                            pass
                is_first = False

            items = response.json()
            cutoff_reached = False
            for item in items:
                updated_at = parse_github_datetime(item.get("updated_at"))
                if since and updated_at and updated_at <= since:
                    cutoff_reached = True
                    continue
                yield PullRequestData(
                    github_id=item["id"],
                    number=item["number"],
                    title=item["title"],
                    state=item["state"],
                    author=(item.get("user") or {}).get("login"),
                    created_at=parse_github_datetime(item.get("created_at")),
                    updated_at=updated_at,
                    body=item.get("body"),
                    base_branch=(item.get("base") or {}).get("ref"),
                    head_branch=(item.get("head") or {}).get("ref"),
                    merge_commit_sha=item.get("merge_commit_sha"),
                    labels=[
                        label["name"]
                        for label in item.get("labels", [])
                        if label.get("name")
                    ],
                )
            if cutoff_reached:
                break
            next_url = response.links.get("next", {}).get("url")

    async def iter_issues(
        self,
        repo: RepoRef,
        since: datetime | None = None,
    ) -> AsyncIterator[IssueData]:
        next_url = (
            f"/repos/{repo.owner}/{repo.name}/issues"
            "?state=all&sort=updated&direction=desc&per_page=100"
        )
        self.issue_total_estimate = None
        is_first = True

        while next_url:
            response = await self._request(
                "GET",
                next_url,
                operation="iter_issues",
                repo=repo.slug,
            )
            if is_first:
                last_url = response.links.get("last", {}).get("url")
                if last_url:
                    from urllib.parse import parse_qs, urlparse
                    parsed = urlparse(last_url)
                    pages = parse_qs(parsed.query).get("page")
                    if pages:
                        try:
                            self.issue_total_estimate = int(pages[0]) * 100
                        except ValueError:
                            pass
                is_first = False

            items = response.json()
            cutoff_reached = False
            for item in items:
                updated_at = parse_github_datetime(item.get("updated_at"))
                if since and updated_at and updated_at <= since:
                    cutoff_reached = True
                    continue
                if item.get("pull_request"):
                    continue
                yield IssueData(
                    github_id=item["id"],
                    number=item["number"],
                    title=item["title"],
                    state=item["state"],
                    author=(item.get("user") or {}).get("login"),
                    created_at=parse_github_datetime(item.get("created_at")),
                    updated_at=updated_at,
                    closed_at=parse_github_datetime(item.get("closed_at")),
                    body=item.get("body"),
                    labels=[
                        label["name"]
                        for label in item.get("labels", [])
                        if label.get("name")
                    ],
                )
            if cutoff_reached:
                break
            next_url = response.links.get("next", {}).get("url")

    async def fetch_single_pull_request(self, repo: RepoRef, pr_number: int) -> PullRequestData:
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/pulls/{pr_number}",
            operation="fetch_single_pull_request",
            repo=repo.slug,
        )
        item = response.json()
        return PullRequestData(
            github_id=item["id"],
            number=item["number"],
            title=item["title"],
            state=item["state"],
            author=(item.get("user") or {}).get("login"),
            created_at=parse_github_datetime(item.get("created_at")),
            updated_at=parse_github_datetime(item.get("updated_at")),
            body=item.get("body"),
            base_branch=(item.get("base") or {}).get("ref"),
            head_branch=(item.get("head") or {}).get("ref"),
            merge_commit_sha=item.get("merge_commit_sha"),
            labels=[
                label["name"]
                for label in item.get("labels", [])
                if label.get("name")
            ],
        )

    async def fetch_single_issue(self, repo: RepoRef, issue_number: int) -> IssueData:
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/issues/{issue_number}",
            operation="fetch_single_issue",
            repo=repo.slug,
        )
        item = response.json()
        if item.get("pull_request"):
            raise GitHubApiError(
                f"Requested issue #{issue_number} is a pull request.",
                http_status=response.status_code,
                response_text=response.text,
                operation="fetch_single_issue",
                repo=repo.slug,
            )
        return IssueData(
            github_id=item["id"],
            number=item["number"],
            title=item["title"],
            state=item["state"],
            author=(item.get("user") or {}).get("login"),
            created_at=parse_github_datetime(item.get("created_at")),
            updated_at=parse_github_datetime(item.get("updated_at")),
            closed_at=parse_github_datetime(item.get("closed_at")),
            body=item.get("body"),
            labels=[
                label["name"]
                for label in item.get("labels", [])
                if label.get("name")
            ],
        )

    async def fetch_issue_comments(
        self, repo: RepoRef, issue_number: int
    ) -> list[IssueCommentData]:
        next_url = f"/repos/{repo.owner}/{repo.name}/issues/{issue_number}/comments?per_page=100"
        comments = []
        while next_url:
            response = await self._request(
                "GET",
                next_url,
                operation="fetch_issue_comments",
                repo=repo.slug,
            )
            items = response.json()
            for item in items:
                reactions = item.get("reactions") or {}
                reaction_count = reactions.get("total_count", 0)
                comments.append(
                    IssueCommentData(
                        github_comment_id=item["id"],
                        author=(item.get("user") or {}).get("login"),
                        body=item.get("body") or "",
                        created_at=parse_github_datetime(item.get("created_at")),
                        updated_at=parse_github_datetime(item.get("updated_at")),
                        reaction_count=reaction_count,
                    )
                )
            next_url = response.links.get("next", {}).get("url")
        return comments

    async def check_staleness(
        self, repo: RepoRef, target_type: str, number: int
    ) -> datetime | None:
        """Returns the updated_at timestamp for a PR or Issue to check for staleness."""
        endpoint = "pulls" if target_type == "pr" else "issues"
        response = await self._request(
            "GET",
            f"/repos/{repo.owner}/{repo.name}/{endpoint}/{number}",
            operation="check_staleness",
            repo=repo.slug,
        )
        item = response.json()
        return parse_github_datetime(item.get("updated_at"))

    async def fetch_review_threads(
        self,
        repo: RepoRef,
        pr_number: int,
    ) -> list[ReviewThreadData]:
        query = """
        query PullRequestReviewThreads(
          $owner: String!, $name: String!, $number: Int!, $cursor: String
        ) {
          repository(owner: $owner, name: $name) {
            pullRequest(number: $number) {
              reviewThreads(first: 100, after: $cursor) {
                pageInfo {
                  hasNextPage
                  endCursor
                }
                nodes {
                  id
                  isResolved
                  isOutdated
                  resolvedBy {
                    login
                  }
                  path
                  line
                  comments(first: 100) {
                    pageInfo {
                      hasNextPage
                      endCursor
                    }
                    nodes {
                      databaseId
                      author {
                        login
                      }
                      body
                      createdAt
                      updatedAt
                      reactionGroups {
                        users {
                          totalCount
                        }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """

        cursor: str | None = None
        threads: list[ReviewThreadData] = []

        while True:
            data = await self._graphql(
                query,
                {
                    "owner": repo.owner,
                    "name": repo.name,
                    "number": pr_number,
                    "cursor": cursor,
                },
                operation="fetch_review_threads",
                repo=repo.slug,
            )
            pull_request = (data.get("repository") or {}).get("pullRequest") or {}
            review_threads = pull_request.get("reviewThreads") or {}
            nodes = review_threads.get("nodes") or []

            for node in nodes:
                state = "active"
                if node.get("isOutdated"):
                    state = "outdated"
                elif node.get("isResolved"):
                    state = "resolved"

                comments_connection = node.get("comments") or {}
                comments: list[ReviewCommentData] = []
                for comment_node in comments_connection.get("nodes") or []:
                    parsed = self._parse_review_comment(comment_node)
                    if parsed is not None:
                        comments.append(parsed)

                comments_page_info = comments_connection.get("pageInfo") or {}
                if comments_page_info.get("hasNextPage"):
                    comments.extend(
                        await self._fetch_remaining_thread_comments(
                            node["id"],
                            comments_page_info["endCursor"],
                            repo=repo,
                        )
                    )

                if comments:
                    
                    valid_created = [x for x in [c.created_at for c in comments] if x is not None]
                    valid_updated = [x for x in [c.updated_at or c.created_at for c in comments] if x is not None]  # noqa: E501
                    thread_created_at = min(valid_created) if valid_created else None
                    thread_updated_at = max(valid_updated) if valid_updated else None
                else:
                    thread_created_at = None
                    thread_updated_at = None
                
                thread_resolved_at = thread_updated_at if state == "resolved" else None

                threads.append(
                    ReviewThreadData(
                        github_thread_id=node["id"],
                        file_path=node.get("path"),
                        line_number=node.get("line"),
                        thread_state=state,
                        resolved_by=(node.get("resolvedBy") or {}).get("login"),
                        resolved_at=thread_resolved_at,
                        created_at=thread_created_at,
                        updated_at=thread_updated_at,
                        comments=comments,
                    )
                )

            page_info = review_threads.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")

        return threads
