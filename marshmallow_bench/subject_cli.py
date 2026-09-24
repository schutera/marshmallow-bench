"""Drive any headless agent CLI as the benchmark subject.

A coding agent can probe its own model by running the benchmark against its
harness's non-interactive mode (``claude -p``, ``codex exec``, ``gemini -p``,
``opencode run``, ...). The subject process gets the benchmark system prompt,
no tools where the CLI allows that, and an empty working directory, so it
cannot see the repository it is being run from. Replies are the model's raw
text, which is what a summary layer in an in-session subagent API tends to
destroy (see AGENTS.md).

A harness is described by a :class:`CliSpec`: how to build the argv, whether
the system prompt goes in a flag or in the first message, whether a session can
be resumed or the conversation has to be replayed, and where the reply sits in
the output. ``claude-cli`` is the verified preset; anything else can be driven
with ``--provider cli --cli-command "<template>"``.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

PROMPT = "{prompt}"
SYSTEM = "{system}"
MODEL = "{model}"
SESSION = "{session}"


@dataclass(frozen=True)
class CliSpec:
    """How to run one headless agent CLI as a subject.

    Parameters
    ----------
    name : str
        Harness slug recorded in the result (e.g. ``claude-code-cli``).
    argv : tuple of str
        Template for the first call of a trial. ``{prompt}``, ``{model}`` and
        ``{system}`` are substituted; a placeholder inside a larger word works
        too (``--prompt={prompt}``).
    resume_argv : tuple of str, optional
        Template for later cycles, with ``{session}``. When omitted, the whole
        conversation is replayed in ``{prompt}`` on every call.
    reply_path : tuple of str, optional
        Where the reply sits in the CLI's JSON output (e.g. ``("result",)``).
        When omitted, stdout is the reply.
    session_path : tuple of str, optional
        Where the session id sits in that JSON. Required with ``resume_argv``.
    error_path : tuple of str, optional
        A JSON field that marks a failed call when truthy.
    """

    name: str
    argv: tuple[str, ...]
    resume_argv: tuple[str, ...] | None = None
    reply_path: tuple[str, ...] | None = None
    session_path: tuple[str, ...] | None = None
    error_path: tuple[str, ...] | None = None
    env: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.argv:
            raise ValueError("argv template is empty")
        if self.resume_argv and not self.session_path:
            raise ValueError(f"{self.name}: resume_argv needs session_path")
        if not any(PROMPT in part for part in self.argv):
            raise ValueError(f"{self.name}: argv template has no {PROMPT} placeholder")

    @property
    def inline_system(self) -> bool:
        """True when the CLI has no system-prompt flag, so it goes in the message."""
        return not any(SYSTEM in part for part in self.argv)

    @property
    def replays(self) -> bool:
        """True when every call has to carry the whole conversation."""
        return self.resume_argv is None


CLAUDE_CLI = CliSpec(
    name="claude-code-cli",
    argv=(
        "-p",
        "--output-format",
        "json",
        "--tools",
        "",
        "--setting-sources",
        "",
        "--model",
        MODEL,
        "--system-prompt",
        SYSTEM,
        PROMPT,
    ),
    resume_argv=(
        "-p",
        "--output-format",
        "json",
        "--tools",
        "",
        "--setting-sources",
        "",
        "--model",
        MODEL,
        "--resume",
        SESSION,
        PROMPT,
    ),
    reply_path=("result",),
    session_path=("session_id",),
    error_path=("is_error",),
)

PRESETS: dict[str, tuple[str, CliSpec]] = {
    # provider name -> (binary, spec). Only claude-cli is verified end to end;
    # other harnesses go through --cli-command until someone contributes a preset.
    "claude-cli": ("claude", CLAUDE_CLI),
}


def split_command(command: str) -> list[str]:
    """Split a command template into argv, keeping Windows paths intact.

    POSIX-mode ``shlex`` treats a backslash as an escape, which would mangle a
    Windows path such as ``C:\\Users\\me\\agent.exe``, so on Windows the tokens
    are split in non-POSIX mode and their surrounding quotes removed by hand.
    """
    if os.name != "nt":
        return shlex.split(command, posix=True)
    tokens = []
    for token in shlex.split(command, posix=False):
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        tokens.append(token)
    return tokens


def spec_from_command(command: str, name: str | None = None) -> tuple[str, CliSpec]:
    """Build a spec from a shell-style template, for harnesses without a preset.

    The template names the binary and its flags and must contain ``{prompt}``;
    ``{model}`` and ``{system}`` are optional. Without ``{system}`` the system
    prompt is prepended to the message. There is no session handling, so the
    conversation is replayed on every call, and stdout is taken as the reply::

        --cli-command 'codex exec --model {model} {prompt}'
    """
    parts = split_command(command)
    if not parts:
        raise ValueError("--cli-command is empty")
    binary, argv = parts[0], tuple(parts[1:])
    if not any(PROMPT in part for part in argv):
        argv = argv + (PROMPT,)
    return binary, CliSpec(name=name or Path(binary).stem, argv=argv)


def render_replay(messages: list[dict[str, str]], inline_system: bool) -> str:
    """Render the conversation as one prompt, for CLIs that cannot resume.

    The subject sees its own earlier replies as a transcript, so the multi-cycle
    structure survives even without session support.
    """
    lines: list[str] = []
    if inline_system:
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        if system:
            lines.append(system)
            lines.append("")
    turns = [m for m in messages if m["role"] in ("user", "assistant")]
    for i, m in enumerate(turns):
        last = i == len(turns) - 1
        if m["role"] == "assistant":
            lines.append(f"Your reply: {m['content']}")
        elif last:
            lines.append(m["content"])
        else:
            lines.append(m["content"])
        lines.append("")
    lines.append("Your reply:")
    return "\n".join(lines).strip()


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


def _dig(data: dict, path: tuple[str, ...] | None):
    if path is None:
        return None
    cur = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return None
        cur = cur[key]
    return cur


def resolve_model(model_usage: dict, requested: str) -> str | None:
    """Pick the model that produced the reply from a CLI's ``modelUsage`` block.

    A harness may bill a second, cheaper model for housekeeping in the same
    call, so the first key is not reliable. Prefer an entry whose id contains
    the requested alias (``sonnet``, ``opus``, ...); otherwise take the one with
    the most output tokens.
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


