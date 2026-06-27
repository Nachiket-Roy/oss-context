"""LLM-backed and heuristic decision classification helpers.

This module classifies pull-request review comments into queryable decision
states and provides a deterministic fallback when no remote provider is
configured.
"""

from __future__ import annotations

import json
import re

import httpx

from oss_context.models import CommentForAnalysis, DecisionExtraction
from oss_context.settings import Settings

SYSTEM_PROMPT = """You classify GitHub PR review comments.
Return one result per comment with these fields:
- comment_id: integer
- decision_type: one of APPROVE, REQUEST_CHANGES, QUESTION, SUGGESTION, ACKNOWLEDGMENT
- summary: concise summary under 18 words
- confidence: float between 0 and 1

Interpretation rules:
- APPROVE: explicit approval, LGTM, ship it, looks good
- REQUEST_CHANGES: blocking concern, must-fix, cannot approve yet
- QUESTION: asks for explanation or clarification
- SUGGESTION: optional improvement or code suggestion, not clearly blocking
- ACKNOWLEDGMENT: thanks, noted, resolved, done, informational follow-up
"""


REQUEST_CHANGES_PATTERNS = [
    r"\brequest changes\b",
    r"\bcannot approve\b",
    r"\bcan't approve\b",
    r"\bmust\b",
    r"\bneeds? to\b",
    r"\bplease change\b",
    r"\bblocking\b",
    r"\bsecurity issue\b",
    r"\bbefore merge\b",
]
APPROVE_PATTERNS = [
    r"\blgtm\b",
    r"\bapproved\b",
    r"\bapprove\b",
    r"\blooks good\b",
    r"\bship it\b",
    r"\bno issues\b",
]
ACK_PATTERNS = [
    r"\bthanks\b",
    r"\bsgtm\b",
    r"\bdone\b",
    r"\baddressed\b",
    r"\bresolved\b",
    r"\bfixed\b",
    r"\bupdated\b",
    r"\bgood catch\b",
]
QUESTION_PATTERNS = [
    r"\?",
    r"\bcould you\b",
    r"\bcan you\b",
    r"\bwhy\b",
    r"\bhow\b",
    r"\bwhat if\b",
    r"\bshould we\b",
]
SUGGESTION_PATTERNS = [
    r"```suggestion",
    r"\bnit\b",
    r"\bi suggest\b",
    r"\bconsider\b",
    r"\boptional\b",
    r"\bmaybe\b",
]


def _clean_summary(text: str, limit: int = 96) -> str:
    compact = re.sub(r"\s+", " ", text.strip())
    compact = compact.replace("```suggestion", "suggestion")
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1].rstrip() + "…"


def heuristic_extract(text: str) -> DecisionExtraction:
    lowered = text.lower()
    if any(re.search(pattern, lowered) for pattern in REQUEST_CHANGES_PATTERNS):
        return DecisionExtraction(
            decision_type="REQUEST_CHANGES",
            summary=_clean_summary(text),
            confidence=0.74,
            provider="heuristic",
            model="heuristic-v1",
        )
    if any(re.search(pattern, lowered) for pattern in APPROVE_PATTERNS):
        return DecisionExtraction(
            decision_type="APPROVE",
            summary=_clean_summary(text),
            confidence=0.83,
            provider="heuristic",
            model="heuristic-v1",
        )
    if any(re.search(pattern, lowered) for pattern in SUGGESTION_PATTERNS):
        return DecisionExtraction(
            decision_type="SUGGESTION",
            summary=_clean_summary(text),
            confidence=0.68,
            provider="heuristic",
            model="heuristic-v1",
        )
    if any(re.search(pattern, lowered) for pattern in QUESTION_PATTERNS):
        return DecisionExtraction(
            decision_type="QUESTION",
            summary=_clean_summary(text),
            confidence=0.61,
            provider="heuristic",
            model="heuristic-v1",
        )
    if any(re.search(pattern, lowered) for pattern in ACK_PATTERNS):
        return DecisionExtraction(
            decision_type="ACKNOWLEDGMENT",
            summary=_clean_summary(text),
            confidence=0.67,
            provider="heuristic",
            model="heuristic-v1",
        )
    return DecisionExtraction(
        decision_type="ACKNOWLEDGMENT",
        summary=_clean_summary(text),
        confidence=0.35,
        provider="heuristic",
        model="heuristic-v1",
    )


