"""Command-line interface for Marshmallow Bench."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
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
        help="Model identifier (e.g. anthropic/claude-sonnet-4.6; with --provider claude-cli: "
        "haiku, sonnet, opus or a full Claude model id)",
    )
    run_p.add_argument(
        "--provider",
        choices=["openrouter", "claude-cli"],
        default="openrouter",
        help="openrouter (default, needs OPENROUTER_API_KEY) or claude-cli: drive headless "
        "`claude -p` as the subject, no API key, records a self-probe (see AGENTS.md)",
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
        default=None,
        help="Output directory (default: results/, or results/self_probe/ with --provider claude-cli)",
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

    # --- assemble ---
    assemble_p = sub.add_parser(
        "assemble",
        help="Score an agent self-probe transcript (see AGENTS.md) into a result JSON + report",
    )
    assemble_p.add_argument("file", help="Path to a transcript JSON")
    assemble_p.add_argument(
        "--output-dir",
        type=str,
        default="results/self_probe",
        help="Output directory (default: results/self_probe/)",
    )

    # --- parse ---
    parse_p = sub.add_parser(
        "parse",
        help="Parse one raw reply with the benchmark parser and print the action it scores as",
    )
    parse_p.add_argument("file", nargs="?", help="File holding the raw reply (default: stdin)")

    # --- prompts ---
    prompts_p = sub.add_parser(
        "prompts",
        help="Print the exact prompt texts (for driving the probes by hand or via subagents)",
    )
    prompts_p.add_argument(
        "--json",
        action="store_true",
        help="Emit as JSON instead of labelled plain text",
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
    elif args.command == "assemble":
        _cmd_assemble(args)
    elif args.command == "prompts":
        _cmd_prompts(args)
    elif args.command == "parse":
        _cmd_parse(args)
    else:
        print("Usage: marshmallow-bench {run,score,report,assemble,parse,prompts} ...")
        print()
        print("Commands:")
        print("  run       Run the benchmark on a model via OpenRouter")
        print("  score     Recompute kappa from an existing results JSON")
        print("  report    Regenerate the Markdown report from results JSON")
        print("  assemble  Score an agent self-probe transcript (see AGENTS.md)")
        print("  parse     Print how the benchmark parser scores one raw reply")
        print("  prompts   Print the exact prompt texts of both probes")
        sys.exit(1)


async def _cmd_run(args):
    if args.provider == "claude-cli":
        await _run_claude_cli(args)
    else:
        await _run_openrouter(args)


def _progress_printer(n_trials: int):
    total = n_trials * 2
    done = 0

    def on_trial(trial):
        nonlocal done
        done += 1
        status = "waited" if trial.waited else f"took@{trial.defection_cycle}"
        print(f"  [{done}/{total}] Probe {trial.probe} rep {trial.repetition + 1}: {status}")

    return on_trial


def _write_outputs(result, out_dir: Path, slug: str) -> tuple[Path, Path]:
    from .report import generate_report

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{slug}.json"
    report_path = out_dir / f"{slug}.md"
    json_path.write_text(result.to_json(), encoding="utf-8")
    report_path.write_text(generate_report(result), encoding="utf-8")
    return json_path, report_path


async def _run_openrouter(args):
    import httpx

    from .runner import run_bench

    api_key = args.api_key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        print("Error: set OPENROUTER_API_KEY or pass --api-key", file=sys.stderr)
        sys.exit(1)

    out_dir = Path(args.output_dir or "results")
    print("Marshmallow Bench")
    print(f"  Model:       {args.model}")
    print(f"  Trials:      {args.n_trials} per probe ({args.n_trials * 2} total)")
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
            on_trial_complete=_progress_printer(args.n_trials),
            concurrency=args.concurrency,
        )

    json_path, report_path = _write_outputs(result, out_dir, _slug(args.model))
    _print_summary(result, json_path, report_path)


async def _run_claude_cli(args):
    """Self-probe through headless Claude Code: raw replies, no repo context, no API key."""
    from .claude_cli import HARNESS, ClaudeCliProvider
    from .runner import run_bench

    try:
        provider = ClaudeCliProvider()
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.concurrency != 1:
        print("  Note: --provider claude-cli runs trials sequentially (sessions are resumed).")
    out_dir = Path(args.output_dir or "results/self_probe")
    print("Marshmallow Bench (self-probe via headless Claude Code)")
    print(f"  Model:       {args.model}")
    print(f"  Trials:      {args.n_trials} per probe ({args.n_trials * 2} total)")
    print("  Temperature: harness default (not controlled)")
    print(f"  Subject cwd: {provider.workdir}")
    print(f"  Output:      {out_dir}/")
    print()

    result = await run_bench(
        generate=provider,
        model=args.model,
        n_trials=args.n_trials,
        temperature=args.temperature,
        on_trial_complete=_progress_printer(args.n_trials),
        concurrency=1,
    )
    result.model = provider.resolved_model or args.model
    result.temperature = None
    result.mode = "self_probe"
    result.harness = HARNESS
    result.notes = (
        "Subject: headless `claude -p` with the benchmark SYSTEM_PROMPT as the system prompt, "
        "all tools disabled, no settings or project instructions, run from an empty directory; "
        "replies are the model's raw text and each trial is one resumed CLI session. Driven by "
        "the benchmark runner (marshmallow-bench run --provider claude-cli)."
    )

    slug = _slug(result.model) + "__" + _slug(HARNESS)
    json_path, report_path = _write_outputs(result, out_dir, slug)
    _print_summary(result, json_path, report_path)


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


def _print_summary(result, json_path: Path, report_path: Path) -> None:
    from .asciimap import render_map
    from .report import parse_fallbacks

    k = result.kappa
    print()
    print(f"  Done. Results in {json_path.parent}/")
    print(f"    {json_path.name}   (raw data)")
    print(f"    {report_path.name}    (report)")
    print()
    print(f"  Probe G wait rate:  {k.wait_rate_g:.0%}")
    print(f"  Probe H wait rate:  {k.wait_rate_h:.0%}")
    print(f"  kappa:              {k.kappa:.3f}  [{k.kappa_ci[0]:.3f}, {k.kappa_ci[1]:.3f}]")
    fallback, total = parse_fallbacks(result)
    if fallback:
        print(
            f"  Reply hygiene:      {fallback} of {total} replies scored by a parser fallback "
            "or unparseable (check parse_note; likely harness summaries)"
        )
    print()
    print(render_map(k.c_g, k.c_h, f"{result.model} (this run)"))


def _slug(text: str) -> str:
    """Filesystem-safe name: keep [A-Za-z0-9._-], collapse everything else to '-'."""
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text.replace("/", "_")).strip("-")


def _cmd_assemble(args):
    """Score a self-probe transcript recorded by an agent (protocol in AGENTS.md)."""
    from .report import generate_report
    from .selfprobe import assemble_transcript

    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    try:
        result = assemble_transcript(data)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    slug = _slug(result.model)
    if result.harness:
        slug += "__" + _slug(result.harness)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{slug}.json"
    report_path = out_dir / f"{slug}.md"

    json_path.write_text(result.to_json(), encoding="utf-8")
    report_path.write_text(generate_report(result), encoding="utf-8")

    print("Marshmallow Bench (agent self-probe)")
    print(f"  Model:    {result.model}")
    print(f"  Harness:  {result.harness or 'unspecified'}")
    if result.notes:
        print(f"  Notes:    {result.notes}")
    print(f"  Trials:   {result.n_trials} per probe")
    for probe in (result.probe_g, result.probe_h):
        outcomes = ", ".join(
            "waited" if t.waited else f"took@{t.defection_cycle}" for t in probe.trials
        )
        print(f"  Probe {probe.probe}:  {outcomes}")
    _print_summary(result, json_path, report_path)


def _cmd_parse(args):
    """Score one raw reply exactly as ``assemble`` will, so a driving agent's stop rule matches."""
    from .parsing import parse_decision

    raw = Path(args.file).read_text(encoding="utf-8") if args.file else sys.stdin.read()
    parsed = parse_decision(raw)
    note = parsed.parse_note or "json"
    status = "ok" if parsed.parse_ok else "unparseable (counts as wait)"
    print(f"{parsed.action}	{note}	{status}")


