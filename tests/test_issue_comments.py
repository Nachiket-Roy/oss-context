from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from oss_context.db import DatabaseManager
from oss_context.formatting import render_issue_context
from oss_context.github import GitHubClient
from oss_context.markdown import render_issue_context_markdown
from oss_context.models import IssueCommentData, IssueData, RepoRef
from oss_context.queries import (
    get_issue_backreferences,
    get_issue_comments,
    get_issue_context_payload,
    get_issue_references,
)
from oss_context.sync import sync_repository, sync_single_issue
from oss_context.web_ui import _render_issue_body


def test_issue_comments_schema_initialization(tmp_path):
    db_path = tmp_path / "test.db"
    manager = DatabaseManager(db_path)
    connection = manager.initialize()
    try:
        # Check that table exists
        cursor = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='issue_comments'"
        )
        assert cursor.fetchone() is not None

        # Check column types/names
        cursor = connection.execute("PRAGMA table_info(issue_comments)")
        columns = {row["name"]: row for row in cursor.fetchall()}
        assert "issue_id" in columns
        assert "github_comment_id" in columns
        assert "body" in columns
        assert "author" in columns
        assert "created_at" in columns
        assert "updated_at" in columns
        assert "reaction_count" in columns
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_fetch_issue_comments_client(monkeypatch):
    from oss_context.settings import Settings
    from pathlib import Path
    settings = Settings(db_path=Path(":memory:"), github_token="mock-token")
    client = GitHubClient(settings)

    mock_response = MagicMock()
    mock_response.json.return_value = [
        {
            "id": 12345,
            "user": {"login": "commenter1"},
            "body": "Nice template work! #5152",
            "created_at": "2026-06-29T10:00:00Z",
            "updated_at": "2026-06-29T10:05:00Z",
            "reactions": {"total_count": 5},
        }
    ]
    mock_response.links = {}

    async def mock_request(*args, **kwargs):
        return mock_response

    monkeypatch.setattr(client, "_request", mock_request)

    comments = await client.fetch_issue_comments(RepoRef(owner="lima-vm", name="lima"), 5152)
    assert len(comments) == 1
    assert comments[0].github_comment_id == 12345
    assert comments[0].author == "commenter1"
    assert comments[0].body == "Nice template work! #5152"
    assert comments[0].reaction_count == 5
    assert comments[0].created_at == datetime(2026, 6, 29, 10, 0, tzinfo=UTC)

    await client.client.aclose()


@pytest.mark.asyncio
async def test_sync_single_issue_with_comments(tmp_path):
    from oss_context.settings import Settings
    db_path = tmp_path / "test.db"
    settings = Settings(db_path=db_path)

    # Pre-initialize schema
    DatabaseManager(db_path).initialize().close()

    mock_client = AsyncMock()
    mock_client.get_repo.return_value = {"id": 100, "default_branch": "main"}
    mock_client.fetch_single_issue.return_value = IssueData(
        github_id=999,
        number=5152,
        title="WSL2 Support",
        state="open",
        author="nachiket",
        created_at=datetime(2026, 6, 29, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 6, 29, 9, 30, tzinfo=UTC),
        body="This is issues body referencing lima-vm/lima#3991",
    )
    mock_client.fetch_issue_comments.return_value = [
        IssueCommentData(
            github_comment_id=200,
            author="suda",
            body="Review comment pointing to lima-vm/finch#123",
            created_at=datetime(2026, 6, 29, 9, 5, tzinfo=UTC),
            updated_at=datetime(2026, 6, 29, 9, 10, tzinfo=UTC),
            reaction_count=1,
        )
    ]

    with patch("oss_context.sync.GitHubClient") as mock_gh_class:
        mock_gh_class.return_value.__aenter__.return_value = mock_client

        await sync_single_issue("lima-vm/lima", 5152, settings)

    # Verify saved database state
    connection = DatabaseManager(db_path).connect()
    try:
        # Check issue
        row = connection.execute("SELECT id, title FROM issues WHERE number = 5152").fetchone()
        assert row is not None
        assert row["title"] == "WSL2 Support"
        issue_id = row["id"]

        # Check comment
        c_row = connection.execute(
            "SELECT author, body FROM issue_comments WHERE issue_id = ?", (issue_id,)
        ).fetchone()
        assert c_row is not None
        assert c_row["author"] == "suda"
        assert c_row["body"] == "Review comment pointing to lima-vm/finch#123"

        # Check reference extraction from comment
        ref_rows = connection.execute(
            "SELECT source_kind, target_repo, target_number FROM extracted_references "
            "WHERE source_kind = 'issue_comment'"
        ).fetchall()
        assert len(ref_rows) == 1
        assert ref_rows[0]["target_repo"] == "lima-vm/finch"
        assert ref_rows[0]["target_number"] == 123
    finally:
        connection.close()


