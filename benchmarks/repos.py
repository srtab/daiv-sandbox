"""Pinned public-repo corpora (frozen to a commit SHA for reproducibility).

Captured 2026-06-10 from each repo's default-branch HEAD. file_count is the number of
regular files in the codeload tarball at that SHA (recorded for reference; the runtime
value is recomputed by corpus.fetch_repo_corpus and written into every report).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepoSpec:
    owner: str
    repo: str
    sha: str
    name: str


REPOS: list[RepoSpec] = [
    RepoSpec("psf", "requests", "6f66281a1d6326b1b9c4ac09ca30de0fc4e6ef43", "small"),  # ~130 files
    RepoSpec("pallets", "flask", "36e4a824f340fdee7ed50937ba8e7f6bc7d17f81", "medium"),  # ~236 files
    RepoSpec("django", "django", "a2f8a4a6f9ac094edde937e56a3ecbd112ee448c", "large"),  # ~7066 files
]
