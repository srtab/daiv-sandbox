from unittest.mock import MagicMock

import pytest

from daiv_sandbox.languages import LanguageManager, PythonLanguageManager
from daiv_sandbox.schemas import RunResult
from daiv_sandbox.sessions import SandboxDockerSession


@pytest.fixture
def setup_manager():
    session = MagicMock(spec=SandboxDockerSession)
    manager = PythonLanguageManager()
    return session, manager


def test_factory():
    manager = LanguageManager.factory("python")
    assert isinstance(manager, PythonLanguageManager)


def test_factory_unsupported_language():
    with pytest.raises(ValueError, match="Unsupported language: unsupported"):
        LanguageManager.factory("unsupported")


def test_run_code(setup_manager):
    session, manager = setup_manager

    # Mock the expected result
    expected_result = RunResult(command="uv run main.py", output="Code executed", exit_code=0, workdir="/")
    session.execute_command.return_value = expected_result

    # Test without dependencies
    code = "print('Hello, World!')"
    result = manager.run_code(session, code)

    # Assertions
    session.copy_to_runtime.assert_called_once()
    session.execute_command.assert_called_once_with("uv run main.py")
    assert result == expected_result

    # Reset mocks
    session.reset_mock()

    # Test with dependencies
    dependencies = ["numpy", "pandas"]
    result = manager.run_code(session, code, dependencies)

    # Verify dependencies are correctly prepended to the code
    session.copy_to_runtime.assert_called_once()
    session.execute_command.assert_called_once_with("uv run main.py")
    assert result == expected_result