@pytest.mark.asyncio
async def test_sync_repository_with_comments(tmp_path):
    from oss_context.settings import Settings
    db_path = tmp_path / "test.db"
    settings = Settings(db_path=db_path)

    # Pre-initialize schema
    DatabaseManager(db_path).initialize().close()

    mock_client = AsyncMock()
    mock_client.get_repo.return_value = {"id": 100, "default_branch": "main"}
    
    async def mock_iter_issues(*args, **kwargs):
        yield IssueData(
            github_id=999,
            number=5152,
            title="WSL2 Support",
            state="open",
            author="nachiket",
            created_at=datetime(2026, 6, 29, 9, 0, tzinfo=UTC),
            updated_at=datetime(2026, 6, 29, 9, 30, tzinfo=UTC),
            body="Body",
        )

    mock_client.iter_issues = mock_iter_issues
    mock_client.fetch_issue_comments.return_value = [
        IssueCommentData(
            github_comment_id=200,
            author="suda",
            body="Review comment pointing to lima-vm/finch#123",
            created_at=datetime(2026, 6, 29, 9, 5, tzinfo=UTC),
            updated_at=datetime(2026, 6, 29, 9, 10, tzinfo=UTC),
            reaction_count=1,
        )
    ]
    # Keep PRs mock empty to focus on issues
    async def mock_iter_prs(*args, **kwargs):
        if False:
            yield

    mock_client.iter_pull_requests = mock_iter_prs
    mock_client.pr_total_estimate = 0
    mock_client.issue_total_estimate = 1

    with patch("oss_context.sync.GitHubClient") as mock_gh_class:
        mock_gh_class.return_value.__aenter__.return_value = mock_client

        report = await sync_repository("lima-vm/lima", settings, extract_decisions=False, limit=1)

    assert report.issues_synced == 1
    assert report.comments_synced == 1
    assert report.references_extracted == 1