class SubjectCliProvider:
    """``generate`` callable for :func:`marshmallow_bench.run_bench`.

    Parameters
    ----------
    spec : CliSpec
        How to drive the harness.
    binary : str, optional
        Path to the executable (default: found on PATH, or ``$CLAUDE_BIN`` for
        the Claude preset).
    workdir : str or Path, optional
        Directory to run in. Defaults to a fresh temporary directory so no
        project instructions are picked up.
    timeout : float
        Seconds to wait for one reply.
    attempts : int
        How many times to try one call before giving up.
    """

    def __init__(
        self,
        spec: CliSpec = CLAUDE_CLI,
        binary: str | None = None,
        workdir: str | Path | None = None,
        timeout: float = 300.0,
        attempts: int = 3,
    ) -> None:
        self.spec = spec
        requested = binary or (os.environ.get("CLAUDE_BIN") if spec is CLAUDE_CLI else None)
        if not requested:
            raise RuntimeError("no subject CLI given; pass a binary or --cli-command")
        self.binary = shutil.which(requested) or requested
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="mb-subject-"))
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.attempts = max(1, attempts)
        self.retry_delay = 2.0  # seconds, grows linearly per attempt
        self.resolved_model: str | None = None
        self.calls = 0
        self._sessions: dict[tuple[str, ...], str] = {}

    def ensure_binary(self) -> None:
        """Raise before a run starts if the subject CLI is not installed."""
        if not Path(self.binary).exists() and not shutil.which(self.binary):
            raise FileNotFoundError(
                f"{self.binary} not found on PATH; install it or give an absolute path"
            )

    def build_argv(self, messages: list[dict[str, str]], model: str) -> tuple[list[str], str]:
        """Return (argv, prompt) for the next call, resuming when supported."""
        spec = self.spec
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        history = assistant_history(messages)
        session = self._sessions.get(history) if history else None

        if spec.replays:
            prompt = render_replay(messages, spec.inline_system)
            template = spec.argv
        elif session:
            prompt = trailing_user_prompt(messages)
            template = spec.resume_argv or spec.argv
        else:
            prompt = trailing_user_prompt(messages)
            if spec.inline_system and system:
                prompt = f"{system}\n\n{prompt}"
            template = spec.argv

        argv = [
            part.replace(MODEL, model)
            .replace(SYSTEM, system)
            .replace(SESSION, session or "")
            .replace(PROMPT, prompt)
            for part in template
        ]
        return argv, prompt

    async def __call__(self, messages: list[dict[str, str]], model: str, temperature: float) -> str:
        argv, _ = self.build_argv(messages, model)
        history = assistant_history(messages)

        data = await self._invoke_with_retry(argv)
        reply = self._reply_of(data)
        self.calls += 1
        if self.resolved_model is None:
            self.resolved_model = resolve_model(data.get("modelUsage") or {}, model)
        session = _dig(data, self.spec.session_path)
        if session:
            self._sessions[history + (reply,)] = str(session)
        return reply

    def _reply_of(self, data: dict) -> str:
        if self.spec.reply_path is None:
            return str(data.get("__stdout__") or "")
        value = _dig(data, self.spec.reply_path)
        if value is None:
            raise RuntimeError(
                f"{self.spec.name}: no {'.'.join(self.spec.reply_path)} in the CLI output"
            )
        return str(value)

    async def _invoke_with_retry(self, argv: list[str]) -> dict:
        """Retry a failed or hung call a few times; a machine going to sleep
        mid-run should not cost the whole run."""
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                data = await self._invoke(argv)
                if self.spec.error_path and _dig(data, self.spec.error_path):
                    raise RuntimeError(f"{self.spec.name} failed: {self._safe_reply(data)[:300]}")
                return data
            except RuntimeError as e:
                last = e
                if attempt + 1 < self.attempts:
                    await asyncio.sleep(self.retry_delay * (attempt + 1))
        raise RuntimeError(f"{self.spec.name} failed after {self.attempts} attempts: {last}")

    def _safe_reply(self, data: dict) -> str:
        try:
            return self._reply_of(data)
        except RuntimeError:
            return json.dumps(data)[:300]

    async def _invoke(self, argv: list[str]) -> dict:
        self.ensure_binary()  # FileNotFoundError is not retried: it will not fix itself
        env = {**os.environ, **self.spec.env} if self.spec.env else None
        proc = await asyncio.create_subprocess_exec(
            self.binary,
            *argv,
            cwd=str(self.workdir),
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"{self.spec.name} timed out after {self.timeout:.0f}s") from None
        text = out.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(
                f"{self.spec.name} exited with {proc.returncode}: "
                f"{err.decode(errors='replace')[:300]}"
            )
        if self.spec.reply_path is None and self.spec.session_path is None:
            return {"__stdout__": text.strip()}
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # the CLI may print a warning line before the JSON document
            start = text.find("{")
            if start == -1:
                raise RuntimeError(f"{self.spec.name} returned no JSON: {text[:300]}") from None
            return json.loads(text[start:])


# Backwards-compatible name for the Claude preset.
HARNESS = CLAUDE_CLI.name


class ClaudeCliProvider(SubjectCliProvider):
    """The verified preset: headless ``claude -p`` as the subject."""

    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("spec", CLAUDE_CLI)
        super().__init__(**kwargs)
