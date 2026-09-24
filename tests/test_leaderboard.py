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