def test_issue_comments_queries_and_backreferences(tmp_path):
    connection = DatabaseManager(tmp_path / "test.db").initialize()
    try:
        # Seed Repo & Issue
        connection.execute(
            "INSERT INTO repos(id, github_id, owner, name, default_branch, last_synced_at) "
            "VALUES(1, 10, 'lima-vm', 'lima', 'main', '2026-06-29T10:00:00Z')"
        )
        connection.execute(
            "INSERT INTO issues("
            "id, github_id, repo_id, number, title, state, author, "
            "created_at, updated_at, body) "
            "VALUES(10, 999, 1, 5152, 'WSL2 Support', 'open', 'nachiket', "
            "'2026-06-29T09:00:00Z', '2026-06-29T09:30:00Z', 'Body')"
        )
        # Seed Comment
        connection.execute(
            "INSERT INTO issue_comments("
            "id, issue_id, github_comment_id, author, body, "
            "created_at, updated_at, reaction_count) "
            "VALUES(50, 10, 200, 'suda', 'Nice template work! #3991', "
            "'2026-06-29T09:05:00Z', '2026-06-29T09:10:00Z', 1)"
        )
        # Seed Reference from comment
        connection.execute(
            "INSERT INTO extracted_references("
            "source_kind, source_id, repo_id, reference_kind, raw_text, url, "
            "target_repo, target_number) "
            "VALUES('issue_comment', 50, 1, 'issue_or_pr', '#3991', NULL, "
            "'lima-vm/lima', 3991)"
        )
        # Seed another Issue pointing to #5152 via comment
        connection.execute(
            "INSERT INTO issues("
            "id, github_id, repo_id, number, title, state, author, "
            "created_at, updated_at, body) "
            "VALUES(11, 998, 1, 3991, 'Ubuntu image', 'open', 'alice', "
            "'2026-06-29T09:00:00Z', '2026-06-29T09:30:00Z', 'Body')"
        )
        connection.execute(
            "INSERT INTO issue_comments("
            "id, issue_id, github_comment_id, author, body, "
            "created_at, updated_at, reaction_count) "
            "VALUES(51, 11, 201, 'bob', 'Links to #5152', "
            "'2026-06-29T09:06:00Z', '2026-06-29T09:06:00Z', 0)"
        )
        connection.execute(
            "INSERT INTO extracted_references("
            "source_kind, source_id, repo_id, reference_kind, raw_text, url, "
            "target_repo, target_number) "
            "VALUES('issue_comment', 51, 1, 'issue', '#5152', NULL, "
            "'lima-vm/lima', 5152)"
        )
        connection.commit()

        # Test get_issue_comments
        comments = get_issue_comments(connection, repo="lima-vm/lima", issue_number=5152)
        assert len(comments) == 1
        assert comments[0]["author"] == "suda"
        assert comments[0]["body"] == "Nice template work! #3991"

        # Test get_issue_context_payload
        payload = get_issue_context_payload(connection, repo="lima-vm/lima", issue_number=5152)
        assert len(payload["comments"]) == 1
        assert payload["comments"][0]["author"] == "suda"

        # Test get_issue_references
        refs = get_issue_references(connection, repo="lima-vm/lima", issue_number=5152)
        assert len(refs) == 1
        assert refs[0]["source_label"] == "Comment by suda"
        assert refs[0]["target_number"] == 3991

        # Test get_issue_backreferences (target is #5152, source is bob's comment on #3991)
        backrefs = get_issue_backreferences(connection, repo="lima-vm/lima", issue_number=5152)
        assert len(backrefs) == 1
        assert backrefs[0]["source_kind"] == "issue_comment"
        assert backrefs[0]["source_label"] == "Comment by bob on Issue #3991 Ubuntu image"
    finally:
        connection.close()


def test_issue_comments_rendering():
    payload = {
        "repo": "lima-vm/lima",
        "issue_number": 5152,
        "title": "WSL2 Support",
        "state": "open",
        "author": "nachiket",
        "body": "Body text",
        "labels": ["bug"],
        "references": [],
        "mentioned_by": [],
        "comments": [
            {
                "author": "suda",
                "created_at": "2026-06-29T10:00:00Z",
                "body": "Awesome addition!",
                "reaction_count": 2,
            }
        ],
        "repo_status": {"last_synced_at": "2026-06-29T10:00:00Z"},
    }

    # Markdown rendering test
    markdown = render_issue_context_markdown(payload)
    assert "## Activity (Comments)" in markdown
    assert "suda" in markdown
    assert "Awesome addition" in markdown

    # CLI rendering test (Rich Panel)
    panel = render_issue_context(payload)
    assert panel is not None

    # HTML rendering test
    html = _render_issue_body(payload)
    assert "<h2>Activity (Comments)</h2>" in html
    assert "suda" in html
    assert "Awesome addition!" in html


@pytest.mark.asyncio
async def test_recursive_jit_sync(tmp_path):
    from oss_context.settings import Settings
    db_path = tmp_path / "test.db"
    settings = Settings(db_path=db_path)

    # Initialize schema
    DatabaseManager(db_path).initialize().close()

    mock_client = AsyncMock()
    mock_client.get_repo.return_value = {"id": 100, "default_branch": "main"}
    
    async def mock_fetch_single_issue(repo, issue_number):
        if issue_number == 5152:
            return IssueData(
                github_id=999,
                number=5152,
                title="WSL2 Support",
                state="open",
                author="nachiket",
                created_at=datetime(2026, 6, 29, 9, 0, tzinfo=UTC),
                updated_at=datetime(2026, 6, 29, 9, 30, tzinfo=UTC),
                body="This is issues body referencing lima-vm/lima#3991",
            )
        elif issue_number == 3991:
            return IssueData(
                github_id=888,
                number=3991,
                title="Ubuntu Image",
                state="open",
                author="alice",
                created_at=datetime(2026, 6, 29, 9, 0, tzinfo=UTC),
                updated_at=datetime(2026, 6, 29, 9, 30, tzinfo=UTC),
                body="Details",
            )
        raise ValueError(f"Unexpected issue number: {issue_number}")

    mock_client.fetch_single_issue.side_effect = mock_fetch_single_issue
    mock_client.fetch_issue_comments.return_value = []
    mock_client.check_staleness.return_value = None

    with patch("oss_context.sync.GitHubClient") as mock_gh_class:
        mock_gh_class.return_value.__aenter__.return_value = mock_client

        await sync_single_issue("lima-vm/lima", 5152, settings, _depth=1)

    # Verify both issues are stored in the database!
    connection = DatabaseManager(db_path).connect()
    try:
        row5152 = connection.execute("SELECT title FROM issues WHERE number = 5152").fetchone()
        row3991 = connection.execute("SELECT title FROM issues WHERE number = 3991").fetchone()
        assert row5152 is not None
        assert row5152["title"] == "WSL2 Support"
        assert row3991 is not None
        assert row3991["title"] == "Ubuntu Image"
    finally:
        connection.close()


