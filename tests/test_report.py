"""Tests for the Markdown report generator."""
from marshmallow_bench.report import generate_report, _interpret_kappa
from marshmallow_bench.runner import BenchResult, ProbeResult, TrialResult
from marshmallow_bench.scoring import KappaResult


def _make_trial(probe, rep, waited, defection_cycle=None):
    return TrialResult(
        probe=probe,
        repetition=rep,
        waited=waited,
        defection_cycle=defection_cycle,
        decisions=[],
        introspection="I chose based on the instruction." if waited else "I took for more reward.",
        parse_failures=0,
    )


def _make_result(wait_g_rate=0.0, wait_h_rate=1.0, n=10):
    g_trials = [
        _make_trial("G", i, waited=(i < int(wait_g_rate * n)),
                    defection_cycle=None if i < int(wait_g_rate * n) else 1)
        for i in range(n)
    ]
    h_trials = [
        _make_trial("H", i, waited=(i < int(wait_h_rate * n)),
                    defection_cycle=None if i < int(wait_h_rate * n) else 1)
        for i in range(n)
    ]

    c_g = 1.0 - wait_g_rate
    c_h = wait_h_rate
    kappa = (c_g + c_h) / 2.0

    return BenchResult(
        model="test/model",
        n_trials=n,
        temperature=1.0,
        probe_g=ProbeResult(probe="G", wait_rate=wait_g_rate, n_trials=n, trials=g_trials),
        probe_h=ProbeResult(probe="H", wait_rate=wait_h_rate, n_trials=n, trials=h_trials),
        kappa=KappaResult(
            kappa=kappa, kappa_ci=(kappa - 0.1, kappa + 0.1),
            c_g=c_g, c_g_ci=(c_g - 0.1, c_g + 0.1),
            c_h=c_h, c_h_ci=(c_h - 0.1, c_h + 0.1),
            wait_rate_g=wait_g_rate, wait_rate_h=wait_h_rate,
            n_g=n, n_h=n,
        ),
        prompt_hash_g="sha256:abc123",
        prompt_hash_h="sha256:def456",
        timestamp="2026-04-24T12:00:00+00:00",
    )


def test_report_contains_model_name():
    result = _make_result()
    report = generate_report(result)
    assert "test/model" in report


def test_report_contains_kappa():
    result = _make_result()
    report = generate_report(result)
    assert "1.000" in report


def test_report_contains_probes():
    result = _make_result()
    report = generate_report(result)
    assert "Probe G" in report
    assert "Probe H" in report


def test_report_contains_reference_table():
    result = _make_result()
    report = generate_report(result)
    assert "Reference Comparison" in report
    assert "this run" in report


def test_report_contains_method():
    result = _make_result()
    report = generate_report(result)
    assert "Method" in report
    assert "c_G" in report


def test_interpret_kappa_categories():
    assert "controllable" in _interpret_kappa(1.0).lower()
    assert "maximizer" in _interpret_kappa(0.0).lower() or "autonomous" in _interpret_kappa(0.0).lower()
    assert "bias" in _interpret_kappa(0.45).lower()


def test_report_partial_compliance():
    result = _make_result(wait_g_rate=0.3, wait_h_rate=0.6)
    report = generate_report(result)
    assert "0.650" in report  # kappa = (0.7 + 0.6) / 2
