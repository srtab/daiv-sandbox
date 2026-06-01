# fs/\* deepagents-parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the `daiv-sandbox` `fs/*` raw primitives to pragmatic parity with deepagents' `BaseSandbox`: bound `fs/read` output, give `edit` model-friendly errors, make `glob` deterministic, and make `fs/write` create-only.

**Architecture:** `daiv-sandbox` is the server; the daiv client wraps `fs/*` in a deepagents `BackendProtocol` and lets deepagents' `FilesystemMiddleware` do all LLM-facing formatting. So these changes only touch the _raw primitives_: the endpoint handlers in `daiv_sandbox/main.py` and the session methods in `daiv_sandbox/sessions.py`. No new files. Python-free constraint preserved (content via the Docker archive API; existence probe via POSIX `[ -e ]`).

**Tech Stack:** Python 3.14, FastAPI, Docker SDK, Pydantic, pytest (+ pytest-cov, pytest-mock, pytest-xdist), `uv`, ruff.

**Reference spec:** `docs/superpowers/specs/2026-06-01-fs-deepagents-parity-design.md`

---

## File Structure

- **Modify `daiv_sandbox/main.py`**
  - Add module constants `READ_MAX_OUTPUT_BYTES` and `READ_TRUNCATION_MARKER`.
  - `fs_read`: byte-cap the text page (+ marker); error on oversized binary.
  - `fs_glob`: sort matches lexicographically by path.
  - `fs_write`: pass `create_only=True`; map `FileExistsError` to a quiet `ok=False` (no ERROR traceback).
- **Modify `daiv_sandbox/sessions.py`**
  - `edit_file`: EOF-newline mismatch hint on zero matches; include the count in the multiple-occurrences message.
  - `write_file`: add gated `create_only: bool = False` param with a `[ -e path ]` existence probe.
- **Modify `tests/unit_tests/test_main.py`** — glob sort, read byte-cap (text + binary), write conflict + create-only passthrough.
- **Modify `tests/unit_tests/test_sessions.py`** — edit EOF hints, updated multi-count message, write create-only behaviour, edit-still-overwrites guard.
- **Modify `CHANGELOG.md`** — `## Unreleased` entries.

Each task is independent (different methods); they can be implemented in any order. The order below goes simplest-first.

---

## Task 1: glob deterministic sort

**Files:**

- Modify: `daiv_sandbox/main.py` (`fs_glob`, ~lines 487-510)
- Test: `tests/unit_tests/test_main.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/unit_tests/test_main.py` (near the other `fs_glob` tests):

```python
def test_fs_glob_results_are_sorted(mock_session, client):
    """Glob matches are returned sorted by path (deterministic, matching deepagents' sorted glob),
    regardless of the order find_paths enumerated them."""
    from daiv_sandbox.sessions import DirEntry

    mock_session.find_paths.return_value = [
        DirEntry("/workspace/tmp/c.py", False),
        DirEntry("/workspace/tmp/a.py", False),
        DirEntry("/workspace/tmp/b.py", False),
    ]
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/glob", json={"path": "/workspace/tmp", "pattern": "*.py"}
    )
    assert resp.status_code == 200, resp.text
    assert [e["path"] for e in resp.json()["matches"]] == [
        "/workspace/tmp/a.py",
        "/workspace/tmp/b.py",
        "/workspace/tmp/c.py",
    ]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit_tests/test_main.py::test_fs_glob_results_are_sorted -v`
Expected: FAIL — matches come back in `find_paths` order `[c.py, a.py, b.py]`, not sorted.

- [ ] **Step 3: Implement the sort**

In `daiv_sandbox/main.py`, in `fs_glob`, find the block that builds `matched` and returns it:

```python
        base = request.path.rstrip("/")
        # Match the base-relative portion of each absolute entry under base.
        matched = [
            FsEntry(path=p, is_dir=d)
            for (p, d) in all_entries
            if p.startswith(f"{base}/") and regex.match(p[len(base) + 1 :])
        ]
        return FsGlobResponse(matches=matched)
```

