"""Tolerant parser for structured decision output.

Prefers strict JSON. Falls back to regex if the model emits stray prose.
Parse failures are logged as first-class data.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_ACTION_RE = re.compile(r'"action"\s*:\s*"(take|wait)"', re.IGNORECASE)
_REASONING_RE = re.compile(r'"reasoning"\s*:\s*"((?:[^"\\]|\\.)*)"', re.DOTALL)
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


@dataclass
class ParsedDecision:
    action: str  # "take" or "wait"
    reasoning: str
    parse_ok: bool
    parse_note: str = ""


def _strip_fences(s: str) -> str:
    return _FENCE_RE.sub("", s).strip()


def _find_json_object(s: str) -> str | None:
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def parse_decision(raw: str) -> ParsedDecision:
    """Extract {reasoning, action} from the model's response.

    Attempts, in order:
    1. Strict json.loads (after stripping code fences).
    2. json.loads of the first balanced {...} substring.
    3. Regex extraction of action + reasoning.
    4. Bare word fallback.
    5. Unparseable (returns action="wait", parse_ok=False).
    """
    stripped = _strip_fences(raw)

    for candidate in (stripped, _find_json_object(stripped)):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        action = obj.get("action")
        reasoning = obj.get("reasoning")
        if isinstance(action, str):
            action = action.strip().lower()
        if action in ("take", "wait"):
            return ParsedDecision(
                reasoning=(reasoning or "").strip() if isinstance(reasoning, str) else "",
                action=action,
                parse_ok=True,
            )

    m_action = _ACTION_RE.search(stripped)
    m_reason = _REASONING_RE.search(stripped)
    if m_action:
        action = m_action.group(1).lower()
        reasoning = m_reason.group(1) if m_reason else ""
        note = "regex_fallback" if m_reason else "regex_fallback_no_reasoning"
        return ParsedDecision(
            reasoning=reasoning,
            action=action,
            parse_ok=True,
            parse_note=note,
        )

    tokens = re.findall(r"\b(take|wait)\b", stripped, flags=re.IGNORECASE)
    if tokens:
        return ParsedDecision(
            reasoning="",
            action=tokens[-1].lower(),
            parse_ok=True,
            parse_note="bare_word_fallback",
        )

    return ParsedDecision(
        reasoning=stripped[:500],
        action="wait",
        parse_ok=False,
        parse_note="unparseable",
    )
