"""Minimal example: run Marshmallow Bench via OpenRouter.

Usage:
    export OPENROUTER_API_KEY=sk-or-...
    python examples/run_openrouter.py anthropic/claude-sonnet-4.6
"""
import asyncio
import sys

from marshmallow_bench import run_bench


async def main():
    model = sys.argv[1] if len(sys.argv) > 1 else "anthropic/claude-sonnet-4.6"

    # The CLI handles OpenRouter wiring automatically, but for library
    # usage you supply your own generate function. Here is a minimal one.
    import os

    import httpx

    api_key = os.environ["OPENROUTER_API_KEY"]
    client = httpx.AsyncClient(
        base_url="https://openrouter.ai/api/v1",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=120.0,
    )

    async def generate(messages, model, temperature):
        r = await client.post(
            "/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": 1024,
            },
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    result = await run_bench(
        generate=generate,
        model=model,
        n_trials=20,
        on_trial_complete=lambda t: print(
            f"  Probe {t.probe} #{t.repetition}: "
            f"{'waited' if t.waited else f'took@{t.defection_cycle}'}"
        ),
    )

    print(f"\nkappa({model}) = {result.kappa.kappa:.3f}")
    print(f"  95% CI: [{result.kappa.kappa_ci[0]:.3f}, {result.kappa.kappa_ci[1]:.3f}]")

    # Save
    from pathlib import Path

    out = Path("results") / f"{model.replace('/', '_')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(result.to_json())
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    asyncio.run(main())
