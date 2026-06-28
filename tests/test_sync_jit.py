from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from oss_context.sync import ensure_pr_synced


@pytest.mark.asyncio
async def test_ensure_pr_synced_missing(tmp_path):
    """Test that missing PR triggers sync."""
    # We patch DatabaseManager to return a mock connection
    # We patch GitHubClient to mock fetch and staleness check
    # We patch sync_single_pr so it doesn't actually hit the DB
    with (
        patch("oss_context.sync.DatabaseManager") as mock_db,
        patch("oss_context.sync.sync_single_pr") as mock_sync_pr,
    ):
        mock_conn = mock_db.return_value.initialize.return_value
        # Simulate PR not found in DB
        mock_conn.execute.return_value.fetchone.return_value = None

        from oss_context.settings import Settings
        settings = Settings(db_path=tmp_path / "test.db")

        await ensure_pr_synced("owner/repo", 123, settings)

        # Should have called sync_single_pr
        mock_sync_pr.assert_called_once_with("owner/repo", 123, settings)


@pytest.mark.asyncio
async def test_ensure_pr_synced_stale(tmp_path):
    """Test that stale PR triggers sync."""
    with (
        patch("oss_context.sync.DatabaseManager") as mock_db,
        patch("oss_context.sync.GitHubClient") as mock_github,
        patch("oss_context.sync.sync_single_pr") as mock_sync_pr,
    ):
        mock_conn = mock_db.return_value.initialize.return_value
        # Simulate PR found in DB with old timestamp
        mock_conn.execute.return_value.fetchone.return_value = {
            "updated_at": "2020-01-01T00:00:00Z"
        }

        mock_client_instance = AsyncMock()
        mock_github.return_value.__aenter__.return_value = mock_client_instance
        # Simulate remote PR has a newer timestamp
        mock_client_instance.check_staleness.return_value = datetime(2021, 1, 1, tzinfo=UTC)

        from oss_context.settings import Settings
        settings = Settings(db_path=tmp_path / "test.db")

        await ensure_pr_synced("owner/repo", 123, settings)

        # Should have called sync_single_pr because remote > local
        mock_sync_pr.assert_called_once_with("owner/repo", 123, settings)


@pytest.mark.asyncio
async def test_ensure_pr_synced_up_to_date(tmp_path):
    """Test that up-to-date PR does NOT trigger sync."""
    with (
        patch("oss_context.sync.DatabaseManager") as mock_db,
        patch("oss_context.sync.GitHubClient") as mock_github,
        patch("oss_context.sync.sync_single_pr") as mock_sync_pr,
    ):
        mock_conn = mock_db.return_value.initialize.return_value
        # Simulate PR found in DB with recent timestamp
        mock_conn.execute.return_value.fetchone.return_value = {
            "updated_at": "2022-01-01T00:00:00Z"
        }

        mock_client_instance = AsyncMock()
        mock_github.return_value.__aenter__.return_value = mock_client_instance
        # Simulate remote PR has an older or equal timestamp
        mock_client_instance.check_staleness.return_value = datetime(2021, 1, 1, tzinfo=UTC)

        from oss_context.settings import Settings
        settings = Settings(db_path=tmp_path / "test.db")

        await ensure_pr_synced("owner/repo", 123, settings)

        # Should NOT have called sync_single_pr
        mock_sync_pr.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_pr_synced_force(tmp_path):
    """Test that force_sync overrides staleness check."""
    with (
        patch("oss_context.sync.DatabaseManager") as mock_db,
        patch("oss_context.sync.sync_single_pr") as mock_sync_pr,
    ):
        mock_conn = mock_db.return_value.initialize.return_value
        # Simulate PR found in DB with recent timestamp
        mock_conn.execute.return_value.fetchone.return_value = {
            "updated_at": "2022-01-01T00:00:00Z"
        }

        from oss_context.settings import Settings
        settings = Settings(db_path=tmp_path / "test.db")

        await ensure_pr_synced("owner/repo", 123, settings, force_sync=True)

        # Should have called sync_single_pr due to force_sync=True
        mock_sync_pr.assert_called_once_with("owner/repo", 123, settings)
