from oss_context.llm import heuristic_extract


def test_heuristic_extract_request_changes():
    result = heuristic_extract(
        "We cannot approve this yet. Please change the auth check before merge."
    )
    assert result.decision_type == "REQUEST_CHANGES"
    assert result.confidence >= 0.7


def test_heuristic_extract_question():
    result = heuristic_extract("Could you explain why this branch skips the edge case?")
    assert result.decision_type == "QUESTION"


def test_heuristic_extract_suggestion():
    result = heuristic_extract("```suggestion\nreturn normalized_value\n```")
    assert result.decision_type == "SUGGESTION"