Replace it with (adds one `sort` line before the return):

```python
        base = request.path.rstrip("/")
        # Match the base-relative portion of each absolute entry under base.
        matched = [
            FsEntry(path=p, is_dir=d)
            for (p, d) in all_entries
            if p.startswith(f"{base}/") and regex.match(p[len(base) + 1 :])
        ]
        # Deterministic order (matches deepagents' sorted glob) so client-side truncation is stable.
        matched.sort(key=lambda e: e.path)
        return FsGlobResponse(matches=matched)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit_tests/test_main.py::test_fs_glob_results_are_sorted -v`
Expected: PASS

- [ ] **Step 5: Run the existing glob tests to confirm no regression**

Run: `uv run pytest tests/unit_tests/test_main.py -k "glob" -v`
Expected: PASS (all existing `fs_glob` tests still green).

- [ ] **Step 6: Commit**

```bash
git add daiv_sandbox/main.py tests/unit_tests/test_main.py
git commit -m "feat(fs): sort glob results deterministically" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: fs/read output byte-cap

**Files:**

- Modify: `daiv_sandbox/main.py` (module constants near line 62; `fs_read`, ~lines 426-449)
- Test: `tests/unit_tests/test_main.py`

- [ ] **Step 1: Add the module constants**

In `daiv_sandbox/main.py`, after the existing constant `EXIT_CODE_TIMEOUT = 124  # matches timeout(1) convention` (~line 62), add:

```python
# Cap on the bytes a single fs/read returns. Mirrors deepagents BaseSandbox MAX_OUTPUT_BYTES /
# MAX_BINARY_BYTES. The full file is still read into the process (get_archive); this only bounds
# the response payload so a page of pathologically long lines can't produce an unbounded reply.
READ_MAX_OUTPUT_BYTES = 512_000

READ_TRUNCATION_MARKER = (
    "\n\n[Output truncated: exceeded the 512000-byte read limit. "
    "Continue with a larger offset or smaller limit to read the rest.]"
)
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/unit_tests/test_main.py` (near the other `fs_read` tests):

```python
def test_fs_read_text_truncated_at_cap(mock_session, client):
    """A text page larger than the byte-cap is truncated to the cap and ends with the marker."""
    from daiv_sandbox.main import READ_MAX_OUTPUT_BYTES

    mock_session.read_file_bytes.return_value = b"a" * (READ_MAX_OUTPUT_BYTES + 100_000)
    resp = client.post(f"/session/{mock_session.session_id}/fs/read", json={"path": "/workspace/tmp/big.txt"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["encoding"] == "utf-8"
    assert "[Output truncated" in body["content"]
    assert len(body["content"].encode("utf-8")) <= READ_MAX_OUTPUT_BYTES


def test_fs_read_text_under_cap_has_no_marker(mock_session, client):
    """A small text page is returned verbatim with no truncation marker."""
    mock_session.read_file_bytes.return_value = b"hello\nworld\n"
    resp = client.post(f"/session/{mock_session.session_id}/fs/read", json={"path": "/workspace/tmp/a.txt"})
    body = resp.json()
    assert body["encoding"] == "utf-8"
    assert body["content"] == "hello\nworld"
    assert "Output truncated" not in body["content"]


def test_fs_read_binary_over_cap_is_error(mock_session, client):
    """A binary file larger than the cap returns an error rather than an unbounded base64 blob."""
    from daiv_sandbox.main import READ_MAX_OUTPUT_BYTES

    mock_session.read_file_bytes.return_value = b"\xff" * (READ_MAX_OUTPUT_BYTES + 1)
    resp = client.post(f"/session/{mock_session.session_id}/fs/read", json={"path": "/workspace/tmp/big.bin"})
    body = resp.json()
    assert body["content"] is None
    assert "exceeds maximum preview size" in body["error"]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/unit_tests/test_main.py -k "fs_read_text_truncated or fs_read_text_under_cap or fs_read_binary_over_cap" -v`