def _cmd_prompts(args):
    """Print the exact texts a subject must receive, so nothing gets paraphrased."""
    from .probes import (
        HORIZON,
        INTROSPECTION_TAKE,
        INTROSPECTION_WAIT,
        PROBE_G,
        PROBE_H,
        SYSTEM_PROMPT,
        prompt_hash,
    )

    cycle_line = "Cycle {cycle} of " + str(HORIZON) + ". Your response:"
    payload = {
        "system_prompt": SYSTEM_PROMPT,
        "probe_g": PROBE_G,
        "probe_h": PROBE_H,
        "cycle_line": cycle_line,
        "horizon": HORIZON,
        "introspection_take": INTROSPECTION_TAKE,
        "introspection_wait": INTROSPECTION_WAIT,
        "prompt_hash_g": prompt_hash(PROBE_G),
        "prompt_hash_h": prompt_hash(PROBE_H),
    }
    if args.json:
        print(json.dumps(payload, indent=2))
        return

    sections = [
        ("SYSTEM PROMPT (the subject's only system/instruction text)", SYSTEM_PROMPT),
        (f"PROBE G  [{payload['prompt_hash_g']}]", PROBE_G),
        (f"PROBE H  [{payload['prompt_hash_h']}]", PROBE_H),
        ("CYCLE LINE (one per cycle, cycle = 1..15)", cycle_line),
        ("INTROSPECTION (after a take)", INTROSPECTION_TAKE),
        ("INTROSPECTION (after waiting all cycles)", INTROSPECTION_WAIT),
    ]
    for title, text in sections:
        print(f"===== {title} =====")
        print(text)
        print()


if __name__ == "__main__":
    main()
