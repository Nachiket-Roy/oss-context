"""Review-assistant summaries for oss-context.

This module builds higher-level merge-readiness and follow-up guidance from the
synced PR health, unresolved-thread state, and extracted cross-reference graph.
"""

from __future__ import annotations

from typing import Any

from oss_context.queries import get_pr_context_payload


def _reference_label(reference: dict[str, Any]) -> str:
    """Render a compact human-readable label for a linked reference."""
    if reference.get("target_repo") and reference.get("target_number") is not None:
        return f"{reference['target_repo']}#{reference['target_number']}"
    if reference.get("target_repo") and reference.get("target_sha"):
        return f"{reference['target_repo']}@{reference['target_sha']}"
    if reference.get("url"):
        return str(reference["url"])
    return str(reference.get("raw_text") or "reference")


def get_merge_readiness_payload(
    connection,
    *,
    repo: str,
    pr_number: int,
    stale_days: int = 3,
) -> dict[str, Any]:
    """Summarize what remains before a PR is likely ready to merge."""
    payload = get_pr_context_payload(connection, repo=repo, pr_number=pr_number)
    health = payload["health"]
    unresolved_threads = payload["unresolved_threads"]
    blocking_threads = [thread for thread in unresolved_threads if thread["blocking"]]
    waiting_on_author = [
        thread for thread in unresolved_threads if thread["reviewer_state"] == "PENDING_AUTHOR"
    ]
    waiting_on_reviewer = [
        thread for thread in unresolved_threads if thread["reviewer_state"] == "WAITING_ON_REVIEWER"
    ]
    stale_threads = [thread for thread in unresolved_threads if thread["age_days"] >= stale_days]

    merge_readiness_score = int(health["health_score"])
    merge_readiness_score -= len(waiting_on_author) * 5
    merge_readiness_score -= len(stale_threads) * 4
    merge_readiness_score += min(len(waiting_on_reviewer) * 4, 12)
    if not unresolved_threads:
        merge_readiness_score = min(100, merge_readiness_score + 15)
    merge_readiness_score = max(0, min(100, merge_readiness_score))

    if blocking_threads:
        readiness_label = "needs author action"
        summary = (
            f"{len(blocking_threads)} blocking review thread"
            f"{'s' if len(blocking_threads) != 1 else ''} still need to be addressed."
        )
    elif waiting_on_reviewer:
        readiness_label = "waiting on reviewer follow-up"
        summary = (
            f"No blocking threads remain; {len(waiting_on_reviewer)} thread"
            f"{'s' if len(waiting_on_reviewer) != 1 else ''} now wait on reviewer follow-up."
        )
    elif unresolved_threads:
        readiness_label = "close, with open discussion"
        summary = (
            "Only non-blocking discussion remains across "
            f"{len(unresolved_threads)} unresolved thread"
            f"{'s' if len(unresolved_threads) != 1 else ''}."
        )
    else:
        readiness_label = "ready from synced review state"
        summary = "No unresolved review threads remain in the synced context."

    recommended_actions: list[str] = []
    for thread in blocking_threads[:3]:
        recommended_actions.append(
            f"Address {thread['reviewer']}'s blocking feedback in "
            f"{thread['file_path']}: {thread['summary']}"
        )
    for thread in waiting_on_author[:2]:
        if not thread["blocking"]:
            recommended_actions.append(
                f"Reply or update {thread['file_path']} for "
                f"{thread['reviewer']}: {thread['summary']}"
            )
    for thread in waiting_on_reviewer[:2]:
        recommended_actions.append(
            f"Follow up with {thread['reviewer']} on {thread['file_path']} "
            "after the latest author response."
        )
    if not recommended_actions:
        recommended_actions.append("PR looks clear based on the synced review graph.")

    follow_up_reviewers = sorted({thread["reviewer"] for thread in waiting_on_reviewer})
    linked_references = [_reference_label(reference) for reference in payload["references"][:10]]
    return {
        "repo": repo,
        "pr_number": pr_number,
        "title": health["title"],
        "author": health["author"],
        "state": health["state"],
        "health_score": health["health_score"],
        "merge_readiness_score": merge_readiness_score,
        "readiness_label": readiness_label,
        "summary": summary,
        "unresolved_threads": len(unresolved_threads),
        "blocking_threads": len(blocking_threads),
        "waiting_on_author_threads": len(waiting_on_author),
        "waiting_on_reviewer_threads": len(waiting_on_reviewer),
        "stale_threads": len(stale_threads),
        "stale_days": stale_days,
        "recommended_actions": recommended_actions,
        "follow_up_reviewers": follow_up_reviewers,
        "linked_references": linked_references,
        "references": payload["references"],
    }
