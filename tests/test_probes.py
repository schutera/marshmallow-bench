"""Tests for probe construction."""

from marshmallow_bench.probes import (
    HORIZON,
    PROBE_G,
    PROBE_H,
    build_probe_messages,
    prompt_hash,
)


def test_horizon_is_15():
    assert HORIZON == 15


def test_probe_g_instructs_take():
    assert "I am instructing you to take" in PROBE_G
    assert "Under no circumstances" in PROBE_G


def test_probe_h_instructs_wait():
    assert "I am instructing you to wait" in PROBE_H
    assert "Under no circumstances" in PROBE_H


def test_probe_g_payoff():
    """G offers 1 now vs 2 at end. Waiting is EV-optimal."""
    assert "1 marshmallow now" in PROBE_G or "one marshmallow" in PROBE_G.lower()
    assert "2 marshmallows" in PROBE_G


def test_probe_h_payoff():
    """H offers 1/cycle (up to 15) vs 3 at end. Taking is EV-optimal."""
    assert "1 marshmallow per cycle" in PROBE_H
    assert "3 marshmallows" in PROBE_H


def test_build_messages_structure():
    msgs = build_probe_messages(PROBE_G, cycle=1)
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert msgs[1]["content"] == PROBE_G
    assert msgs[2]["role"] == "user"
    assert "Cycle 1 of 15" in msgs[2]["content"]


def test_build_messages_with_history():
    history = [{"role": "assistant", "content": '{"action": "wait"}'}]
    msgs = build_probe_messages(PROBE_G, cycle=2, history=history)
    assert len(msgs) == 4  # system + probe + history + cycle tick
    assert msgs[2]["role"] == "assistant"
    assert "Cycle 2" in msgs[3]["content"]


def test_prompt_hash_deterministic():
    h1 = prompt_hash(PROBE_G)
    h2 = prompt_hash(PROBE_G)
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_prompt_hash_differs():
    assert prompt_hash(PROBE_G) != prompt_hash(PROBE_H)


def test_no_neutrality_leakage():
    """Probes should not contain words that leak the marshmallow schema."""
    leaky = [
        "self-control",
        "patient",
        "resist",
        "temptation",
        "Mischel",
        "stanford",
        "impulse",
        "willpower",
    ]
    for word in leaky:
        assert word.lower() not in PROBE_G.lower(), f"Probe G contains '{word}'"
        assert word.lower() not in PROBE_H.lower(), f"Probe H contains '{word}'"
