from unittest.mock import AsyncMock, Mock, patch

import pytest

from daiv_sandbox.browser import BrowserManager
from daiv_sandbox.main import app


@pytest.fixture(autouse=True)
def _no_browser_image_probe():
    """Keep the app's lifespan off the Docker daemon: it would otherwise inspect, or pull, the browser image."""
    with patch("daiv_sandbox.main.probe_browser_image", new=AsyncMock(return_value=None)):
        yield


@pytest.fixture(autouse=True)
def browser_manager():
    """The app's BrowserManager, replaced so endpoint tests never reach Docker through it."""
    manager = Mock(spec=BrowserManager)
    manager.start = AsyncMock()
    manager.stop = AsyncMock()
    manager.remove = AsyncMock()
    manager.find = Mock(return_value=None)
    # Not monkeypatch: its undo deletes a starlette State attribute instead of restoring it.
    original = app.state.browser_manager
    app.state.browser_manager = manager
    yield manager
    app.state.browser_manager = original