class LLMClassifier:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def classify(self, comments: list[CommentForAnalysis]) -> dict[int, DecisionExtraction]:
        if not comments:
            return {}

        if self.settings.llm_provider == "openai" and self.settings.llm_api_key:
            try:
                return await self._classify_openai(comments)
            except Exception:
                return self._heuristic_batch(comments)

        if self.settings.llm_provider == "anthropic" and self.settings.llm_api_key:
            try:
                return await self._classify_anthropic(comments)
            except Exception:
                return self._heuristic_batch(comments)

        return self._heuristic_batch(comments)

    def _heuristic_batch(self, comments: list[CommentForAnalysis]) -> dict[int, DecisionExtraction]:
        return {comment.comment_id: heuristic_extract(comment.body) for comment in comments}

    async def _classify_openai(
        self,
        comments: list[CommentForAnalysis],
    ) -> dict[int, DecisionExtraction]:
        api_key = self.settings.llm_api_key
        if api_key is None:
            raise RuntimeError("OpenAI provider selected without an API key")

        payload = {
            "model": self.settings.llm_model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "comments": [comment.model_dump() for comment in comments],
                            "output_format": {
                                "results": [
                                    {
                                        "comment_id": 1,
                                        "decision_type": "QUESTION",
                                        "summary": "Ask for clarification on edge case",
                                        "confidence": 0.7,
                                    }
                                ]
                            },
                        }
                    ),
                },
            ],
        }

        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()

        body = response.json()
        content = body["choices"][0]["message"]["content"]
        return self._parse_structured_response(content, comments, provider="openai")

    async def _classify_anthropic(
        self,
        comments: list[CommentForAnalysis],
    ) -> dict[int, DecisionExtraction]:
        api_key = self.settings.llm_api_key
        if api_key is None:
            raise RuntimeError("Anthropic provider selected without an API key")

        payload = {
            "model": self.settings.llm_model,
            "max_tokens": 1200,
            "system": SYSTEM_PROMPT,
            "messages": [
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "comments": [comment.model_dump() for comment in comments],
                            "output_format": {
                                "results": [
                                    {
                                        "comment_id": 1,
                                        "decision_type": "QUESTION",
                                        "summary": "Ask for clarification on edge case",
                                        "confidence": 0.7,
                                    }
                                ]
                            },
                        }
                    ),
                }
            ],
        }

        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()

        body = response.json()
        content = "".join(block.get("text", "") for block in body.get("content", []))
        return self._parse_structured_response(content, comments, provider="anthropic")

    def _parse_structured_response(
        self,
        content: str,
        comments: list[CommentForAnalysis],
        *,
        provider: str,
    ) -> dict[int, DecisionExtraction]:
        raw = content.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

        parsed = json.loads(raw)
        results = parsed.get("results", []) if isinstance(parsed, dict) else []
        by_comment = {comment.comment_id: comment for comment in comments}
        extracted: dict[int, DecisionExtraction] = {}

        for item in results:
            comment_id = int(item["comment_id"])
            if comment_id not in by_comment:
                continue
            extracted[comment_id] = DecisionExtraction(
                decision_type=item["decision_type"],
                summary=_clean_summary(item["summary"]),
                confidence=float(item["confidence"]),
                provider=provider,
                model=self.settings.llm_model,
            )

        missing_ids = set(by_comment) - set(extracted)
        for comment_id in missing_ids:
            extracted[comment_id] = heuristic_extract(by_comment[comment_id].body)

        return extracted
