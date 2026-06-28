"""Tests for architectural memory generation and caching."""

from unittest.mock import patch

import pytest

from oss_context.architecture import explain_code, generate_architectural_memory
from oss_context.db import DatabaseManager


@pytest.fixture
def db_conn(tmp_path):
    manager = DatabaseManager(tmp_path / "test.db")
    conn = manager.initialize()
    
    # Insert a dummy repo and target PR/Issue for testing
    conn.execute("INSERT INTO repos (id, github_id, owner, name, default_branch, last_synced_at) VALUES (1, 100, 'test', 'repo', 'main', '2020-01-01T00:00:00Z')")  # noqa: E501
    conn.execute("INSERT INTO prs (id, repo_id, number, state, author, title, body, created_at, updated_at) VALUES (1, 1, 42, 'OPEN', 'bob', 'test pr', '', '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')")  # noqa: E501
    conn.execute("INSERT INTO review_threads (id, github_thread_id, pr_id, file_path, thread_state, created_at) VALUES (1, 'thread1', 1, 'src/auth.py', 'OPEN', '2020-01-01T00:00:00Z')")  # noqa: E501
    conn.execute("INSERT INTO review_comments (id, thread_id, github_comment_id, author, body, created_at, updated_at) VALUES (1, 1, 100, 'alice', 'Ask about auth', '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')")  # noqa: E501
    
    # Insert some architectural decisions
    conn.execute("INSERT INTO decision_log (id, comment_id, pr_id, decision_type, extracted_summary, extracted_at) VALUES (1, 1, 1, 'QUESTION', 'Ask about auth', '2020-01-01T00:00:00Z')")  # noqa: E501
    conn.execute("INSERT INTO architectural_decisions (id, repo_id, pr_id, summary, rationale, alternatives, outcome, created_at) VALUES (1, 1, 1, 'Use OAuth2', 'Because it is standard', 'JWT', 'ACCEPTED', '2020-01-01T00:00:00Z')")  # noqa: E501
    conn.execute("INSERT INTO implementation_summaries (repo_id, target_type, target_id, file_path, summary, generated_at) VALUES (1, 'pr', 42, 'src/auth.py', 'Implements OAuth2', '2020-01-01T00:00:00Z')")  # noqa: E501
    conn.commit()
    
    yield conn
    conn.close()


@pytest.mark.asyncio
@patch("oss_context.architecture._call_llm_for_architecture")
async def test_generate_architectural_memory_caches_result(mock_call_llm, db_conn):
    # Setup mock LLM response
    mock_call_llm.return_value = {
      "design_summary": "Test design summary",
      "decisions": [
        {
          "summary": "Decide on X",
          "rationale": "Because Y",
          "alternatives": "Z",
          "outcome": "ACCEPTED"
        }
      ],
      "implementation": [
        {
          "file_path": "src/main.py",
          "summary": "Added main logic"
        }
      ],
      "rationale_links": [
        {
          "target_type": "decision",
          "target_id": "1",
          "relationship": "supports"
        }
      ]
    }
    
    # First call - should call LLM and cache
    memory = await generate_architectural_memory(db_conn, "pr", 42, 1)
    
    assert mock_call_llm.call_count == 1
    assert memory["design_summary"] == "Test design summary"
    assert len(memory["decisions"]) == 1
    assert memory["implementation"][0]["file_path"] == "src/main.py"
    
    # Verify DB insertion
    row = db_conn.execute("SELECT summary FROM design_summaries WHERE repo_id = 1 AND target_type = 'pr' AND target_id = 42").fetchone()  # noqa: E501
    assert row is not None
    assert row["summary"] == "Test design summary"
    
    imp_row = db_conn.execute("SELECT summary FROM implementation_summaries WHERE repo_id = 1 AND file_path = 'src/main.py'").fetchone()  # noqa: E501
    assert imp_row is not None
    assert imp_row["summary"] == "Added main logic"
    
    # Second call - should fetch from cache
    memory2 = await generate_architectural_memory(db_conn, "pr", 42, 1)
    assert mock_call_llm.call_count == 1  # No additional calls
    assert memory2["design_summary"] == "Test design summary"
    assert len(memory2["decisions"]) == 2
    # The first one is the seeded one: "Use OAuth2"
    # The second is from mock: "Decide on X"
    assert memory2["decisions"][1]["rationale"] == "Because Y"
    assert len(memory2["rationale_links"]) == 1
    assert memory2["rationale_links"][0]["relationship"] == "supports"


@pytest.mark.asyncio
async def test_explain_code(db_conn):
    # explain_code falls back to returning the generated context when no API key is present
    explanation = await explain_code(db_conn, 1, "test/repo", "src/auth.py")
    assert "OAuth2" in explanation
    assert "Because it is standard" in explanation
