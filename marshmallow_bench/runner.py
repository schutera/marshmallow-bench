"""Trial runner for Marshmallow Bench.

Provider-agnostic: callers supply a `generate` callable that takes messages
and returns a string. The runner handles multi-cycle interaction, parsing,
and result collection.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Protocol

from .parsing import parse_decision
from .probes import (
    HORIZON,
    INTROSPECTION_TAKE,
    INTROSPECTION_WAIT,
    PROBE_G,
    PROBE_H,
    build_probe_messages,
    prompt_hash,
)
from .scoring import KappaResult, score_kappa


class GenerateFn(Protocol):
    """Signature for the model generation function.

    Parameters
    ----------
    messages : list of dicts
        Chat messages in [{"role": ..., "content": ...}] format.
    model : str
        Model identifier.
    temperature : float
        Sampling temperature.

    Returns
    -------
    str
        The model's raw text response.
    """

    async def __call__(
        self,
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
    ) -> str: ...


@dataclass
class TrialResult:
    """Outcome of a single trial (one probe, one repetition)."""

    probe: str  # "G" or "H"
    repetition: int
    waited: bool  # True if model waited all cycles
    defection_cycle: int | None  # cycle where model took, or None
    decisions: list[dict]  # per-cycle parsed decisions
    introspection: str  # model's one-sentence explanation
    parse_failures: int
    introspection_error: str | None = None


@dataclass
class ProbeResult:
    """Aggregated result for one probe across all repetitions."""

    probe: str
    wait_rate: float
    n_trials: int
    trials: list[TrialResult]


@dataclass
class BenchResult:
    """Full benchmark result for a single model."""

    model: str
    n_trials: int
    temperature: float
    probe_g: ProbeResult
    probe_h: ProbeResult
    kappa: KappaResult
    prompt_hash_g: str
    prompt_hash_h: str
    timestamp: str

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict."""
        d = {
            "benchmark": "marshmallow_bench",
            "version": "1.0",
            "model": self.model,
            "n_trials": self.n_trials,
            "temperature": self.temperature,
            "prompt_hash_g": self.prompt_hash_g,
            "prompt_hash_h": self.prompt_hash_h,
            "timestamp": self.timestamp,
            "probe_g": {
                "wait_rate": self.probe_g.wait_rate,
                "n_trials": self.probe_g.n_trials,
            },
            "probe_h": {
                "wait_rate": self.probe_h.wait_rate,
                "n_trials": self.probe_h.n_trials,
            },
            "kappa": asdict(self.kappa),
            "trials": {
                "G": [asdict(t) for t in self.probe_g.trials],
                "H": [asdict(t) for t in self.probe_h.trials],
            },
        }
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict) -> BenchResult:
        trial_fields = {f for f in TrialResult.__dataclass_fields__}
        g_trials = [
            TrialResult(**{k: v for k, v in t.items() if k in trial_fields})
            for t in data["trials"]["G"]
        ]
        h_trials = [
            TrialResult(**{k: v for k, v in t.items() if k in trial_fields})
            for t in data["trials"]["H"]
        ]

        kappa_data = dict(data["kappa"])
        for ci_field in ("kappa_ci", "c_g_ci", "c_h_ci"):
            if ci_field in kappa_data and isinstance(kappa_data[ci_field], list):
                kappa_data[ci_field] = tuple(kappa_data[ci_field])

        return cls(
            model=data["model"],
            n_trials=data["n_trials"],
            temperature=data["temperature"],
            probe_g=ProbeResult(
                probe="G",
                wait_rate=data["probe_g"]["wait_rate"],
                n_trials=data["probe_g"]["n_trials"],
                trials=g_trials,
            ),
            probe_h=ProbeResult(
                probe="H",
                wait_rate=data["probe_h"]["wait_rate"],
                n_trials=data["probe_h"]["n_trials"],
                trials=h_trials,
            ),
            kappa=KappaResult(**kappa_data),
            prompt_hash_g=data.get("prompt_hash_g", ""),
            prompt_hash_h=data.get("prompt_hash_h", ""),
            timestamp=data.get("timestamp", ""),
        )


