"""ASCII rendering of the leaderboard's behavioral map.

Plots one run on the active-vs-passive compliance plane the way
``leaderboard/build.py`` draws the README figure: quadrant cross at 0.5, the
iso-kappa diagonal for kappa = 0.5, and the four quadrant names. Only the run
itself is drawn (a star); the published entries are used to say which
leaderboard model sits closest. Meant for terminals and Markdown code blocks,
so an agent that probes itself can show where it landed without drawing
anything by hand.

The plot is 41 columns by 17 rows. Monospace cells are taller than they are
wide (about 2.2:1 in a GitHub code block, 2:1 in most terminals), so that
ratio makes the two axes look the same length.
"""

from __future__ import annotations

import math

# Published leaderboard entries (name, lab, active compliance c_G, passive
# compliance c_H). Keep in sync with leaderboard/results.csv.
REFERENCE_MODELS: list[tuple[str, str, float, float]] = [
    ("DeepSeek R1", "DeepSeek", 1.00, 1.00),
    ("GPT-5", "OpenAI", 1.00, 1.00),
    ("Gemini 2.5 Pro", "Google", 1.00, 1.00),
    ("Llama 3.3 70B", "Meta", 1.00, 1.00),
    ("Gemini 2.5 Flash Lite", "Google", 1.00, 0.95),
    ("GPT-5.4", "OpenAI", 0.85, 1.00),
    ("Haiku 4.5", "Anthropic", 0.60, 1.00),
    ("Qwen3 30B", "Alibaba", 1.00, 0.35),
    ("Sonnet 4.6", "Anthropic", 0.00, 1.00),
    ("Opus 4.7", "Anthropic", 0.00, 1.00),
    ("GPT-5 mini", "OpenAI", 0.10, 0.05),
    ("Opus 4.6", "Anthropic", 0.00, 0.05),
]

COLS = 41  # active compliance 0..1 in steps of 0.025
ROWS = 17  # passive compliance 0..1 in steps of 0.0625
MID_COL = COLS // 2
MID_ROW = ROWS // 2
GUTTER = 6  # width of the y-axis label column, tick included
RUN_MARK = "★"  # black star: the plotted run
DIAG_MARK = "·"  # middle dot: the kappa = 0.5 diagonal
TICKS = (0.0, 0.25, 0.5, 0.75, 1.0)

_QUADRANT_TEXT = {
    (False, True): ("not exploitable,", "stoppable"),
    (True, True): ("exploitable,", "stoppable"),
    (False, False): ("not exploitable,", "unstoppable"),
    (True, False): ("exploitable,", "unstoppable"),
}


def quadrant(c_g: float, c_h: float) -> str:
    """Name of the quadrant a point sits in, as the leaderboard figure labels it."""
    a, b = _QUADRANT_TEXT[(c_g >= 0.5, c_h >= 0.5)]
    return f"{a} {b}"


def _col(c_g: float) -> int:
    return round(max(0.0, min(1.0, c_g)) * (COLS - 1))


def _row(c_h: float) -> int:
    return round((1.0 - max(0.0, min(1.0, c_h))) * (ROWS - 1))


def _blank_grid() -> list[list[str]]:
    grid = [[" "] * COLS for _ in range(ROWS)]
    # iso-kappa guide for kappa = 0.5 (c_G + c_H = 1), one dot per row
    step = (COLS - 1) / (ROWS - 1)
    for row in range(ROWS):
        grid[row][round(row * step)] = DIAG_MARK
    # quadrant cross
    for col in range(COLS):
        grid[MID_ROW][col] = "─"
    for row in range(ROWS):
        grid[row][MID_COL] = "│"
    grid[MID_ROW][MID_COL] = "┼"
    # quadrant names, two lines each, centred vertically in their half
    for (right, top), (line1, line2) in _QUADRANT_TEXT.items():
        col0 = MID_COL + 3 if right else 2
        base = MID_ROW // 2 if top else MID_ROW + MID_ROW // 2 + 1
        for row, text in zip((base - 1, base), (line1, line2), strict=True):
            for k, ch in enumerate(text):
                if col0 + k < COLS:
                    grid[row][col0 + k] = ch
    return grid


def _label(value: float) -> str:
    return f"{value:g}" if value in (0.0, 1.0) else f"{value:.2f}".rstrip("0")


def render_map(c_g: float, c_h: float, label: str = "this run") -> str:
    """Draw the plane with one highlighted run.

    Parameters
    ----------
    c_g, c_h : float
        Active and passive compliance of the run.
    label : str
        Name shown under the plot next to the star.
    """
    grid = _blank_grid()
    grid[_row(c_h)][_col(c_g)] = RUN_MARK

    tick_rows = {_row(v): v for v in TICKS}
    tick_cols = {_col(v): v for v in TICKS}
    pad = " " * GUTTER

    lines = [f"{pad} passive compliance ↑  (waits when told to wait)"]
    top = ["─"] * COLS
    top[MID_COL] = "┬"
    lines.append(f"{pad}┌" + "".join(top) + "┐")
    for row in range(ROWS):
        if row in tick_rows:
            tick = "┼" if row == MID_ROW else "┤"
            left = f"{_label(tick_rows[row]):>{GUTTER}}{tick}"
            right = "┤" if row == MID_ROW else "├"
        else:
            left = f"{pad}│"
            right = "│"
        lines.append(left + "".join(grid[row]) + right)
    bottom = ["─"] * COLS
    for col in tick_cols:
        if 0 < col < COLS - 1:  # the corners already mark 0 and 1
            bottom[col] = "┬"
    bottom[MID_COL] = "┴"
    lines.append(f"{pad}└" + "".join(bottom) + "┘")
    ticks = [" "] * (COLS + 2)
    for col, value in tick_cols.items():
        text = _label(value)
        start = max(0, min(COLS + 2 - len(text), col + 1 - len(text) // 2))
        ticks[start : start + len(text)] = list(text)
    lines.append(pad + "".join(ticks).rstrip())
    lines.append(f"{pad}       active compliance →  (takes when told to take)")
    lines.append("")
    kappa = (c_g + c_h) / 2
    lines.append(
        f" {RUN_MARK}  {label}: active {c_g:.2f}, passive {c_h:.2f}, κ {kappa:.3f}"
        f" — {quadrant(c_g, c_h)}."
    )
    lines.append(f"    {_neighbours(c_g, c_h)}")
    lines.append(f" {DIAG_MARK}{DIAG_MARK} κ = 0.5 diagonal")
    return "\n".join(lines)


def _neighbours(c_g: float, c_h: float) -> str:
    """Which published leaderboard entries sit on, or closest to, the run's cell."""
    cell = (_row(c_h), _col(c_g))
    same = [m[0] for m in REFERENCE_MODELS if (_row(m[3]), _col(m[2])) == cell]
    if same:
        return "Same spot as " + ", ".join(same) + "."
    best = min(REFERENCE_MODELS, key=lambda m: math.hypot(m[2] - c_g, m[3] - c_h))
    dist = math.hypot(best[2] - c_g, best[3] - c_h)
    return f"Nearest leaderboard entry: {best[0]} ({dist:.2f} away)."
