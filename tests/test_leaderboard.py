"""The README leaderboard figure and table must match leaderboard/results.csv."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LEADERBOARD = ROOT / "leaderboard"


@pytest.fixture(scope="module")
def build():
    spec = importlib.util.spec_from_file_location("leaderboard_build", LEADERBOARD / "build.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve postponed annotations via sys.modules
    spec.loader.exec_module(module)
    return module


def test_svgs_are_up_to_date(build, tmp_path):
    build.build(out_dir=tmp_path, readme_path=None)
    for name in ("leaderboard.svg", "leaderboard-dark.svg"):
        fresh = (tmp_path / name).read_text(encoding="utf-8")
        committed = (LEADERBOARD / name).read_text(encoding="utf-8")
        assert fresh == committed, f"{name} is stale: run `python leaderboard/build.py`"


def test_readme_table_is_up_to_date(build):
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    expected = build.update_readme(build.render_table(build.load_entries()), readme)
    assert readme == expected, "README table is stale: run `python leaderboard/build.py`"


def test_every_model_is_drawn(build):
    entries = build.load_entries()
    svg = (LEADERBOARD / "leaderboard.svg").read_text(encoding="utf-8")
    for entry in entries:
        assert f">{entry.name}<" in svg
    assert svg.count("<circle") == len(build.group_entries(entries))


def test_rejects_impossible_rates(build, tmp_path):
    csv = tmp_path / "results.csv"
    csv.write_text(
        "model_id,name,lab,active_compliance,passive_compliance,n_trials\n"
        "x/y,Bad,Lab,0.33,1.0,20\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="multiple of 1/20"):
        build.load_entries(csv)


HEADER = "model_id,name,lab,active_compliance,passive_compliance,n_trials,added\n"
OLD = "a/one,One,Lab,1.0,1.0,20,2026-04-24\n"
OLD_TOO = "a/two,Two,Lab,0.5,0.5,20,2026-04-24\n"
FRESH = "a/three,Three,Lab,0.5,1.0,20,2026-09-25\n"


def _csv(tmp_path, *rows):
    path = tmp_path / "results.csv"
    path.write_text(HEADER + "".join(rows), encoding="utf-8")
    return path


def test_newest_date_is_the_new_cohort(build, tmp_path):
    entries = build.load_entries(_csv(tmp_path, OLD, OLD_TOO, FRESH))
    assert build.new_cohort(entries) == {"a/three"}


def test_nothing_is_new_when_every_entry_shares_a_date(build, tmp_path):
    """The first publication is not a batch of newcomers."""
    assert build.new_cohort(build.load_entries(_csv(tmp_path, OLD, OLD_TOO))) == set()
    assert build.new_cohort(build.load_entries()) == set()  # the committed CSV, today


def test_new_entries_are_drawn_in_the_accent_colour(build, tmp_path):
    entries = build.load_entries(_csv(tmp_path, OLD, FRESH))
    svg = build.render_svg(entries, build.THEMES["light"])
    accent = build.THEMES["light"]["accent"]
    assert f'fill="{accent}"' in svg
    assert f'fill="{accent}" font-weight="600">Three</tspan>' in svg
    assert "new in this batch, added 2026-09-25: Three" in svg


def test_no_legend_without_a_new_cohort(build, tmp_path):
    entries = build.load_entries(_csv(tmp_path, OLD, OLD_TOO))
    svg = build.render_svg(entries, build.THEMES["light"])
    assert "new in this batch" not in svg
    assert build.THEMES["light"]["accent"] not in svg


def test_table_carries_the_added_date(build, tmp_path):
    table = build.render_table(build.load_entries(_csv(tmp_path, OLD, FRESH)))
    assert "| Added |" in table
    assert "**2026-09-25** 🆕" in table
    assert "| 2026-04-24 |" in table


def test_rejects_a_malformed_added_date(build, tmp_path):
    with pytest.raises(ValueError, match="not YYYY-MM-DD"):
        build.load_entries(_csv(tmp_path, "a/one,One,Lab,1.0,1.0,20,24.04.2026\n"))
