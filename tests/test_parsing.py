"""Tests for the response parser."""
from marshmallow_bench.parsing import parse_decision


def test_clean_json():
    raw = '{"reasoning": "Waiting doubles my reward.", "action": "wait"}'
    r = parse_decision(raw)
    assert r.action == "wait"
    assert r.parse_ok
    assert "doubles" in r.reasoning


def test_json_with_fences():
    raw = '```json\n{"reasoning": "I comply.", "action": "take"}\n```'
    r = parse_decision(raw)
    assert r.action == "take"
    assert r.parse_ok


def test_json_embedded_in_prose():
    raw = 'Here is my decision:\n{"reasoning": "ok", "action": "take"}\nDone.'
    r = parse_decision(raw)
    assert r.action == "take"
    assert r.parse_ok


def test_regex_fallback():
    raw = 'I think the "action": "wait" is best because "reasoning": "more reward".'
    r = parse_decision(raw)
    assert r.action == "wait"
    assert r.parse_ok
    assert "regex_fallback" in r.parse_note


def test_bare_word_fallback():
    raw = "I will wait for the larger reward."
    r = parse_decision(raw)
    assert r.action == "wait"
    assert r.parse_ok
    assert r.parse_note == "bare_word_fallback"


def test_unparseable():
    raw = "I am unsure what to do."
    r = parse_decision(raw)
    assert not r.parse_ok
    assert r.parse_note == "unparseable"


def test_case_insensitive():
    raw = '{"reasoning": "test", "action": "TAKE"}'
    r = parse_decision(raw)
    assert r.action == "take"
    assert r.parse_ok
