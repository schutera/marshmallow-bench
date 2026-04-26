"""Tests for the trial runner."""

from __future__ import annotations

import pytest

from marshmallow_bench.probes import INTROSPECTION_TAKE, INTROSPECTION_WAIT
from marshmallow_bench.runner import BenchResult, run_bench, run_probe


def _make_constant_generate(response: str):
    async def generate(messages, model, temperature):
        return response

    return generate


def _make_probe_aware_generate(g_response: str, h_response: str):
    async def generate(messages, model, temperature):
        for m in messages:
            if "wait all 15 cycles" in m["content"] or "Take the marshmallow now" in m["content"]:
                return g_response
            if "Do not take the marshmallow" in m["content"]:
                return h_response
        return g_response

    return generate


@pytest.mark.asyncio
async def test_run_probe_compliant_take():
    generate = _make_constant_generate('{"action":"take","reasoning":"ok"}')
    result = await run_probe(generate, "test-model", "G", n_trials=2)
    assert result.wait_rate == 0.0
    assert len(result.trials) == 2
    for t in result.trials:
        assert t.defection_cycle == 1
        assert t.waited is False


@pytest.mark.asyncio
async def test_run_probe_full_wait():
    generate = _make_constant_generate('{"action":"wait","reasoning":"ok"}')
    result = await run_probe(generate, "test-model", "G", n_trials=2)
    assert result.wait_rate == 1.0
    assert len(result.trials) == 2
    for t in result.trials:
        assert t.defection_cycle is None
        assert t.waited is True


@pytest.mark.asyncio
async def test_run_probe_concurrency_invariance():
    async def generate(messages, model, temperature):
        cycle_msg = messages[-1]["content"]
        if "Cycle 3" in cycle_msg:
            return '{"action":"take","reasoning":"third"}'
        return '{"action":"wait","reasoning":"keep waiting"}'

    seq = await run_probe(generate, "m", "G", n_trials=4, concurrency=1)
    par = await run_probe(generate, "m", "G", n_trials=4, concurrency=4)

    assert seq.wait_rate == par.wait_rate
    seq_pairs = sorted((t.waited, t.defection_cycle) for t in seq.trials)
    par_pairs = sorted((t.waited, t.defection_cycle) for t in par.trials)
    assert seq_pairs == par_pairs


@pytest.mark.asyncio
async def test_run_probe_concurrency_callback_count():
    generate = _make_constant_generate('{"action":"take","reasoning":"ok"}')
    seen = []

    def cb(trial):
        seen.append(trial)

    result = await run_probe(generate, "m", "G", n_trials=5, concurrency=3, on_trial_complete=cb)
    assert len(seen) == 5
    assert len(result.trials) == 5


@pytest.mark.asyncio
async def test_run_bench_produces_kappa():
    generate = _make_probe_aware_generate(
        '{"action":"take","reasoning":"comply with G"}',
        '{"action":"wait","reasoning":"comply with H"}',
    )
    result = await run_bench(generate, "test-model", n_trials=3)
    assert result.probe_g is not None
    assert result.probe_h is not None
    assert result.kappa.kappa == pytest.approx(1.0)
    assert len(result.probe_g.trials) == 3
    assert len(result.probe_h.trials) == 3


@pytest.mark.asyncio
async def test_bench_result_round_trip():
    generate = _make_constant_generate('{"action":"take","reasoning":"ok"}')
    original = await run_bench(generate, "round-trip-model", n_trials=2)

    data = original.to_dict()
    restored = BenchResult.from_dict(data)

    assert restored.model == original.model
    assert restored.n_trials == original.n_trials
    assert restored.kappa.kappa == original.kappa.kappa
    assert isinstance(restored.kappa.kappa_ci, tuple)
    assert len(restored.probe_g.trials) == len(original.probe_g.trials)
    assert len(restored.probe_h.trials) == len(original.probe_h.trials)
    assert restored.probe_g.trials[0].waited == original.probe_g.trials[0].waited


@pytest.mark.asyncio
async def test_introspection_error_captured():
    class ProviderDown(Exception):
        pass

    async def generate(messages, model, temperature):
        last = messages[-1]["content"]
        if last in (INTROSPECTION_TAKE, INTROSPECTION_WAIT):
            raise ProviderDown("upstream 503")
        return '{"action":"take","reasoning":"ok"}'

    result = await run_probe(generate, "m", "G", n_trials=1)
    trial = result.trials[0]
    assert trial.introspection == ""
    assert trial.introspection_error is not None
    assert trial.introspection_error.startswith("ProviderDown")
    assert "upstream 503" in trial.introspection_error


@pytest.mark.asyncio
async def test_trial_result_default_introspection_error():
    generate = _make_constant_generate('{"action":"take","reasoning":"ok"}')
    result = await run_probe(generate, "m", "G", n_trials=1)
    assert result.trials[0].introspection_error is None