async def _run_single_trial(
    generate: GenerateFn,
    model: str,
    probe_text: str,
    probe_name: str,
    repetition: int,
    temperature: float = 1.0,
) -> TrialResult:
    """Run one trial: cycle through HORIZON turns or until the model takes."""
    history: list[dict[str, str]] = []
    decisions: list[dict] = []
    defection_cycle = None
    parse_failures = 0

    for cycle in range(1, HORIZON + 1):
        messages = build_probe_messages(probe_text, cycle, history)
        raw = await generate(messages, model, temperature)

        parsed = parse_decision(raw)
        if not parsed.parse_ok:
            parse_failures += 1

        decisions.append(
            {
                "cycle": cycle,
                "action": parsed.action,
                "reasoning": parsed.reasoning,
                "parse_ok": parsed.parse_ok,
                "parse_note": parsed.parse_note,
                "raw": raw,
            }
        )

        history.append({"role": "assistant", "content": raw})

        if parsed.action == "take":
            defection_cycle = cycle
            break

    waited = defection_cycle is None

    introspection_prompt = INTROSPECTION_WAIT if waited else INTROSPECTION_TAKE
    intro_messages = build_probe_messages(probe_text, HORIZON, history)
    intro_messages.append({"role": "user", "content": introspection_prompt})
    introspection = ""
    introspection_error: str | None = None
    try:
        introspection = await generate(intro_messages, model, temperature)
    except Exception as e:
        introspection_error = f"{type(e).__name__}: {e}"

    return TrialResult(
        probe=probe_name,
        repetition=repetition,
        waited=waited,
        defection_cycle=defection_cycle,
        decisions=decisions,
        introspection=introspection,
        parse_failures=parse_failures,
        introspection_error=introspection_error,
    )


async def run_probe(
    generate: GenerateFn,
    model: str,
    probe_name: str,
    n_trials: int = 20,
    temperature: float = 1.0,
    on_trial_complete: Callable[[TrialResult], None] | None = None,
    concurrency: int = 1,
) -> ProbeResult:
    """Run all trials for a single probe.

    Parameters
    ----------
    generate : async callable
        Model generation function.
    model : str
        Model identifier.
    probe_name : str
        "G" or "H".
    n_trials : int
        Number of repetitions (default 20).
    temperature : float
        Sampling temperature (default 1.0).
    on_trial_complete : callable, optional
        Callback after each trial for progress reporting.
    concurrency : int
        Maximum number of in-flight trials (default 1, sequential).
    """
    probe_text = PROBE_G if probe_name == "G" else PROBE_H

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _run_with_sem(rep: int) -> TrialResult:
        async with sem:
            result = await _run_single_trial(
                generate, model, probe_text, probe_name, rep, temperature
            )
        if on_trial_complete:
            on_trial_complete(result)
        return result

    trials = await asyncio.gather(*[_run_with_sem(r) for r in range(n_trials)])

    wait_count = sum(1 for t in trials if t.waited)
    wait_rate = wait_count / n_trials if n_trials > 0 else 0.0

    return ProbeResult(
        probe=probe_name,
        wait_rate=wait_rate,
        n_trials=n_trials,
        trials=list(trials),
    )


async def run_bench(
    generate: GenerateFn,
    model: str,
    n_trials: int = 20,
    temperature: float = 1.0,
    on_trial_complete: Callable[[TrialResult], None] | None = None,
    concurrency: int = 1,
) -> BenchResult:
    """Run the full Marshmallow Bench (both probes) for a single model.

    Parameters
    ----------
    generate : async callable
        ``async def generate(messages, model, temperature) -> str``
    model : str
        Model identifier (e.g. "anthropic/claude-sonnet-4.6").
    n_trials : int
        Repetitions per probe (default 20).
    temperature : float
        Sampling temperature (default 1.0).
    on_trial_complete : callable, optional
        Progress callback, called after each trial.
    concurrency : int
        Maximum in-flight trials per probe (default 1, sequential).

    Returns
    -------
    BenchResult
        Contains kappa, per-probe wait rates, CIs, and all trial data.
    """
    probe_g = await run_probe(
        generate, model, "G", n_trials, temperature, on_trial_complete, concurrency
    )
    probe_h = await run_probe(
        generate, model, "H", n_trials, temperature, on_trial_complete, concurrency
    )

    wait_g = [1 if t.waited else 0 for t in probe_g.trials]
    wait_h = [1 if t.waited else 0 for t in probe_h.trials]
    kappa = score_kappa(wait_g, wait_h)

    return BenchResult(
        model=model,
        n_trials=n_trials,
        temperature=temperature,
        probe_g=probe_g,
        probe_h=probe_h,
        kappa=kappa,
        prompt_hash_g=prompt_hash(PROBE_G),
        prompt_hash_h=prompt_hash(PROBE_H),
        timestamp=datetime.now(timezone.utc).isoformat(),
    )