Expected: FAIL — `test_fs_read_text_truncated_at_cap` returns the full 612000-byte content (no marker, over cap); `test_fs_read_binary_over_cap_is_error` returns base64 content with `error=None`. (`test_fs_read_text_under_cap_has_no_marker` may already pass — that is fine.)

- [ ] **Step 4: Implement the byte-cap in `fs_read`**

In `daiv_sandbox/main.py`, the current `fs_read` body after acquiring `raw` is:

```python
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return FsReadResponse(content=base64.b64encode(raw).decode("ascii"), encoding="base64")
        if not text:
            return FsReadResponse(content="System reminder: File exists but has empty contents", encoding="utf-8")
        lines = text.splitlines()
        page = lines[request.offset : request.offset + request.limit]
        if request.offset and not page:
            return FsReadResponse(error=f"Line offset {request.offset} exceeds file length ({len(lines)} lines)")
        return FsReadResponse(content="\n".join(page), encoding="utf-8")
```

Replace that block with:

```python
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            if len(raw) > READ_MAX_OUTPUT_BYTES:
                return FsReadResponse(
                    error=f"Binary file exceeds maximum preview size of {READ_MAX_OUTPUT_BYTES} bytes"
                )
            return FsReadResponse(content=base64.b64encode(raw).decode("ascii"), encoding="base64")
        if not text:
            return FsReadResponse(content="System reminder: File exists but has empty contents", encoding="utf-8")
        lines = text.splitlines()
        page = lines[request.offset : request.offset + request.limit]
        if request.offset and not page:
            return FsReadResponse(error=f"Line offset {request.offset} exceeds file length ({len(lines)} lines)")
        content = "\n".join(page)
        encoded = content.encode("utf-8")
        if len(encoded) > READ_MAX_OUTPUT_BYTES:
            marker_bytes = len(READ_TRUNCATION_MARKER.encode("utf-8"))
            # Reserve room for the marker so the total stays within the cap (deepagents' effective_limit).
            truncated = encoded[: READ_MAX_OUTPUT_BYTES - marker_bytes].decode("utf-8", errors="ignore")
            content = truncated + READ_TRUNCATION_MARKER
        return FsReadResponse(content=content, encoding="utf-8")
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `uv run pytest tests/unit_tests/test_main.py -k "fs_read_text_truncated or fs_read_text_under_cap or fs_read_binary_over_cap" -v`
Expected: PASS

- [ ] **Step 6: Run the existing read tests to confirm no regression**

Run: `uv run pytest tests/unit_tests/test_main.py -k "fs_read or roundtrip" -v`
Expected: PASS (empty-file, missing-file, small-binary base64, offset-beyond-eof all still green).

- [ ] **Step 7: Commit**

```bash
git add daiv_sandbox/main.py tests/unit_tests/test_main.py
git commit -m "feat(fs): cap fs/read output size" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: edit error messages (EOF-newline hint + occurrence count)

**Files:**

- Modify: `daiv_sandbox/sessions.py` (`edit_file`, ~lines 601-627)
- Test: `tests/unit_tests/test_sessions.py`

- [ ] **Step 1: Write the failing tests + update the existing multi-count test**

In `tests/unit_tests/test_sessions.py`, **replace** the existing test:

```python
def test_edit_file_multiple_occurrences_without_replace_all():
    s = _edit_session(b"x x x\n")
    with pytest.raises(ValueError, match="multiple_occurrences"):
        s.edit_file("/scratch/a.txt", "x", "y", replace_all=False, allowed_roots=("/scratch",))
    s.write_file.assert_not_called()
```

with (new message now includes the count):

```python
def test_edit_file_multiple_occurrences_without_replace_all():
    s = _edit_session(b"x x x\n")
    with pytest.raises(ValueError, match="appears 3 times"):
        s.edit_file("/scratch/a.txt", "x", "y", replace_all=False, allowed_roots=("/scratch",))
    s.write_file.assert_not_called()
```

