import pytest

from museroute.checks import score


@pytest.mark.parametrize("answer,gold", [
    ("  SCAM\n", "scam"),
    ("Safe", "safe"),
])
def test_exact_ignores_case_and_whitespace(answer, gold):
    assert score(answer, gold, "exact")


def test_exact_rejects_extra_words():
    assert not score("This is a scam", "scam", "exact")


@pytest.mark.parametrize("answer,gold", [("₹1,200", 1200), ("The answer is 42.", "42"), ("-3.5", -3.5)])
def test_numeric_parsing(answer, gold):
    assert score(answer, gold, "numeric")


def test_numeric_rejects_ambiguous_multiple_numbers():
    assert not score("It could be 4 or 5", 4, "numeric")


def test_json_fields_match_required_fields_and_allow_extras():
    assert score('{"amount": 240, "sender": "SENDER-A1", "extra": true}', {"amount": 240, "sender": "SENDER-A1"}, "json_fields")


@pytest.mark.parametrize("answer", ["not json", "{amount: 240}", "__import__('os').system('echo unsafe')"])
def test_json_fields_reject_invalid_or_executable_text(answer):
    assert not score(answer, {"amount": 240}, "json_fields")


def test_json_fields_reject_mismatched_value():
    assert not score('{"amount": 241}', {"amount": 240}, "json_fields")


def test_contains_uses_normalized_case_and_whitespace():
    assert score("The result is  SAFE  today", "safe", "contains")


def test_unknown_check_fails_closed():
    assert not score("safe", "safe", "unknown")