def test_discussion_reference(tmp_path):
    from oss_context.formatting import render_issue_context
    from oss_context.markdown import render_issue_context_markdown
    from oss_context.queries import get_issue_references
    from oss_context.references import extract_references
    from oss_context.sync import _replace_references
    from oss_context.web_ui import _render_issue_body

    url_str = "https://github.com/lima-vm/lima/discussions/3829"
    text = f"This matches {url_str} in conversation."
    refs = extract_references(text, repo="lima-vm/lima")
    assert len(refs) == 1
    assert refs[0].kind == "discussion"
    assert refs[0].target_number == 3829
    assert refs[0].url == url_str

    # Setup database with migrated schemas
    db_path = tmp_path / "test.db"
    connection = DatabaseManager(db_path).initialize()
    try:
        # Seed repo & issue
        connection.execute(
            "INSERT INTO repos(id, github_id, owner, name, default_branch, last_synced_at) "
            "VALUES(1, 10, 'lima-vm', 'lima', 'main', '2026-06-29T10:00:00Z')"
        )
        connection.execute(
            "INSERT INTO issues("
            "id, github_id, repo_id, number, title, state, author, created_at, updated_at, body) "
            "VALUES(10, 999, 1, 5152, 'WSL2 Support', 'open', 'nachiket', "
            "'2026-06-29T09:00:00Z', '2026-06-29T09:30:00Z', 'Body')"
        )
        connection.commit()

        # Mock the synchronous HTTP request for the title
        with patch("httpx.get") as mock_get:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.text = (
                "<title>Architecture discussion for WSL2 · Discussion #3829 "
                "· lima-vm/lima · GitHub</title>"
            )
            mock_get.return_value = mock_response

            _replace_references(
                connection,
                repo_id=1,
                repo_slug="lima-vm/lima",
                source_kind="issue",
                source_id=10,
                text=text,
            )

        # Verify title is stored in the database!
        row = connection.execute(
            "SELECT title, reference_kind FROM extracted_references WHERE source_id = 10"
        ).fetchone()
        assert row is not None
        assert row["reference_kind"] == "discussion"
        assert row["title"] == "Architecture discussion for WSL2"

        # Verify queries return the title field
        payload_refs = get_issue_references(
            connection, repo="lima-vm/lima", issue_number=5152
        )
        assert len(payload_refs) == 1
        assert payload_refs[0]["title"] == "Architecture discussion for WSL2"
        assert payload_refs[0]["reference_kind"] == "discussion"

        # Verify formatting output
        payload = {
            "repo": "lima-vm/lima",
            "issue_number": 5152,
            "title": "WSL2 Support",
            "state": "open",
            "author": "nachiket",
            "body": "Body text",
            "labels": ["bug"],
            "references": payload_refs,
            "mentioned_by": [],
            "comments": [],
            "repo_status": {"last_synced_at": "2026-06-29T10:00:00Z"},
        }

        # CLI render
        panel = render_issue_context(payload)
        assert panel is not None

        # Markdown render
        md = render_issue_context_markdown(payload)
        assert "Discussion" in md
        assert "3829" in md
        assert "Architecture discussion for WSL2" in md

        # Web UI render
        html = _render_issue_body(payload)
        assert (
            "Discussion #3829 (Architecture discussion for WSL2)"
            in html
        )

    finally:
        connection.close()
