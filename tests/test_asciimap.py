"""Tests for the ASCII behavioral map."""

from __future__ import annotations

from marshmallow_bench.asciimap import COLS, GUTTER, ROWS, RUN_MARK, quadrant, render_map


def test_quadrant_names_match_the_figure():
    assert quadrant(1.0, 1.0) == "exploitable, stoppable"
    assert quadrant(0.0, 1.0) == "not exploitable, stoppable"
    assert quadrant(1.0, 0.0) == "exploitable, unstoppable"
    assert quadrant(0.1, 0.05) == "not exploitable, unstoppable"


def _plot_rows(out: str) -> list[str]:
    lines = out.splitlines()
    top = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("┌"))
    bottom = next(i for i, ln in enumerate(lines) if ln.lstrip().startswith("└"))
    return lines[top + 1 : bottom]


def test_render_map_geometry_is_a_closed_frame():
    out = render_map(0.6, 0.4, "fake/model (this run)")
    rows = _plot_rows(out)
    assert len(rows) == ROWS
    assert all(len(ln) == GUTTER + 1 + COLS + 1 for ln in rows)
    assert all(ln[GUTTER] in "│┤┼" and ln[-1] in "│├┤" for ln in rows)
    assert sum(ln.count(RUN_MARK) for ln in rows) == 1
    for label in ("1", "0.75", "0.5", "0.25", "0"):
        assert any(ln.startswith(f"{label:>{GUTTER}}") for ln in rows)


def test_render_map_summary_lines():
    out = render_map(0.6, 0.4, "fake/model (this run)")
    assert "fake/model (this run): active 0.60, passive 0.40, κ 0.500" in out
    assert "exploitable, unstoppable" in out
    assert "Nearest leaderboard entry" in out
    # no per-model legend any more
    assert "DeepSeek" not in out or "Nearest leaderboard entry: DeepSeek" in out


def test_render_map_reports_shared_spot():
    out = render_map(0.0, 1.0)
    assert "Same spot as Sonnet 4.6, Opus 4.7." in out


def test_run_marker_position():
    rows = _plot_rows(render_map(1.0, 1.0))
    assert rows[0][GUTTER + 1 + COLS - 1] == RUN_MARK  # top-right cell
    rows = _plot_rows(render_map(0.0, 0.0))
    assert rows[-1][GUTTER + 1] == RUN_MARK  # bottom-left cell
