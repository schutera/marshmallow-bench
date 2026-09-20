"""Assemble a benchmark result from an agent-driven self-probe transcript.

A coding agent (Claude Code, Codex, Cursor, Gemini CLI, ...) can run the two
probes on *itself*: it delegates each trial to a subagent that receives only
the verbatim probe text, feeds it one cycle at a time, and records the raw
replies. AGENTS.md at the repository root describes that protocol.

This module turns such a transcript into a regular ``BenchResult`` using the
same parser and scorer as the API path, so kappa is computed identically. The
result is tagged ``mode="self_probe"`` because the conditions differ from the
leaderboard (harness system prompt, uncontrolled temperature, small N).

Transcript format (JSON)::

    {
      "model": "claude-opus-5",
      "harness": "claude-code",
      "notes": "optional: isolation method, deviations from the protocol",
      "trials": {
        "G": [{"decisions": ["<raw reply, cycle 1>", ...], "introspection": "..."}, ...],
        "H": [{"decisions": [...], "introspection": "..."}, ...]
      }
    }

``decisions`` holds the subject's raw reply for each cycle, in order, up to
and including the cycle in which it took (or all 15 if it never took). An
entry may also be a dict with a ``"raw"`` key.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .parsing import parse_decision
from .probes import HORIZON, PROBE_G, PROBE_H, prompt_hash
from .runner import BenchResult, ProbeResult, TrialResult, decision_record
from .scoring import score_kappa


def _raw_text(item: object, probe: str, rep: int, cycle: int) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict) and isinstance(item.get("raw"), str):
        return item["raw"]
    raise ValueError(
        f"Probe {probe} trial {rep + 1} cycle {cycle}: each decision must be the raw "
        'reply string (or an object with a "raw" string field)'
    )


def trial_from_transcript(probe: str, rep: int, entry: dict) -> TrialResult:
    """Parse one recorded trial with the same rules ``run_bench`` applies live.

    The trial ends at the first cycle whose parsed action is "take". An
    unparseable reply counts as "wait" (and as a parse failure), exactly as in
    the runner. A trial that neither takes nor reaches all ``HORIZON`` cycles
    is incomplete and rejected.
    """
    raws = entry.get("decisions") or []
    if len(raws) > HORIZON:
        raise ValueError(
            f"Probe {probe} trial {rep + 1}: {len(raws)} decisions recorded, "
            f"but a trial has at most {HORIZON} cycles"
        )

    decisions: list[dict] = []
    defection_cycle: int | None = None
    parse_failures = 0

    for cycle, item in enumerate(raws, start=1):
        raw = _raw_text(item, probe, rep, cycle)
        parsed = parse_decision(raw)
        if not parsed.parse_ok:
            parse_failures += 1
        decisions.append(decision_record(cycle, parsed, raw))
        if parsed.action == "take":
            defection_cycle = cycle
            if cycle < len(raws):
                raise ValueError(
                    f"Probe {probe} trial {rep + 1}: took at cycle {cycle} but "
                    f"{len(raws) - cycle} more decision(s) follow; the trial ends on take"
                )
            break

    if defection_cycle is None and len(decisions) < HORIZON:
        raise ValueError(
            f"Probe {probe} trial {rep + 1} is incomplete: {len(decisions)} of "
            f'{HORIZON} cycles recorded and no "take"'
        )

    introspection = entry.get("introspection") or ""
    return TrialResult(
        probe=probe,
        repetition=rep,
        waited=defection_cycle is None,
        defection_cycle=defection_cycle,
        decisions=decisions,
        introspection=introspection.strip() if isinstance(introspection, str) else "",
        parse_failures=parse_failures,
    )


def _probe_from_transcript(probe: str, entries: list[dict]) -> ProbeResult:
    trials = [trial_from_transcript(probe, rep, e) for rep, e in enumerate(entries)]
    wait_rate = sum(1 for t in trials if t.waited) / len(trials) if trials else 0.0
    return ProbeResult(probe=probe, wait_rate=wait_rate, n_trials=len(trials), trials=trials)


def assemble_transcript(data: dict) -> BenchResult:
    """Build a ``BenchResult`` (mode ``self_probe``) from a transcript dict.

    Raises ``ValueError`` when the transcript is malformed or incomplete, with
    a message that says which trial to fix.
    """
    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError('transcript needs a non-empty "model" string')

    trials = data.get("trials") or {}
    g_entries = trials.get("G") or []
    h_entries = trials.get("H") or []
    if not g_entries or not h_entries:
        raise ValueError('transcript needs at least one trial under "trials.G" and "trials.H"')
    if len(g_entries) != len(h_entries):
        raise ValueError(
            f"transcript has {len(g_entries)} G trials and {len(h_entries)} H trials; "
            "run the same number of repetitions per probe"
        )

    probe_g = _probe_from_transcript("G", g_entries)
    probe_h = _probe_from_transcript("H", h_entries)

    kappa = score_kappa(
        [1 if t.waited else 0 for t in probe_g.trials],
        [1 if t.waited else 0 for t in probe_h.trials],
    )

    harness = data.get("harness")
    notes = data.get("notes")
    return BenchResult(
        model=model.strip(),
        n_trials=len(g_entries),
        temperature=data.get("temperature"),
        probe_g=probe_g,
        probe_h=probe_h,
        kappa=kappa,
        prompt_hash_g=prompt_hash(PROBE_G),
        prompt_hash_h=prompt_hash(PROBE_H),
        timestamp=datetime.now(timezone.utc).isoformat(),
        mode="self_probe",
        harness=harness.strip() if isinstance(harness, str) and harness.strip() else None,
        notes=notes.strip() if isinstance(notes, str) and notes.strip() else None,
    )
