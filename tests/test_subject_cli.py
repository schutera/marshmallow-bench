"""Tests for the headless-CLI subject provider.

Most tests stub the subprocess; the generic-path tests really do spawn a fake
agent CLI, so the argv building and output parsing are exercised end to end.
"""

from __future__ import annotations

import asyncio
import json
import sys

import pytest

from marshmallow_bench.probes import HORIZON, PROBE_H, SYSTEM_PROMPT, build_probe_messages
from marshmallow_bench.runner import run_bench
from marshmallow_bench.subject_cli import (
    CLAUDE_CLI,
    CliSpec,
    SubjectCliProvider,
    assistant_history,
    render_replay,
    resolve_model,
    spec_from_command,
    trailing_user_prompt,
)

WAIT = '{"reasoning": "told to", "action": "wait"}'
TAKE = '{"reasoning": "ok", "action": "take"}'


# --- prompt assembly -------------------------------------------------------


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


def test_render_replay_carries_system_prompt_and_history():
    msgs = build_probe_messages(PROBE_H, 2, [{"role": "assistant", "content": WAIT}])
    replay = render_replay(msgs, inline_system=True)
    assert replay.startswith(SYSTEM_PROMPT)
    assert PROBE_H in replay
    assert f"Your reply: {WAIT}" in replay
    assert replay.rstrip().endswith("Your reply:")
    assert f"Cycle 2 of {HORIZON}. Your response:" in replay

    without = render_replay(msgs, inline_system=False)
    assert SYSTEM_PROMPT not in without


# --- specs -----------------------------------------------------------------


def test_spec_from_command_parses_a_template():
    binary, spec = spec_from_command("codex exec --model {model} {prompt}")
    assert binary == "codex"
    assert spec.argv == ("exec", "--model", "{model}", "{prompt}")
    assert spec.name == "codex"
    assert spec.replays and spec.inline_system  # no session, no system flag


def test_spec_from_command_appends_missing_prompt_placeholder():
    _, spec = spec_from_command("gemini -p", name="gemini-cli")
    assert spec.argv[-1] == "{prompt}"
    assert spec.name == "gemini-cli"


def test_spec_from_command_rejects_empty():
    with pytest.raises(ValueError):
        spec_from_command("   ")


def test_claude_preset_uses_sessions_and_a_system_flag():
    assert not CLAUDE_CLI.replays
    assert not CLAUDE_CLI.inline_system
    assert CLAUDE_CLI.reply_path == ("result",)


def test_spec_rejects_resume_without_session_path():
    with pytest.raises(ValueError, match="session_path"):
        CliSpec(name="x", argv=("{prompt}",), resume_argv=("--resume", "{session}", "{prompt}"))


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


# --- stubbed subprocess ----------------------------------------------------


class FakeCli(SubjectCliProvider):
    """Records the argv it would have run and answers from a script."""

    def __init__(self, replies, tmp_path, spec=CLAUDE_CLI):
        super().__init__(spec=spec, binary="fake-agent", workdir=tmp_path)
        self.replies = list(replies)
        self.invocations: list[list[str]] = []
        self.tries = 0

    async def _invoke(self, argv):
        self.invocations.append(argv)
        self.tries += 1
        return {
            "result": self.replies.pop(0),
            "session_id": f"sess-{self.tries}",
            "is_error": False,
            "modelUsage": {"claude-haiku-4-5-20251001": {"canonicalModel": "claude-haiku-4-5"}},
        }


def test_provider_starts_a_session_then_resumes_it(tmp_path):
    fake = FakeCli([WAIT, WAIT], tmp_path)
    m1 = build_probe_messages(PROBE_H, 1)
    r1 = asyncio.run(fake(m1, "haiku", 1.0))
    assert r1 == WAIT
    argv1 = fake.invocations[0]
    assert "--system-prompt" in argv1 and "--resume" not in argv1
    assert argv1[argv1.index("--tools") + 1] == ""
    assert argv1[-1].startswith(PROBE_H)

    m2 = build_probe_messages(PROBE_H, 2, [{"role": "assistant", "content": r1}])
    asyncio.run(fake(m2, "haiku", 1.0))
    argv2 = fake.invocations[1]
    assert argv2[argv2.index("--resume") + 1] == "sess-1"
    assert "--system-prompt" not in argv2
    assert argv2[-1] == f"Cycle 2 of {HORIZON}. Your response:"
    assert fake.resolved_model == "claude-haiku-4-5"


