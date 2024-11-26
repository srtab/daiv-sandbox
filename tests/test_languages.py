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


def test_install_dependencies(setup_manager):
    session, manager = setup_manager

    # Mock the expected result
    expected_result = RunResult(command="pip install numpy pandas", output="Dependencies installed", exit_code=0)
    session.execute_command.return_value = expected_result

    # Call the method
    result = manager.install_dependencies(session, ["numpy", "pandas"])

    # Assertions
    session.execute_command.assert_called_once_with("pip install numpy pandas", workdir="/")
    assert result == expected_result


def test_run_code(setup_manager):
    session, manager = setup_manager

    # Mock the expected result
    expected_result = RunResult(command="python main.py", output="Code executed", exit_code=0)
    session.execute_command.return_value = expected_result

    # Call the method
    code = "print('Hello, World!')"
    result = manager.run_code(session, "/workdir", code)

    # Assertions
    session.copy_to_runtime.assert_called_once()
    session.execute_command.assert_called_once_with("python main.py", workdir="/workdir")
    assert result == expected_result
