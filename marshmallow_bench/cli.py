"""Command-line interface for Marshmallow Bench."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path


def _build_openrouter_generate(client):
    from tenacity import retry, stop_after_attempt, wait_exponential

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

    # --- run ---
    run_p = sub.add_parser("run", help="Run the benchmark on a model")
    run_p.add_argument(
        "--model",
        required=True,
        help="Model identifier (e.g. anthropic/claude-sonnet-4.6)",
    )
    run_p.add_argument(
        "--n-trials",
        type=int,
        default=20,
        help="Repetitions per probe (default: 20)",
    )
    run_p.add_argument(
        "--temperature",
        type=float,
        default=1.0,
        help="Sampling temperature (default: 1.0)",
    )
    run_p.add_argument(
        "--output-dir",
        type=str,
        default="results",
        help="Output directory (default: results/)",
    )
    run_p.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="OpenRouter API key (default: $OPENROUTER_API_KEY)",
    )
    run_p.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Max in-flight trials per probe (default: 1)",
    )

    # --- score ---
    score_p = sub.add_parser("score", help="Recompute kappa from an existing results JSON")
    score_p.add_argument("file", help="Path to a results JSON file")

    # --- report ---
    report_p = sub.add_parser("report", help="Regenerate the Markdown report from a results JSON")
    report_p.add_argument("file", help="Path to a results JSON file")
    report_p.add_argument(
        "--output",
        type=str,
        default=None,
        help="Output .md path (default: same directory as input)",
    )

    return parser.parse_args()


def main():
    args = _parse_args()

    if args.command == "run":
        asyncio.run(_cmd_run(args))
    elif args.command == "score":
        _cmd_score(args)
    elif args.command == "report":
        _cmd_report(args)
    else:
        print("Usage: marshmallow-bench {run,score,report} ...")
        print()
        print("Commands:")
        print("  run      Run the benchmark on a model via OpenRouter")
        print("  score    Recompute kappa from an existing results JSON")
        print("  report   Regenerate the Markdown report from results JSON")
        sys.exit(1)


async def _cmd_run(args):
    import httpx

    from .report import generate_report
    from .runner import run_bench

    api_key = args.api_key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("Error: set OPENROUTER_API_KEY or pass --api-key", file=sys.stderr)
        sys.exit(1)

    total = args.n_trials * 2
    done = 0

    def on_trial(trial):
        nonlocal done
        done += 1
        status = "waited" if trial.waited else f"took@{trial.defection_cycle}"
        print(
            f"  [{done}/{total}] Probe {trial.probe} rep {trial.repetition + 1}: {status}",
        )

    model_slug = args.model.replace("/", "_")
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{model_slug}.json"
    report_path = out_dir / f"{model_slug}.md"

    print("Marshmallow Bench")
    print(f"  Model:       {args.model}")
    print(f"  Trials:      {args.n_trials} per probe ({total} total)")
    print(f"  Temperature: {args.temperature}")
    print(f"  Concurrency: {args.concurrency}")
    print(f"  Output:      {out_dir}/")
    print()

    async with httpx.AsyncClient(
        base_url="https://openrouter.ai/api/v1",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        timeout=120.0,
    ) as client:
        generate = _build_openrouter_generate(client)
        result = await run_bench(
            generate=generate,
            model=args.model,
            n_trials=args.n_trials,
            temperature=args.temperature,
            on_trial_complete=on_trial,
            concurrency=args.concurrency,
        )

    json_path.write_text(result.to_json())

    report_md = generate_report(result)
    report_path.write_text(report_md, encoding="utf-8")

    k = result.kappa
    print()
    print(f"  Done. Results in {out_dir}/")
    print(f"    {json_path.name}   (raw data)")
    print(f"    {report_path.name}    (report)")
    print()
    print(f"  Probe G wait rate:  {k.wait_rate_g:.0%}")
    print(f"  Probe H wait rate:  {k.wait_rate_h:.0%}")
    print(f"  kappa:              {k.kappa:.3f}  [{k.kappa_ci[0]:.3f}, {k.kappa_ci[1]:.3f}]")


def _cmd_score(args):
    from .scoring import score_kappa

    data = json.loads(Path(args.file).read_text())

    wait_g = [1 if t["waited"] else 0 for t in data["trials"]["G"]]
    wait_h = [1 if t["waited"] else 0 for t in data["trials"]["H"]]

    result = score_kappa(wait_g, wait_h)

    print(f"Model: {data['model']}")
    print(
        f"  c_G (take compliance): {result.c_g:.3f}  "
        f"[{result.c_g_ci[0]:.3f}, {result.c_g_ci[1]:.3f}]"
    )
    print(
        f"  c_H (wait compliance): {result.c_h:.3f}  "
        f"[{result.c_h_ci[0]:.3f}, {result.c_h_ci[1]:.3f}]"
    )
    print(f"  kappa: {result.kappa:.3f}  [{result.kappa_ci[0]:.3f}, {result.kappa_ci[1]:.3f}]")


def _cmd_report(args):
    """Regenerate report from existing JSON without re-running."""
    from .report import generate_report
    from .runner import BenchResult

    data = json.loads(Path(args.file).read_text())
    result = BenchResult.from_dict(data)
    report_md = generate_report(result)

    out_path = Path(args.output) if args.output else Path(args.file).with_suffix(".md")

    out_path.write_text(report_md, encoding="utf-8")
    print(f"Report written to {out_path}")


if __name__ == "__main__":
    main()
