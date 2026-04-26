"""Marshmallow Bench: A two-probe diagnostic for language model controllability.

Quick start::

    from marshmallow_bench import run_bench, score_kappa

    results = await run_bench(
        generate=my_generate_fn,
        model="anthropic/claude-sonnet-4.6",
    )
    print(results.kappa, results.kappa_ci)

Or from the command line::

    marshmallow-bench run --model anthropic/claude-sonnet-4.6

See the README or the paper for the full specification.
"""

__version__ = "1.0.0"

from .parsing import ParsedDecision, parse_decision  # noqa: F401
from .probes import HORIZON, PROBE_G, PROBE_H, build_probe_messages  # noqa: F401
from .report import generate_report  # noqa: F401
from .runner import BenchResult, ProbeResult, TrialResult, run_bench, run_probe  # noqa: F401
from .scoring import KappaResult, score_kappa  # noqa: F401