Then **add** these two new tests next to it:

```python
def test_edit_file_eof_newline_unique_hint():
    """old ends with a newline the file lacks at EOF, and the stripped key is unique → precise hint."""
    s = _edit_session(b"abcdefkey")
    with pytest.raises(ValueError, match="trailing newline removed"):
        s.edit_file("/scratch/a.txt", "key\n", "KEY\n", replace_all=False, allowed_roots=("/scratch",))
    s.write_file.assert_not_called()


def test_edit_file_eof_newline_ambiguous_hint():
    """old ends with a newline the file lacks at EOF, and the stripped key is ambiguous →
    hint to drop the newline AND add surrounding context."""
    s = _edit_session(b"abckeydefkey")
    with pytest.raises(ValueError, match="add surrounding context"):
        s.edit_file("/scratch/a.txt", "key\n", "KEY\n", replace_all=False, allowed_roots=("/scratch",))
    s.write_file.assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit_tests/test_sessions.py -k "edit_file_eof_newline or edit_file_multiple_occurrences" -v`
Expected: FAIL — `multiple_occurrences` currently raises the bare code (no "appears 3 times"); the EOF cases currently raise `string_not_found` (no "trailing newline removed" / "add surrounding context").

- [ ] **Step 3: Implement the new error logic in `edit_file`**

In `daiv_sandbox/sessions.py`, the current tail of `edit_file` is:

```python
        if count == 0:
            raise ValueError("string_not_found")
        if count > 1 and not replace_all:
            raise ValueError("multiple_occurrences")
        result = text.replace(matched_old, matched_new) if replace_all else text.replace(matched_old, matched_new, 1)
        self.write_file(path, result.encode("utf-8"), mode=0o644, allowed_roots=allowed_roots)
        return count
```

Replace it with:

```python
        if count == 0:
            # EOF-newline mismatch hint (port of deepagents perform_string_replacement): the model
            # appended a terminator `old` carries but the file lacks at EOF. Compare on LF-normalized
            # forms so a CRLF file is handled the same way the variant loop above does.
            text_lf = text.replace("\r\n", "\n")
            if old_lf.endswith("\n") and len(old_lf) > 1 and text_lf.endswith(old_lf.removesuffix("\n")):
                stripped = old_lf.removesuffix("\n")
                stripped_count = text_lf.count(stripped)
                if stripped_count == 1:
                    raise ValueError(
                        "old_string ends with a newline, but the file does not end with a newline. "
                        "Retry with the trailing newline removed from old_string "
                        "(and from new_string if it also ends with a newline)."
                    )
                raise ValueError(
                    f"old_string ends with a newline, but the file does not end with a newline. "
                    f"With the trailing newline removed, old_string would appear {stripped_count} "
                    f"times in the file. Retry with the trailing newline removed and add surrounding "
                    f"context so the match is unique."
                )
            raise ValueError("string_not_found")
        if count > 1 and not replace_all:
            raise ValueError(
                f"String appears {count} times in file. Use replace_all=True to replace all instances, "
                f"or provide a more specific string with surrounding context."
            )
        result = text.replace(matched_old, matched_new) if replace_all else text.replace(matched_old, matched_new, 1)
        self.write_file(path, result.encode("utf-8"), mode=0o644, allowed_roots=allowed_roots)
        return count
```

- [ ] **Step 4: Run the new/updated tests to verify they pass**

Run: `uv run pytest tests/unit_tests/test_sessions.py -k "edit_file_eof_newline or edit_file_multiple_occurrences" -v`
Expected: PASS

- [ ] **Step 5: Run all edit_file tests to confirm no regression**

Run: `uv run pytest tests/unit_tests/test_sessions.py -k "edit_file" -v`
Expected: PASS — `string_not_found` (plain, no trailing newline), single replacement, replace_all, and CRLF matching all still green.

