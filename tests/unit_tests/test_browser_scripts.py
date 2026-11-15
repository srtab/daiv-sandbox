import importlib.util
import json
from pathlib import Path

import pytest

BROWSER_DIR = Path(__file__).resolve().parents[2] / "browser"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"browser_{name}", BROWSER_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launch = _load("launch")
save_tabs = _load("save_tabs")


def test_only_web_pages_are_saved_least_recently_active_first():
    targets = [
        {"type": "page", "url": "https://example.com/newest"},
        {"type": "page", "url": "data:text/html,<script>alert(1)</script>"},
        {"type": "page", "url": "file:///profile/Default/Cookies"},
        {"type": "page", "url": "chrome://settings"},
        {"type": "page", "url": "about:blank"},
        {"type": "service_worker", "url": "http://localhost:5173/sw.js"},
        {"type": "page", "url": "HTTP://localhost:5173/oldest"},
    ]

    assert save_tabs.restorable_urls(targets) == ["HTTP://localhost:5173/oldest", "https://example.com/newest"]


def test_a_saved_tab_file_restores_only_web_pages(tmp_path, monkeypatch):
    tabs = tmp_path / ".daiv-tabs.json"
    tabs.write_text(json.dumps(["http://localhost:5173/", "data:text/html,hi", "javascript:alert(1)"]))
    monkeypatch.setattr(launch, "TABS_FILE", tabs)

    assert launch.read_tabs() == (["http://localhost:5173/"], "ok")


@pytest.mark.parametrize("name", launch.EXECUTABLE_STATE)
def test_a_launch_wipes_state_that_could_run_code_and_keeps_cookies_and_storage(tmp_path, monkeypatch, name):
    default = tmp_path / "Default"
    (default / name / "abc").mkdir(parents=True)
    (default / "Local Storage").mkdir()
    (default / "Cookies").write_text("session=1")
    monkeypatch.setattr(launch, "PROFILE", tmp_path)

    launch.wipe_executable_state()

    assert sorted(path.name for path in default.iterdir()) == ["Cookies", "Local Storage"]
