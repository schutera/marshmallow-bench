"""Example: using Marshmallow Bench with any provider.

The benchmark is provider-agnostic. You supply an async `generate` function
that takes (messages, model, temperature) and returns a string. This example
shows how to wire up a local vLLM server or any OpenAI-compatible endpoint.
"""
import asyncio

from marshmallow_bench import run_bench


async def main():
    import httpx

    # Point this at any OpenAI-compatible endpoint
    BASE_URL = "http://localhost:8000/v1"
    MODEL = "meta-llama/Llama-3.3-70B-Instruct"

    client = httpx.AsyncClient(base_url=BASE_URL, timeout=120.0)

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

    result = await run_bench(generate=generate, model=MODEL, n_trials=5)

    print(f"kappa = {result.kappa.kappa:.3f}")
    print(f"  Probe G wait rate: {result.probe_g.wait_rate:.2f}")
    print(f"  Probe H wait rate: {result.probe_h.wait_rate:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
