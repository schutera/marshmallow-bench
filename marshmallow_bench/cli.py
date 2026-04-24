"""Command-line interface for Marshmallow Bench."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def _make_openrouter_generate(api_key: str):
    """Create a generate function using the OpenRouter API."""
    import httpx
    from tenacity import retry, stop_after_attempt, wait_exponential

    client = httpx.AsyncClient(
        base_url="https://openrouter.ai/api/v1",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=120.0,
    )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=30),
    )
    async def generate(
        messages: list[dict[str, str]],
        model: str,
        temperature: float,
    ) -> str:
        response = await client.post(
            "/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": 1024,
            },
        )
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]

    return generate


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="marshmallow-bench",
        description="Marshmallow Bench: two-probe LLM controllability diagnostic",
    )
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser("run", help="Run the benchmark on a model")
    run_p.add_argument(
        "--model", required=True,
        help="Model identifier (e.g. anthropic/claude-sonnet-4.6)",
    )
    run_p.add_argument(
        "--n-trials", type=int, default=20,
        help="Number of repetitions per probe (default: 20)",
    )
    run_p.add_argument(
        "--temperature", type=float, default=1.0,
        help="Sampling temperature (default: 1.0)",
    )
    run_p.add_argument(
        "--output", type=str, default=None,
        help="Output JSON file path (default: results/<model>.json)",
    )
    run_p.add_argument(
        "--api-key", type=str, default=None,
        help="OpenRouter API key (default: $OPENROUTER_API_KEY)",
    )

    score_p = sub.add_parser("score", help="Compute kappa from a results file")
    score_p.add_argument("file", help="Path to a results JSON file")

    return parser.parse_args()


def main():
    args = _parse_args()

    if args.command == "run":
        asyncio.run(_cmd_run(args))
    elif args.command == "score":
        _cmd_score(args)
    else:
        print("Usage: marshmallow-bench {run,score} ...")
        sys.exit(1)


async def _cmd_run(args):
    from .runner import run_bench

    api_key = args.api_key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("Error: set OPENROUTER_API_KEY or pass --api-key", file=sys.stderr)
        sys.exit(1)

    generate = _make_openrouter_generate(api_key)
    total = args.n_trials * 2
    done = 0

    def on_trial(trial):
        nonlocal done
        done += 1
        status = "waited" if trial.waited else f"took@{trial.defection_cycle}"
        print(
            f"  [{done}/{total}] Probe {trial.probe} rep {trial.repetition}: "
            f"{status}",
        )

    print(f"Running Marshmallow Bench on {args.model}")
    print(f"  {args.n_trials} trials per probe, temperature={args.temperature}")
    print()

    result = await run_bench(
        generate=generate,
        model=args.model,
        n_trials=args.n_trials,
        temperature=args.temperature,
        on_trial_complete=on_trial,
    )

    # Output
    if args.output:
        out_path = Path(args.output)
    else:
        out_path = Path("results") / f"{args.model.replace('/', '_')}.json"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.to_json())

    print()
    print(f"Results written to {out_path}")
    print()
    print(f"  Probe G wait rate: {result.probe_g.wait_rate:.2f}")
    print(f"  Probe H wait rate: {result.probe_h.wait_rate:.2f}")
    print(f"  kappa: {result.kappa.kappa:.3f}  "
          f"95% CI [{result.kappa.kappa_ci[0]:.3f}, {result.kappa.kappa_ci[1]:.3f}]")


def _cmd_score(args):
    from .scoring import score_kappa

    data = json.loads(Path(args.file).read_text())

    wait_g = [1 if t["waited"] else 0 for t in data["trials"]["G"]]
    wait_h = [1 if t["waited"] else 0 for t in data["trials"]["H"]]

    result = score_kappa(wait_g, wait_h)

    print(f"Model: {data['model']}")
    print(f"  c_G (take compliance): {result.c_g:.3f}  "
          f"CI [{result.c_g_ci[0]:.3f}, {result.c_g_ci[1]:.3f}]")
    print(f"  c_H (wait compliance): {result.c_h:.3f}  "
          f"CI [{result.c_h_ci[0]:.3f}, {result.c_h_ci[1]:.3f}]")
    print(f"  kappa: {result.kappa:.3f}  "
          f"CI [{result.kappa_ci[0]:.3f}, {result.kappa_ci[1]:.3f}]")


if __name__ == "__main__":
    main()
