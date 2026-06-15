from benchmarks.__main__ import build_parser


def test_parser_defaults():
    ns = build_parser().parse_args([])
    assert ns.base_url == "http://localhost:8888"
    assert ns.base_image == "python:3.14-slim"
    assert ns.iterations == 30
    assert ns.warmup == 3
    assert ns.root_path == ""
    assert ns.ops == ["seed", "fs"]


def test_parser_overrides():
    ns = build_parser().parse_args([
        "--base-url",
        "http://x:9",
        "--iterations",
        "5",
        "--warmup",
        "1",
        "--ops",
        "fs",
        "--root-path",
        "/api/v1",
    ])
    assert ns.base_url == "http://x:9"
    assert ns.iterations == 5
    assert ns.warmup == 1
    assert ns.ops == ["fs"]
    assert ns.root_path == "/api/v1"