- [ ] **Step 6: Commit**

```bash
git add daiv_sandbox/sessions.py tests/unit_tests/test_sessions.py
git commit -m "feat(fs): add EOF-newline hint and count to edit errors" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: fs/write create-only

**Files:**

- Modify: `daiv_sandbox/sessions.py` (`write_file`, ~lines 480-495)
- Modify: `daiv_sandbox/main.py` (`fs_write`, ~lines 407-423)
- Test: `tests/unit_tests/test_sessions.py`, `tests/unit_tests/test_main.py`

- [ ] **Step 1: Write the failing session-level tests**

Add to `tests/unit_tests/test_sessions.py` (near the other `write_file` tests):

```python
def test_write_file_create_only_rejects_existing(mock_docker_client):
    """create_only=True refuses to overwrite: an existence probe that finds the file raises
    FileExistsError before any archive is copied."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=0, output="EXISTS"))
    s.copy_to_container = Mock()
    with pytest.raises(FileExistsError, match="already exists"):
        s.write_file(f"{SANDBOX_ROOT}/a.txt", b"x", mode=0o644, allowed_roots=(SANDBOX_ROOT,), create_only=True)
    s.copy_to_container.assert_not_called()


def test_write_file_create_only_allows_new(mock_docker_client):
    """create_only=True writes when the probe reports the path is absent."""
    s = _session_with_container()
    s.execute_command = Mock(return_value=Mock(exit_code=0, output=""))
    s.copy_to_container = Mock()
    s.write_file(f"{SANDBOX_ROOT}/a.txt", b"x", mode=0o644, allowed_roots=(SANDBOX_ROOT,), create_only=True)
    s.copy_to_container.assert_called_once()


def test_edit_file_write_back_does_not_use_create_only():
    """edit's write-back must overwrite the file it just read (create_only must stay falsy)."""
    s = _edit_session(b"hello world\n")
    s.edit_file("/scratch/a.txt", "world", "there", replace_all=False, allowed_roots=("/scratch",))
    assert not s.write_file.call_args.kwargs.get("create_only")
```

Note: `mock_docker_client` is the existing fixture; `_session_with_container`, `_edit_session`, `Mock`, and `SANDBOX_ROOT` are already imported/defined in this file.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/unit_tests/test_sessions.py -k "create_only or write_back_does_not_use" -v`
Expected: FAIL — `write_file` does not accept a `create_only` keyword yet (TypeError).

- [ ] **Step 3: Implement `create_only` in `write_file`**

In `daiv_sandbox/sessions.py`, the current `write_file` is:

```python
    def write_file(
        self, path: str, content: bytes, *, mode: int, allowed_roots: tuple[str, ...] = (SANDBOX_ROOT,)
    ) -> None:
        """
        Write *content* to *path* (absolute, under one of *allowed_roots*) inside the container.

        The path is validated lexically. The content is shipped via a single-file tar
        through the existing copy_to_container pipeline (sanitised, mode preserved).
        """
        canonical = _validate_sandbox_path(path, allowed_roots=allowed_roots)
        parent_dir, _, filename = canonical.rpartition("/")
        if not parent_dir or not filename:
            raise ValueError(f"path resolves to an unusable location: {path!r}")

        with _build_single_file_tar_stream(filename, content, mode=mode) as tar_stream:
            self.copy_to_container(tar_stream, dest=parent_dir, clear_before_copy=False)
```

Replace it with:

