"""Architectural Memory Extraction and Management for oss-context."""

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

import httpx

from oss_context.settings import load_settings

ARCHITECTURAL_MEMORY_PROMPT = """You extract architectural memory from GitHub discussions.
Given a discussion context (issues, PRs, comments, commits), synthesize the final design, 
architectural decisions, implementation summaries by file, and causal rationale links.

Output format:
{
  "design_summary": "Concise summary of the final accepted design",
  "decisions": [
    {
      "summary": "Short decision summary",
      "rationale": "Why it was decided",
      "alternatives": "What was rejected",
      "outcome": "ACCEPTED or REJECTED"
    }
  ],
  "implementation": [
    {
      "file_path": "path/to/file.py",
      "summary": "What changed conceptually"
    }
  ],
  "rationale_links": [
    {
      "target_type": "issue or pr",
      "target_id": "issue/pr number",
      "relationship": "addresses or supports"
    }
  ]
}
"""

async def _call_llm_for_architecture(context_text: str) -> dict[str, Any]:
    settings = load_settings()
    api_key = settings.llm_api_key
    if not api_key:
        return {}

    # Simplify by only using OpenAI structure for now (or Anthropic if configured, but let's assume OpenAI/generic JSON)  # noqa: E501
    payload = {
        "model": settings.llm_model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": ARCHITECTURAL_MEMORY_PROMPT},
            {"role": "user", "content": json.dumps({"context": context_text})}
        ]
    }
    
    url = "https://api.openai.com/v1/chat/completions"
    if "claude" in settings.llm_model.lower():
        # Quick fallback for Anthropic structure if needed, or we just raise/skip
        # Just stubbing generic OpenAI format for now
        pass
        
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)
        except Exception:
            return {}

async def generate_architectural_memory(
    connection: sqlite3.Connection,
    target_type: str,
    target_id: int,
    repo_id: int
) -> dict[str, Any]:
    """Lazily generate and cache architectural memory for a PR or Issue."""
    if target_type not in ("pr", "issue"):
        raise ValueError(f"Invalid target_type: {target_type}")
    
    # Check cache first
    cached = connection.execute(
        "SELECT summary FROM design_summaries WHERE repo_id = ? AND target_type = ? AND target_id = ?",  # noqa: E501
        (repo_id, target_type, target_id)
    ).fetchone()
    if cached:
        # We assume if design_summaries exists, others exist. Return from DB.
        # But returning everything reconstructed might be tedious, so let's reconstruct it.
        design_summary = cached["summary"]
        
        # We need the internal prs.id / issues.id, not the PR number
        if target_type == "pr":
            target_row = connection.execute("SELECT id FROM prs WHERE repo_id = ? AND number = ?", (repo_id, target_id)).fetchone()  # noqa: E501
        else:
            target_row = connection.execute("SELECT id FROM issues WHERE repo_id = ? AND number = ?", (repo_id, target_id)).fetchone()  # noqa: E501
            
        internal_id = target_row["id"] if target_row else None
        
        if not internal_id:
            return {}
            
        decisions = connection.execute(
            f"SELECT summary, rationale, alternatives, outcome FROM architectural_decisions WHERE {target_type}_id = ?",  # noqa: E501
            (internal_id,)
        ).fetchall()
        impls = connection.execute(
            "SELECT file_path, summary FROM implementation_summaries WHERE repo_id = ? AND target_type = ? AND target_id = ?",  # noqa: E501
            (repo_id, target_type, target_id)
        ).fetchall()
        links = connection.execute(
            "SELECT target_type, target_id, relationship FROM rationale_links WHERE repo_id = ? AND source_type = 'design_summary' AND source_id = ?",  # noqa: E501
            (repo_id, target_id,)
        ).fetchall()
        return {
            "design_summary": design_summary,
            "decisions": [dict(d) for d in decisions],
            "implementation": [dict(i) for i in impls],
            "rationale_links": [dict(link_row) for link_row in links]
        }
    
    # Gather context
    if target_type == "pr":
        row = connection.execute("SELECT * FROM prs WHERE repo_id = ? AND number = ?", (repo_id, target_id)).fetchone()  # noqa: E501
        if not row:
            return {}
        context = f"PR #{row['number']}: {row['title']}\n\n{row['body']}\n\n"
        decisions = connection.execute(
            "SELECT * FROM decision_log WHERE pr_id = ?", (row["id"],)
        ).fetchall()
        for d in decisions:
            context += f"Comment: {d['raw_text']}\nDecision: {d['decision_status']} - {d['extracted_summary']}\nReason: {d['decision_reason']}\n\n"  # noqa: E501
    else:
        row = connection.execute("SELECT * FROM issues WHERE repo_id = ? AND number = ?", (repo_id, target_id)).fetchone()  # noqa: E501
        if not row:
            return {}
        context = f"Issue #{row['number']}: {row['title']}\n\n{row['body']}\n\n"
        
    result = await _call_llm_for_architecture(context)
    if not result:
        return {}
        
    # Cache results
    now = datetime.now(UTC)
    
    connection.execute(
        "INSERT INTO design_summaries (repo_id, target_type, target_id, summary, generated_at) VALUES (?, ?, ?, ?, ?)",  # noqa: E501
        (repo_id, target_type, target_id, result.get("design_summary", ""), now)
    )
    
    pr_id = row["id"] if target_type == "pr" else None
    issue_id = row["id"] if target_type == "issue" else None
    
    for d in result.get("decisions", []):
        connection.execute(
            "INSERT INTO architectural_decisions (repo_id, pr_id, issue_id, summary, rationale, alternatives, outcome, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",  # noqa: E501
            (repo_id, pr_id, issue_id, d.get("summary", ""), d.get("rationale", ""), d.get("alternatives", ""), d.get("outcome", ""), now)  # noqa: E501
        )
        
    for i in result.get("implementation", []):
        connection.execute(
            "INSERT INTO implementation_summaries (repo_id, target_type, target_id, file_path, summary, generated_at) VALUES (?, ?, ?, ?, ?, ?)",  # noqa: E501
            (repo_id, target_type, target_id, i.get("file_path", ""), i.get("summary", ""), now)
        )
        
    for link_obj in result.get("rationale_links", []):
        connection.execute(
            "INSERT INTO rationale_links (repo_id, source_type, source_id, target_type, target_id, relationship) VALUES (?, ?, ?, ?, ?, ?)",  # noqa: E501
            (repo_id, "design_summary", target_id, link_obj.get("target_type", ""), link_obj.get("target_id", ""), link_obj.get("relationship", ""))  # noqa: E501
        )
        
    connection.commit()
    return result

