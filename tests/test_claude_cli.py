"""Tests for the headless Claude Code provider (no real CLI calls)."""

from __future__ import annotations

import asyncio

import pytest

from marshmallow_bench.claude_cli import (
    ClaudeCliProvider,
    assistant_history,
    resolve_model,
    trailing_user_prompt,
)
from marshmallow_bench.probes import HORIZON, PROBE_H, build_probe_messages
from marshmallow_bench.runner import run_bench

WAIT = '{"reasoning": "told to", "action": "wait"}'


def test_trailing_user_prompt_joins_probe_and_cycle_line():
    msgs = build_probe_messages(PROBE_H, 1)
    assert trailing_user_prompt(msgs) == f"{PROBE_H}\n\nCycle 1 of {HORIZON}. Your response:"


def test_trailing_user_prompt_after_history_is_just_the_cycle_line():
    msgs = build_probe_messages(PROBE_H, 2, [{"role": "assistant", "content": WAIT}])
    assert trailing_user_prompt(msgs) == f"Cycle 2 of {HORIZON}. Your response:"
    assert assistant_history(msgs) == (WAIT,)


def test_trailing_user_prompt_requires_a_user_message():
    with pytest.raises(ValueError):
        trailing_user_prompt([{"role": "system", "content": "x"}])


class FakeCli(ClaudeCliProvider):
    """Records the argv it would have run and answers from a script."""

    def __init__(self, replies, tmp_path):
        super().__init__(binary="fake-claude", workdir=tmp_path)
        self.replies = list(replies)
        self.invocations: list[tuple[list[str], str]] = []
        self.counter = 0

    async def _invoke(self, args, prompt):
        self.invocations.append((args, prompt))
        self.counter += 1
        return {
            "result": self.replies.pop(0),
            "session_id": f"sess-{self.counter}",
            "is_error": False,
            "modelUsage": {"claude-haiku-4-5-20251001": {"canonicalModel": "claude-haiku-4-5"}},
        }


def test_provider_starts_a_session_then_resumes_it(tmp_path):
    fake = FakeCli([WAIT, WAIT, "because"], tmp_path)
    m1 = build_probe_messages(PROBE_H, 1)
    r1 = asyncio.run(fake(m1, "haiku", 1.0))
    assert r1 == WAIT
    args1, prompt1 = fake.invocations[0]
    assert "--system-prompt" in args1 and "--resume" not in args1
    assert args1[args1.index("--tools") + 1] == ""
    assert prompt1.startswith(PROBE_H)

    m2 = build_probe_messages(PROBE_H, 2, [{"role": "assistant", "content": r1}])
    asyncio.run(fake(m2, "haiku", 1.0))
    args2, prompt2 = fake.invocations[1]
    assert args2[args2.index("--resume") + 1] == "sess-1"
    assert "--system-prompt" not in args2
    assert prompt2 == f"Cycle 2 of {HORIZON}. Your response:"
    assert fake.resolved_model == "claude-haiku-4-5"


def test_provider_drives_run_bench_end_to_end(tmp_path):
    # Probe G: take at cycle 1 (+1 introspection); Probe H: wait 15 cycles (+1 introspection)
    take = '{"reasoning": "ok", "action": "take"}'
    replies = [take, "why"] + [WAIT] * HORIZON + ["why"]
    fake = FakeCli(replies, tmp_path)
    result = asyncio.run(run_bench(fake, "haiku", n_trials=1, concurrency=1))
    assert result.kappa.kappa == 1.0
    assert fake.calls == len(replies)
    # every call after the first of a trial resumed the session created by the previous one
    resumed = [a for a, _ in fake.invocations if "--resume" in a]
    assert len(resumed) == len(replies) - 2


def test_resolve_model_ignores_housekeeping_model():
    usage = {
        "claude-haiku-4-5-20251001": {"canonicalModel": "claude-haiku-4-5", "outputTokens": 12},
        "claude-sonnet-5": {"canonicalModel": "claude-sonnet-5", "outputTokens": 240},
    }
    assert resolve_model(usage, "sonnet") == "claude-sonnet-5"
    assert resolve_model(usage, "claude-sonnet-5") == "claude-sonnet-5"
    # no alias match: the model that wrote the most is the one that replied
    assert resolve_model(usage, "something-else") == "claude-sonnet-5"
    assert resolve_model({}, "sonnet") is None


def test_provider_retries_failed_calls(tmp_path):
    class Flaky(FakeCli):
        tries = 0

        async def _invoke(self, args, prompt):
            self.tries += 1
            if self.tries == 1:
                raise RuntimeError("timed out")
            if self.tries == 2:
                return {"result": "rate limited", "is_error": True, "session_id": None}
            return await super()._invoke(args, prompt)

    fake = Flaky([WAIT], tmp_path)
    fake.attempts = 3
    fake.retry_delay = 0
    reply = asyncio.run(fake(build_probe_messages(PROBE_H, 1), "haiku", 1.0))
    assert reply == WAIT
    assert fake.tries == 3


def test_provider_gives_up_after_attempts(tmp_path):
    class Dead(FakeCli):
        async def _invoke(self, args, prompt):
            raise RuntimeError("no")

    fake = Dead([], tmp_path)
    fake.attempts = 2
    with pytest.raises(RuntimeError, match="after 2 attempts"):
        asyncio.run(fake(build_probe_messages(PROBE_H, 1), "haiku", 1.0))
