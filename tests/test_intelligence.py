import pytest

from oss_context.db import DatabaseManager
from oss_context.intelligence import analyze_pending_comments
from oss_context.settings import load_settings


@pytest.mark.asyncio
async def test_analyze_pending_comments_rejects_non_positive_batch_size(tmp_path):
    connection = DatabaseManager(tmp_path / "oss_context.db").initialize()
    settings = load_settings(tmp_path / "oss_context.db")

    with pytest.raises(ValueError, match="batch_size"):
        await analyze_pending_comments(connection, settings, batch_size=0)

    connection.close()