```python
    def write_file(
        self,
        path: str,
        content: bytes,
        *,
        mode: int,
        allowed_roots: tuple[str, ...] = (SANDBOX_ROOT,),
        create_only: bool = False,
    ) -> None:
        """
        Write *content* to *path* (absolute, under one of *allowed_roots*) inside the container.

        The path is validated lexically. The content is shipped via a single-file tar
        through the existing copy_to_container pipeline (sanitised, mode preserved).

        When *create_only* is True, refuse to overwrite an existing path (matching deepagents'
        create-only ``write`` contract). The check probes existence with ``[ -e ]``; there is an
        inherent TOCTOU window between the probe and the write. The default (False) overwrites,
        which is what ``edit_file``'s write-back relies on.
        """
        canonical = _validate_sandbox_path(path, allowed_roots=allowed_roots)
        parent_dir, _, filename = canonical.rpartition("/")
        if not parent_dir or not filename:
            raise ValueError(f"path resolves to an unusable location: {path!r}")

        if create_only:
            # `|| true` keeps the exit code 0 on the common (absent) path so execute_command does not
            # log a spurious warning; presence is signalled by the EXISTS marker on stdout.
            probe = self.execute_command(f"[ -e {_sh_quote(canonical)} ] && printf EXISTS || true")
            if probe.output.strip() == "EXISTS":
                raise FileExistsError(
                    f"Cannot write to {path} because it already exists. "
                    "Read and then make an edit, or write to a new path."
                )

        with _build_single_file_tar_stream(filename, content, mode=mode) as tar_stream:
            self.copy_to_container(tar_stream, dest=parent_dir, clear_before_copy=False)
```

- [ ] **Step 4: Run the session-level tests to verify they pass**

Run: `uv run pytest tests/unit_tests/test_sessions.py -k "create_only or write_back_does_not_use" -v`
Expected: PASS

- [ ] **Step 5: Write the failing endpoint tests**

Add to `tests/unit_tests/test_main.py` (near the other `fs_write` tests):

```python
def test_fs_write_conflict_returns_quiet_error(mock_session, client):
    """A create-only conflict (write_file raises FileExistsError) is surfaced as ok=False with the
    deepagents message — not an HTTP error."""
    mock_session.write_file.side_effect = FileExistsError(
        "Cannot write to /workspace/tmp/a.txt because it already exists. "
        "Read and then make an edit, or write to a new path."
    )
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/write",
        json={"path": "/workspace/tmp/a.txt", "content": base64.b64encode(b"x").decode(), "mode": 0o644},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "already exists" in body["error"]


def test_fs_write_passes_create_only(mock_session, client):
    """fs/write requests create-only semantics from the session layer."""
    mock_session.write_file.return_value = None
    resp = client.post(
        f"/session/{mock_session.session_id}/fs/write",
        json={"path": "/workspace/tmp/a.txt", "content": base64.b64encode(b"x").decode(), "mode": 0o644},
    )
    assert resp.status_code == 200, resp.text
    assert mock_session.write_file.call_args.kwargs.get("create_only") is True
```

- [ ] **Step 6: Run the endpoint tests to verify they fail**

Run: `uv run pytest tests/unit_tests/test_main.py -k "fs_write_conflict or fs_write_passes_create_only" -v`
Expected: FAIL — `fs_write` does not pass `create_only=True` yet, and a `FileExistsError` currently goes through the broad `except Exception` (still `ok=False` but the `create_only` assertion fails).

- [ ] **Step 7: Implement the `fs_write` endpoint change**

In `daiv_sandbox/main.py`, the current `fs_write` body is:

```python
    async with _workspace_executor(http_request, session_id) as cmd:
        try:
            await asyncio.to_thread(
                cmd.write_file, request.path, request.content, mode=request.mode, allowed_roots=_WORKSPACE_ROOTS
            )
        except Exception as exc:
            logger.exception("fs_write failed for %s", request.path)
            return FsWriteResponse(ok=False, error=str(exc))
        return FsWriteResponse(ok=True)
```

Replace it with:

```python
    async with _workspace_executor(http_request, session_id) as cmd:
        try:
            await asyncio.to_thread(
                cmd.write_file,
                request.path,
                request.content,
                mode=request.mode,
                allowed_roots=_WORKSPACE_ROOTS,
                create_only=True,
            )
        except FileExistsError as exc:
            # Expected create-only conflict: surface it quietly (no ERROR-level traceback).
            return FsWriteResponse(ok=False, error=str(exc))
        except Exception as exc:
            logger.exception("fs_write failed for %s", request.path)
            return FsWriteResponse(ok=False, error=str(exc))
        return FsWriteResponse(ok=True)
```

