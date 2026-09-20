"""Tests for the ASCII behavioral map."""

from __future__ import annotations

from marshmallow_bench.asciimap import (
    COLS,
    REFERENCE_MODELS,
    ROWS,
    RUN_MARK,
    quadrant,
    render_map,
)


def test_quadrant_names_match_the_figure():
    assert quadrant(1.0, 1.0) == "exploitable, stoppable"
    assert quadrant(0.0, 1.0) == "not exploitable, stoppable"
    assert quadrant(1.0, 0.0) == "exploitable, unstoppable"
    assert quadrant(0.1, 0.05) == "not exploitable, unstoppable"


def test_render_map_has_grid_legend_and_landing_line():
    out = render_map(0.6, 0.4, "fake/model (this run)")
    lines = out.splitlines()
    plot = [ln for ln in lines if ln.startswith((" 1.0 ", " 0.5 ", " 0.0 ", "     │"))]
    assert len(plot) == ROWS
    assert all(len(ln) == 6 + COLS for ln in plot)
    assert RUN_MARK in out
    assert "fake/model (this run)" in out
    for name, _, _, _ in REFERENCE_MODELS:
        assert name in out
    assert "lands in: exploitable, unstoppable" in out
    assert "Nearest leaderboard entry" in out


def test_render_map_reports_shared_spot():
    out = render_map(0.0, 1.0)
    assert "Same spot as Sonnet 4.6, Opus 4.7." in out


def test_run_marker_wins_over_reference_marker():
    out = render_map(1.0, 1.0)
    top = next(ln for ln in out.splitlines() if ln.startswith(" 1.0 "))
    assert top.endswith(RUN_MARK)
