---
name: self-probe
description: Run Marshmallow Bench on yourself (the agent) and report kappa plus your spot on the ASCII behavioral map. Use when the user says "probe yourself", "test yourself", "self-probe", "benchmark yourself", or invokes /self-probe. Optional args: N (repetitions per probe, default 5) and --submit (open a flagged PR with the results).
---

Follow the **Self-probe** section of `AGENTS.md` at the repository root, exactly
as written there. This skill adds nothing to it.

Arguments: `$ARGUMENTS`

- A bare integer sets N (repetitions per probe). Default 5.
- `--submit` means the user has asked for the results to be submitted: after
  reporting, continue with "Submitting a self-probe" in AGENTS.md. Without it,
  never open a PR.

In Claude Code use **Path A**: `marshmallow-bench run --provider claude-cli
--model <the model you are running as> --n-trials N`. Only fall back to the
subagent loop (Path B, `marshmallow-subject` subagent) if the CLI provider is
unavailable, and apply the hygiene gate after the first reply. Finish by pasting
the Behavioral Map from the output verbatim and stating the caveats.
