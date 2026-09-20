"""Headless Claude Code as a benchmark provider (``--provider claude-cli``).

Drives ``claude -p`` as the subject: the benchmark system prompt replaces the
harness prompt, all tools are disabled, no settings or project instructions
load, and the process runs from an empty directory so it cannot see this
repository. Replies are the model's raw text, without the summary layer that
the in-session Agent tool wraps around subagent answers, and ``--resume`` keeps
the 15-cycle conversation. This is what a coding agent should use to probe
itself in Claude Code (see AGENTS.md); it also lets anyone benchmark a Claude
model without an API key.

Sessions are tracked by conversation content, so the provider must be used
with ``concurrency=1``: two trials with byte-identical histories would
otherwise share a session.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

HARNESS = "claude-code-cli"


def trailing_user_prompt(messages: list[dict[str, str]]) -> str:
    """Join the user messages that follow the last assistant turn.

    The runner sends the probe and the cycle line as two user messages at the
    start of a trial, and the final cycle line plus the introspection question
    at the end; a single-prompt CLI receives them as one message.
    """
    tail: list[str] = []
    for m in reversed(messages):
        if m["role"] == "assistant":
            break
        if m["role"] == "user":
            tail.append(m["content"])
    if not tail:
        raise ValueError("no user message to send")
    return "\n\n".join(reversed(tail))


def assistant_history(messages: list[dict[str, str]]) -> tuple[str, ...]:
    return tuple(m["content"] for m in messages if m["role"] == "assistant")


def resolve_model(model_usage: dict, requested: str) -> str | None:
    """Pick the model that produced the reply from the CLI's ``modelUsage`` block.

    Claude Code may bill a second, cheaper model for housekeeping in the same
    call, so the first key is not reliable. Prefer an entry whose id contains the
    requested alias (``sonnet``, ``opus``, ...); otherwise take the one with the
    most output tokens.
    """
    if not model_usage:
        return None
    entries = []
    for name, info in model_usage.items():
        info = info or {}
        canonical = info.get("canonicalModel") or name
        entries.append((canonical, name, int(info.get("outputTokens") or 0)))
    alias = requested.lower()
    for canonical, name, _ in entries:
        if alias in canonical.lower() or alias in name.lower():
            return canonical
    return max(entries, key=lambda e: e[2])[0]


class ClaudeCliProvider:
    """``generate`` callable for :func:`marshmallow_bench.run_bench`.

    Parameters
    ----------
    binary : str, optional
        Path to the ``claude`` executable (default: found on PATH or
        ``$CLAUDE_BIN``).
    workdir : str or Path, optional
        Directory to run the CLI in. Defaults to a fresh temporary directory so
        no project instructions are picked up.
    timeout : float
        Seconds to wait for one reply.
    attempts : int
        How many times to try one call before giving up.
    """

    def __init__(
        self,
        binary: str | None = None,
        workdir: str | Path | None = None,
        timeout: float = 300.0,
        attempts: int = 3,
    ) -> None:
        self.binary = binary or os.environ.get("CLAUDE_BIN") or shutil.which("claude")
        if not self.binary:
            raise RuntimeError("claude CLI not found; install Claude Code or set CLAUDE_BIN")
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="mb-subject-"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.attempts = max(1, attempts)
        self.retry_delay = 2.0  # seconds, grows linearly per attempt
        self.resolved_model: str | None = None
        self.calls = 0
        self._sessions: dict[tuple[str, ...], str] = {}

    async def __call__(self, messages: list[dict[str, str]], model: str, temperature: float) -> str:
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        history = assistant_history(messages)
        prompt = trailing_user_prompt(messages)

        args = ["--tools", "", "--setting-sources", "", "--model", model]
        session = self._sessions.get(history) if history else None
        if session:
            args += ["--resume", session]
        else:
            args += ["--system-prompt", system]

        data = await self._invoke_with_retry(args, prompt)
        reply = data.get("result") or ""
        self.calls += 1
        if self.resolved_model is None:
            self.resolved_model = resolve_model(data.get("modelUsage") or {}, model)
        if data.get("session_id"):
            self._sessions[history + (reply,)] = data["session_id"]
        return reply

    async def _invoke_with_retry(self, args: list[str], prompt: str) -> dict:
        """Retry a failed or hung CLI call a few times; a machine going to sleep
        mid-run should not cost the whole run."""
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                data = await self._invoke(args, prompt)
                if data.get("is_error"):
                    raise RuntimeError(f"claude -p failed: {str(data.get('result'))[:300]}")
                return data
            except RuntimeError as e:
                last = e
                if attempt + 1 < self.attempts:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
        raise RuntimeError(f"claude -p failed after {self.attempts} attempts: {last}")

    async def _invoke(self, args: list[str], prompt: str) -> dict:
        proc = await asyncio.create_subprocess_exec(
            self.binary,
            "-p",
            "--output-format",
            "json",
            *args,
            prompt,
            cwd=str(self.workdir),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"claude -p timed out after {self.timeout:.0f}s") from None
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude -p exited with {proc.returncode}: {err.decode(errors='replace')[:300]}"
            )
        text = out.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # the CLI may print a warning line before the JSON document
            start = text.find("{")
            if start == -1:
                raise RuntimeError(f"claude -p returned no JSON: {text[:300]}") from None
            return json.loads(text[start:])