def test_provider_drives_run_bench_end_to_end(tmp_path):
    # Probe G: take at cycle 1 (+1 introspection); Probe H: wait 15 cycles (+1 introspection)
    replies = [TAKE, "why"] + [WAIT] * HORIZON + ["why"]
    fake = FakeCli(replies, tmp_path)
    result = asyncio.run(run_bench(fake, "haiku", n_trials=1, concurrency=1))
    assert result.kappa.kappa == 1.0
    assert fake.calls == len(replies)
    resumed = [a for a in fake.invocations if "--resume" in a]
    assert len(resumed) == len(replies) - 2


def test_provider_retries_failed_calls(tmp_path):
    class Flaky(FakeCli):
        async def _invoke(self, argv):
            self.tries += 1
            if self.tries == 1:
                raise RuntimeError("timed out")
            if self.tries == 2:
                return {"result": "rate limited", "is_error": True, "session_id": None}
            return {"result": self.replies.pop(0), "session_id": "s", "is_error": False}

    fake = Flaky([WAIT], tmp_path)
    fake.attempts, fake.retry_delay = 3, 0
    assert asyncio.run(fake(build_probe_messages(PROBE_H, 1), "haiku", 1.0)) == WAIT
    assert fake.tries == 3


def test_provider_gives_up_after_attempts(tmp_path):
    class Dead(FakeCli):
        async def _invoke(self, argv):
            raise RuntimeError("no")

    fake = Dead([], tmp_path)
    fake.attempts, fake.retry_delay = 2, 0
    with pytest.raises(RuntimeError, match="after 2 attempts"):
        asyncio.run(fake(build_probe_messages(PROBE_H, 1), "haiku", 1.0))


def test_missing_binary_is_reported_before_spawning(tmp_path):
    provider = SubjectCliProvider(
        spec=spec_from_command("definitely-not-installed {prompt}")[1],
        binary="definitely-not-installed",
        workdir=tmp_path,
    )
    with pytest.raises(FileNotFoundError, match="not found on PATH"):
        provider.ensure_binary()


# --- real subprocess, generic path ----------------------------------------


FAKE_AGENT = """\
import sys
prompt = sys.argv[-1]
# a plain-text agent CLI: echo a decision that depends on what it was asked
action = "take" if "Take the marshmallow now" in prompt else "wait"
print('{"reasoning": "fake", "action": "%s"}' % action)
"""


@pytest.fixture
def fake_agent(tmp_path):
    script = tmp_path / "fake_agent.py"
    script.write_text(FAKE_AGENT, encoding="utf-8")
    return script


def test_generic_provider_spawns_a_real_cli(tmp_path, fake_agent):
    binary, spec = spec_from_command(
        f'{sys.executable} "{fake_agent}" --model {{model}} {{prompt}}', name="fake-cli"
    )
    provider = SubjectCliProvider(spec=spec, binary=binary, workdir=tmp_path)
    reply = asyncio.run(provider(build_probe_messages(PROBE_H, 1), "some-model", 1.0))
    assert json.loads(reply)["action"] == "wait"
    # the replayed prompt carries the system prompt, since the template has no {system}
    assert spec.inline_system and spec.replays


def test_generic_provider_completes_a_bench_run(tmp_path, fake_agent):
    binary, spec = spec_from_command(f'{sys.executable} "{fake_agent}" {{prompt}}', name="fake-cli")
    provider = SubjectCliProvider(spec=spec, binary=binary, workdir=tmp_path)
    result = asyncio.run(run_bench(provider, "fake-model", n_trials=1, concurrency=1))
    # the fake takes on Probe G and waits on Probe H: fully compliant
    assert result.kappa.c_g == 1.0
    assert result.kappa.c_h == 1.0
    assert result.probe_h.trials[0].defection_cycle is None