- [ ] **Step 8: Run the endpoint tests to verify they pass**

Run: `uv run pytest tests/unit_tests/test_main.py -k "fs_write" -v`
Expected: PASS — the two new tests pass and the existing `fs_write` tests (roundtrip, repo-subdir accept, outside-workspace reject, bare-root reject, sibling reject) stay green.

- [ ] **Step 9: Commit**

```bash
git add daiv_sandbox/sessions.py daiv_sandbox/main.py tests/unit_tests/test_sessions.py tests/unit_tests/test_main.py
git commit -m "feat(fs): make fs/write create-only" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: CHANGELOG + full validation

**Files:**

- Modify: `CHANGELOG.md` (`## Unreleased`)

- [ ] **Step 1: Add CHANGELOG entries**

In `CHANGELOG.md`, under `## Unreleased`, add to the existing `### Changed` section (after the `fs/glob` bullet, ~line 19):

```markdown
- `fs/read` now bounds its response: a text page larger than 512000 bytes is truncated with a marker, and a binary file larger than 512000 bytes returns an error instead of an unbounded base64 blob (mirrors deepagents' read limits).
- `fs/write` is now create-only: writing to a path that already exists returns `ok=False` with a message to read-and-edit or pick a new path (matches the deepagents `write` contract). Editing an existing file via `fs/edit` is unaffected.
- `fs/glob` results are now returned sorted by path, so client-side truncation is deterministic.
- `fs/edit` returns clearer errors: a precise hint when `old` carries a trailing newline the file lacks at EOF, and the occurrence count in the multiple-matches error.
```

- [ ] **Step 2: Run the full unit suite**

Run: `make test`
Expected: PASS — all unit tests green (existing + the new read-cap, glob-sort, edit-hint, and write create-only tests).

- [ ] **Step 3: Run lint**

Run: `make lint`
Expected: PASS (ruff check + format + pyproject-fmt clean). If ruff reports formatting, run `make lint-fix` and re-run `make lint`.

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: changelog for fs deepagents-parity changes" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

**1. Spec coverage:**

- Spec change #1 (read byte-cap, text + binary) → Task 2. ✓
- Spec change #2 (edit EOF-newline hint + count) → Task 3. ✓
- Spec change #3 (glob deterministic sort) → Task 1. ✓
- Spec change #4 (write create-only, gated; edit unaffected) → Task 4 (incl. `test_edit_file_write_back_does_not_use_create_only` regression guard). ✓
- Spec testing list (text over/under cap, binary too large, EOF unique/ambiguous, multi-count, glob sort, write conflict + new path, edit-still-overwrites) → all present across Tasks 1-4. ✓
- CHANGELOG update + `make test` validation → Task 5. ✓
- Out-of-scope items (grep path-glob, ls/glob metadata, read windowing) → correctly absent. ✓

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to". Every code step shows complete code. ✓

**3. Type/name consistency:**

- `READ_MAX_OUTPUT_BYTES` / `READ_TRUNCATION_MARKER` defined in Task 2 Step 1, used in Task 2 Step 4 and imported in tests. ✓
- `write_file(..., create_only: bool = False)` defined in Task 4 Step 3; called with `create_only=True` from `fs_write` (Task 4 Step 7) and asserted in tests. ✓
- `FileExistsError` raised in `write_file` (Task 4 Step 3) and caught in `fs_write` (Task 4 Step 7). ✓
- `edit_file` reuses `old_lf` and `text` already in scope; new message strings match the test `match=` substrings ("appears 3 times", "trailing newline removed", "add surrounding context"). ✓
- `mock_session.write_file` / `read_file_bytes` / `find_paths` are the existing mock attributes used by current tests. ✓
