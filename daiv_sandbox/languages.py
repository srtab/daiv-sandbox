from __future__ import annotations

import abc
import io
import tarfile
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from daiv_sandbox.schemas import RunResult
    from daiv_sandbox.sessions import SandboxDockerSession


LANGUAGE_BASE_IMAGES = {"python": "python:3.12-slim"}


class LanguageManager(abc.ABC):
    """
    Abstract base class for language managers.
    """

    @abc.abstractmethod
    def install_dependencies(self, session: SandboxDockerSession, dependencies: list[str]) -> RunResult:
        pass

    @abc.abstractmethod
    def run_code(self, session: SandboxDockerSession, code: str) -> RunResult:
        pass

    @staticmethod
    def factory(language: Literal["python"]) -> LanguageManager:
        if language == "python":
            return PythonLanguageManager()
        raise ValueError(f"Unsupported language: {language}")


class PythonLanguageManager(LanguageManager):
    """
    Language manager for Python.
    """

    def install_dependencies(self, session: SandboxDockerSession, dependencies: list[str]) -> RunResult:
        """
        Install dependencies.
        """
        return session.execute_command(f"pip install {' '.join(dependencies)}", workdir="/")

    def run_code(self, session: SandboxDockerSession, code: str) -> RunResult:
        """
        Run code.
        """
        with io.BytesIO() as tar_file:
            with tarfile.open(fileobj=tar_file, mode="w:gz") as tar:
                tarinfo = tarfile.TarInfo(name="main.py")
                tarinfo.size = len(code.encode())
                tar.addfile(tarinfo, io.BytesIO(code.encode()))

            tar_file.seek(0)
            session.copy_to_runtime(tar_file)

        return session.execute_command("python main.py")