async def explain_code(connection: sqlite3.Connection, repo_id: int, repo_slug: str, file_path: str) -> str:  # noqa: E501
    """Synthesize a consolidated explanation for a file."""
    
    # 1. Fetch all implementation summaries for this file
    impls = connection.execute(
        "SELECT target_type, target_id, summary FROM implementation_summaries WHERE repo_id = ? AND file_path = ?",  # noqa: E501
        (repo_id, file_path,)
    ).fetchall()
    
    if not impls:
        return f"No architectural implementation context found for {file_path}."
        
    context = f"File: {file_path}\n\n"
    for imp in impls:
        target_ref = f"{imp['target_type']} #{imp['target_id']}"
        context += f"From {target_ref}: {imp['summary']}\n"
        
        # Pull ADRs for that target
        if imp['target_type'] == 'pr':
            target_row = connection.execute("SELECT id FROM prs WHERE repo_id = ? AND number = ?", (repo_id, imp['target_id'])).fetchone()  # noqa: E501
            if target_row:
                adrs = connection.execute("SELECT summary, outcome, rationale FROM architectural_decisions WHERE pr_id = ?", (target_row['id'],)).fetchall()  # noqa: E501
            else:
                adrs = []
        else:
            target_row = connection.execute("SELECT id FROM issues WHERE repo_id = ? AND number = ?", (repo_id, imp['target_id'])).fetchone()  # noqa: E501
            if target_row:
                adrs = connection.execute("SELECT summary, outcome, rationale FROM architectural_decisions WHERE issue_id = ?", (target_row['id'],)).fetchall()  # noqa: E501
            else:
                adrs = []
                
        if adrs:
            context += "Decisions:\n"
            for adr in adrs:
                context += f"- {adr['outcome']}: {adr['summary']} (Rationale: {adr['rationale']})\n"
        context += "\n"
        
    # Generate final explanation
    settings = load_settings()
    api_key = settings.llm_api_key
    if not api_key:
        return context # Fallback
        
    prompt = "Synthesize a consolidated explanation of why this file looks the way it does, combining implementation summaries, decisions, and rationale."  # noqa: E501
    payload = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": context}
        ]
    }
    
    url = "https://api.openai.com/v1/chat/completions"
    async with httpx.AsyncClient(timeout=60) as client:
        try:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload
            )
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except Exception:
            return context
