from __future__ import annotations

import abc
import io
import tarfile
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from daiv_sandbox.schemas import RunResult
    from daiv_sandbox.sessions import SandboxDockerSession


LANGUAGE_BASE_IMAGES = {"python": "ghcr.io/astral-sh/uv:python3.12-bookworm-slim"}


class LanguageManager(abc.ABC):
    """
    Abstract base class for language managers.
    """

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

    def run_code(self, session: SandboxDockerSession, code: str, dependencies: list[str] = None) -> RunResult:
        """
        Run code with uv, incorporating dependencies directly into the code file.
        See https://docs.astral.sh/uv/guides/scripts/#declaring-script-dependencies for more information on how dependencies are declared.
        """
        # Format dependencies in uv format if they exist
        formatted_dependencies = ""
        if dependencies:
            deps_list = [f'  "{dep}",' for dep in dependencies]
            formatted_dependencies = "# /// script\n# dependencies = [\n" + "\n".join(deps_list) + "\n# ]\n# ///\n\n"
        
        # Prepend formatted dependencies to the code
        combined_code = formatted_dependencies + code
        
        with io.BytesIO() as tar_file:
            with tarfile.open(fileobj=tar_file, mode="w:gz") as tar:
                tarinfo = tarfile.TarInfo(name="main.py")
                tarinfo.size = len(combined_code.encode())
                tar.addfile(tarinfo, io.BytesIO(combined_code.encode()))

            tar_file.seek(0)
            session.copy_to_runtime(tar_file)

        return session.execute_command("uv run main.py")
