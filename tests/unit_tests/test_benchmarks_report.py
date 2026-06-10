import json

from benchmarks.report import render, write


def _groups():
    return {"seed": {"small": [1.0, 2.0, 3.0], "large": [10.0, 20.0, 30.0]}, "fs": {"grep": [4.0, 5.0, 6.0]}}


def test_render_markdown_has_sections_and_rows():
    md, obj = render({"service_version": "9.9.9", "base_url": "http://x"}, _groups())
    assert "## seed" in md
    assert "## fs" in md
    assert "small" in md and "large" in md and "grep" in md
    assert "9.9.9" in md  # metadata header rendered
    assert obj["groups"]["seed"]["small"]["summary"]["n"] == 3
    assert obj["groups"]["seed"]["small"]["samples_ms"] == [1.0, 2.0, 3.0]
    assert obj["meta"]["service_version"] == "9.9.9"


def test_write_emits_md_and_json(tmp_path):
    md_path, json_path = write(tmp_path, {"service_version": "9.9.9"}, _groups())
    assert md_path.exists() and md_path.suffix == ".md"
    assert json_path.exists() and json_path.suffix == ".json"
    loaded = json.loads(json_path.read_text())
    assert loaded["groups"]["fs"]["grep"]["summary"]["n"] == 3
    assert md_path.stem == json_path.stem  # same timestamp basename


def test_write_stamp_wins_over_caller_generated_at(tmp_path):
    # write() owns the timestamp: a caller-supplied generated_at must not survive.
    md_path, json_path = write(tmp_path, {"generated_at": "caller-value"}, _groups())
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded["meta"]["generated_at"] != "caller-value"
    assert loaded["meta"]["generated_at"] == md_path.stem  # equals the filename stamp
