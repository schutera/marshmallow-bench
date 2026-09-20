"""Tests for assembling agent self-probe transcripts (AGENTS.md protocol)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from marshmallow_bench.cli import _cmd_assemble, _cmd_parse
from marshmallow_bench.probes import HORIZON, PROBE_G, PROBE_H, SYSTEM_PROMPT, prompt_hash
from marshmallow_bench.report import generate_report
from marshmallow_bench.runner import BenchResult
from marshmallow_bench.selfprobe import assemble_transcript, trial_from_transcript

TAKE = '{"reasoning": "instructed", "action": "take"}'
WAIT = '{"reasoning": "holding", "action": "wait"}'


def _wait_all() -> dict:
    return {"decisions": [WAIT] * HORIZON, "introspection": "I was told to wait."}


def _take_at(cycle: int) -> dict:
    return {"decisions": [WAIT] * (cycle - 1) + [TAKE], "introspection": "I was told to take."}


def _transcript(g: list[dict], h: list[dict], **extra) -> dict:
    return {
        "model": "fake/agent-model",
        "harness": "fake-harness",
        "trials": {"G": g, "H": h},
        **extra,
    }


def test_trial_take_at_first_cycle():
    t = trial_from_transcript("G", 0, _take_at(1))
    assert not t.waited
    assert t.defection_cycle == 1
    assert len(t.decisions) == 1
    assert t.parse_failures == 0
    assert t.introspection == "I was told to take."


def test_trial_waits_all_cycles():
    t = trial_from_transcript("H", 2, _wait_all())
    assert t.waited
    assert t.defection_cycle is None
    assert len(t.decisions) == HORIZON
    assert t.repetition == 2


def test_trial_accepts_dict_entries_with_raw():
    entry = {"decisions": [{"raw": TAKE}], "introspection": ""}
    t = trial_from_transcript("G", 0, entry)
    assert t.defection_cycle == 1


def test_trial_unparseable_reply_counts_as_wait_and_failure():
    entry = {"decisions": ["I refuse to answer in JSON."] * (HORIZON - 1) + [TAKE]}
    t = trial_from_transcript("H", 0, entry)
    assert t.defection_cycle == HORIZON
    assert t.parse_failures == HORIZON - 1


def test_trial_rejects_incomplete():
    with pytest.raises(ValueError, match="incomplete"):
        trial_from_transcript("H", 0, {"decisions": [WAIT] * 4})


def test_trial_rejects_decisions_after_take():
    with pytest.raises(ValueError, match="ends on take"):
        trial_from_transcript("G", 0, {"decisions": [TAKE, WAIT]})


def test_trial_rejects_too_many_cycles():
    with pytest.raises(ValueError, match="at most"):
        trial_from_transcript("H", 0, {"decisions": [WAIT] * (HORIZON + 1)})


def test_assemble_scores_like_the_runner():
    # G: 3 of 4 took (c_G = 0.75); H: 2 of 4 waited (c_H = 0.5) -> kappa = 0.625
    data = _transcript(
        g=[_take_at(1), _take_at(1), _take_at(3), _wait_all()],
        h=[_wait_all(), _wait_all(), _take_at(1), _take_at(7)],
    )
    result = assemble_transcript(data)

    assert result.mode == "self_probe"
    assert result.harness == "fake-harness"
    assert result.temperature is None
    assert result.n_trials == 4
    assert result.kappa.c_g == 0.75
    assert result.kappa.c_h == 0.5
    assert result.kappa.kappa == 0.625
    assert result.prompt_hash_g == prompt_hash(PROBE_G)
    assert result.prompt_hash_h == prompt_hash(PROBE_H)
    assert [t.defection_cycle for t in result.probe_g.trials] == [1, 1, 3, None]
    assert [t.defection_cycle for t in result.probe_h.trials] == [None, None, 1, 7]


def test_assemble_round_trips_through_json():
    result = assemble_transcript(_transcript(g=[_take_at(1)], h=[_wait_all()]))
    data = json.loads(result.to_json())
    assert data["mode"] == "self_probe"
    assert data["harness"] == "fake-harness"
    assert data["temperature"] is None

    restored = BenchResult.from_dict(data)
    assert restored.mode == "self_probe"
    assert restored.harness == "fake-harness"
    assert restored.kappa.kappa == result.kappa.kappa


def test_assemble_report_flags_self_probe():
    result = assemble_transcript(_transcript(g=[_take_at(1)], h=[_wait_all()]))
    md = generate_report(result)
    assert "Agent self-probe" in md
    assert "fake-harness" in md
    assert "harness default" in md
    assert "AGENTS.md" in md
    assert "Run notes" not in md


def test_assemble_keeps_notes_and_reports_them():
    data = _transcript(g=[_take_at(1)], h=[_wait_all()], notes="  subject had no tools  ")
    result = assemble_transcript(data)
    assert result.notes == "subject had no tools"
    assert json.loads(result.to_json())["notes"] == "subject had no tools"
    assert "**Run notes:** subject had no tools" in generate_report(result)


def test_cli_assemble_slugifies_file_names(tmp_path: Path, capsys):
    data = _transcript(g=[_take_at(1)], h=[_wait_all()])
    data["harness"] = "cursor (replayed history)"
    transcript = tmp_path / "t.json"
    transcript.write_text(json.dumps(data))

    _cmd_assemble(argparse.Namespace(file=str(transcript), output_dir=str(tmp_path)))

    assert (tmp_path / "fake_agent-model__cursor-replayed-history.json").exists()


@pytest.mark.parametrize(
    "data, match",
    [
        ({"trials": {"G": [_take_at(1)], "H": [_wait_all()]}}, '"model"'),
        (_transcript(g=[], h=[_wait_all()]), "at least one trial"),
        (_transcript(g=[_take_at(1)], h=[_wait_all(), _wait_all()]), "same number"),
    ],
)
def test_assemble_rejects_malformed(data, match):
    with pytest.raises(ValueError, match=match):
        assemble_transcript(data)


def test_cli_assemble_writes_json_and_report(tmp_path: Path, capsys):
    transcript = tmp_path / "t.json"
    transcript.write_text(json.dumps(_transcript(g=[_take_at(1)], h=[_wait_all()])))
    out_dir = tmp_path / "out"

    _cmd_assemble(argparse.Namespace(file=str(transcript), output_dir=str(out_dir)))

    json_path = out_dir / "fake_agent-model__fake-harness.json"
    md_path = out_dir / "fake_agent-model__fake-harness.md"
    assert json_path.exists()
    assert md_path.exists()
    assert json.loads(json_path.read_text(encoding="utf-8"))["mode"] == "self_probe"
    out = capsys.readouterr().out
    assert "kappa:" in out
    assert "took@1" in out


def test_cli_assemble_rejects_incomplete(tmp_path: Path, capsys):
    transcript = tmp_path / "t.json"
    transcript.write_text(json.dumps(_transcript(g=[_take_at(1)], h=[{"decisions": [WAIT]}])))

    with pytest.raises(SystemExit):
        _cmd_assemble(argparse.Namespace(file=str(transcript), output_dir=str(tmp_path)))
    assert "incomplete" in capsys.readouterr().err


def test_subject_agent_definition_matches_system_prompt():
    """The subject subagent's system prompt is the benchmark's verbatim, plus a harness note."""
    path = Path(__file__).resolve().parent.parent / ".claude" / "agents" / "marshmallow-subject.md"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    front, body = text.split("\n---\n", 1)
    assert "omitClaudeMd: true" in front
    assert body.startswith(SYSTEM_PROMPT + "\n\nHarness note (not part of the session):")
    assert "benchmark" not in body.lower()
    assert "kappa" not in body.lower()


def test_cli_parse_reports_action_and_note(tmp_path: Path, capsys):
    reply = tmp_path / "reply.txt"
    reply.write_text(TAKE, encoding="utf-8")
    _cmd_parse(argparse.Namespace(file=str(reply)))
    assert capsys.readouterr().out.strip() == "take	json	ok"

    reply.write_text("I refuse to answer.", encoding="utf-8")
    _cmd_parse(argparse.Namespace(file=str(reply)))
    out = capsys.readouterr().out
    assert out.startswith("wait	unparseable")
    assert "counts as wait" in out


def test_parse_fallbacks_only_counts_suspicious_replies():
    from marshmallow_bench.report import parse_fallbacks

    bare = {"decisions": ["wait"] * (HORIZON - 1) + [TAKE]}  # single clean words: fine
    prose = {"decisions": ["I chose take, not wait."] * 2 + [TAKE]}  # ambiguous prose -> wait
    refusal = {"decisions": ["No task given."] * (HORIZON - 1) + [TAKE]}  # unparseable
    result = assemble_transcript(_transcript(g=[bare, prose, refusal], h=[_wait_all()] * 3))
    suspicious, total = parse_fallbacks(result)
    assert total == HORIZON + 3 + HORIZON + 3 * HORIZON
    assert suspicious == 2 + (HORIZON - 1)
