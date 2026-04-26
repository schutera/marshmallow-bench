"""Tests for the CLI score and report subcommands."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from marshmallow_bench.cli import _cmd_report, _cmd_score
from marshmallow_bench.runner import BenchResult, run_bench


def _make_constant_generate(response: str):
    async def generate(messages, model, temperature):
        return response

    return generate


def _build_result(model: str = "fake/test-model", n_trials: int = 2) -> BenchResult:
    generate = _make_constant_generate('{"action":"take","reasoning":"ok"}')
    return asyncio.run(run_bench(generate, model, n_trials=n_trials))


def test_cli_score_from_json(tmp_path: Path, capsys):
    result = _build_result()
    json_path = tmp_path / "result.json"
    json_path.write_text(result.to_json())

    args = argparse.Namespace(file=str(json_path))
    _cmd_score(args)

    out = capsys.readouterr().out
    assert "kappa:" in out
    assert result.model in out
    assert f"{result.kappa.kappa:.3f}" in out


def test_cli_report_from_json(tmp_path: Path, capsys):
    result = _build_result(model="fake/report-model")
    json_path = tmp_path / "report-input.json"
    json_path.write_text(result.to_json())

    out_md = tmp_path / "out.md"
    args = argparse.Namespace(file=str(json_path), output=str(out_md))
    _cmd_report(args)

    assert out_md.exists()
    content = out_md.read_text(encoding="utf-8")
    assert "fake/report-model" in content
    assert "Marshmallow Bench Report" in content


def test_cli_report_default_output_path(tmp_path: Path):
    result = _build_result()
    json_path = tmp_path / "model.json"
    json_path.write_text(result.to_json())

    args = argparse.Namespace(file=str(json_path), output=None)
    _cmd_report(args)

    expected = json_path.with_suffix(".md")
    assert expected.exists()


def test_cli_report_handles_missing_optional_fields(tmp_path: Path):
    result = _build_result()
    data = result.to_dict()
    data.pop("timestamp", None)
    data.pop("prompt_hash_g", None)
    data.pop("prompt_hash_h", None)

    json_path = tmp_path / "minimal.json"
    json_path.write_text(json.dumps(data))

    out_md = tmp_path / "minimal.md"
    args = argparse.Namespace(file=str(json_path), output=str(out_md))
    _cmd_report(args)

    assert out_md.exists()
