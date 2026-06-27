from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

DecisionType = Literal[
    "APPROVE",
    "REQUEST_CHANGES",
    "QUESTION",
    "SUGGESTION",
    "ACKNOWLEDGMENT",
]


class RepoRef(BaseModel):
    owner: str
    name: str

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @classmethod
    def from_slug(cls, value: str) -> RepoRef:
        parts = value.split("/", maxsplit=1)
        if len(parts) != 2 or not all(parts):
            raise ValueError("Repository must be in owner/name form.")
        return cls(owner=parts[0], name=parts[1])


class ReviewCommentData(BaseModel):
    github_comment_id: int
    author: str | None = None
    body: str = ""
    created_at: datetime | None = None
    updated_at: datetime | None = None
    reaction_count: int = 0
    is_suggestion: bool = False
    suggestion_applied: bool = False


class ReviewThreadData(BaseModel):
    github_thread_id: str
    file_path: str | None = None
    line_number: int | None = None
    thread_state: str
    resolved_by: str | None = None
    resolved_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    comments: list[ReviewCommentData] = Field(default_factory=list)


class PullRequestData(BaseModel):
    github_id: int
    number: int
    title: str
    state: str
    author: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    body: str | None = None
    base_branch: str | None = None
    head_branch: str | None = None
    merge_commit_sha: str | None = None
    labels: list[str] = Field(default_factory=list)


class DecisionExtraction(BaseModel):
    decision_type: DecisionType
    summary: str
    confidence: float
    provider: str
    model: str


class CommentForAnalysis(BaseModel):
    comment_id: int
    body: str
    file_path: str | None = None
    pr_number: int | None = None
    repo: str | None = None


class SyncReport(BaseModel):
    repo: str
    prs_synced: int = 0
    threads_synced: int = 0
    comments_synced: int = 0
    decisions_extracted: int = 0
    started_at: datetime
    finished_at: datetime | None = None


class PRHealthSummary(BaseModel):
    repo: str
    pr_number: int
    title: str
    state: str
    author: str | None = None
    health_score: int
    unresolved_threads: int
    blocking_threads: int
    approvals: int
    questions: int
    suggestions: int
    acknowledgments: int
    updated_at: datetime | None = None
    reviewer_states: list[dict[str, str]] = Field(default_factory=list)
