# fs/\* primitives: pragmatic parity with deepagents

**Date:** 2026-06-01
**Status:** Approved (design)
**Scope:** `daiv_sandbox/sessions.py`, `daiv_sandbox/main.py`, tests, `CHANGELOG.md`

## Problem

The `daiv-sandbox` `fs/*` endpoints were modelled on the filesystem backend in
`deepagents`. This spec captures the result of comparing the two implementations
(deepagents 0.5.9) and the concrete, in-scope improvements that follow from it.

## Architecture: who does what

`daiv-sandbox` is the **server**. In the `daiv` repo, `SandboxFileBackend`
(`daiv/automation/agent/middlewares/file_system.py`) implements deepagents'
`BackendProtocol` by calling the `fs/*` endpoints, and hands that backend to
deepagents' `FilesystemMiddleware`. That middleware — **on the client side** — does
all the LLM-facing shaping:

- line numbering (`format_content_with_line_numbers`, cat -n style),
- long-line chunking (continuation markers `5.1`, `5.2`),
- token-budget truncation and message eviction,
- binary-vs-text decision **by file extension** (it ignores the `encoding` field
  `fs/read` returns),
- the tool descriptions shown to the model.

Therefore those concerns are **not** `daiv-sandbox`'s job — re-implementing them
server-side would double them up. `daiv-sandbox`'s job is to return correct,
robust **raw primitives**. "Can it be improved?" reduces to: where are the raw
primitives weaker than deepagents' own `BaseSandbox` returns?

Error-message convention in deepagents: the sandbox layer returns terse **codes**;
the client maps codes to model-facing sentences. Where `daiv-sandbox` owns the
file content (e.g. during `edit`), it is the right place to produce the helpful
message, because the daiv client surfaces `resp.error` verbatim.

## Already at parity (no change)

Empty-file sentinel string (exact match), literal grep (`grep -rHnF`),
`offset`/`limit` read with `limit=2000` default, CRLF-aware edit matching,
`string_not_found` / `multiple_occurrences` error codes, parent-dir creation on
write, path confinement (stricter than deepagents — confined to `/workspace`).

## Explicitly out of scope (verified non-gaps)

- **grep path-glob.** `BaseSandbox.grep` uses GNU `grep --include=GLOB`, which
  matches the **basename** only — exactly what the current basename `fnmatch`
  does. Path-aware globbing exists only in deepagents' _local_ `FilesystemBackend`
  (`wcmatch` + `GLOBSTAR`), which is beyond the sandbox contract.
- **ls/glob `size` + `modified_at` metadata.** deepagents `FileInfo` carries them,
  but the daiv client maps results to `path`/`is_dir` only, so they would be dead
  weight on the wire.
- **read server-side windowing.** Considered (deepagents `BaseSandbox` reads only
  the requested window via a server-side script). Deliberately **not** adopted:
  we keep the simple full-file `get_archive` read and only bound the response. See
  the tradeoff note under change #1.

## Changes

All four changes are Python-free (content via the Docker archive API; probes via
POSIX `[ -e ]`), consistent with the existing `fs/*` design.

### 1. `fs_read` — output byte-cap

No new method. `read_file_bytes` (full `get_archive` read) is unchanged.

- New module constant `READ_MAX_OUTPUT_BYTES = 512_000` (matches deepagents'
  `MAX_OUTPUT_BYTES`/`MAX_BINARY_BYTES`).
- **Text:** after `"\n".join(page)`, if the UTF-8 encoding exceeds the cap,
  truncate to `cap - len(marker)` (byte-safe, `decode(..., errors="ignore")`) and
  append the truncation marker, so the total stays within the cap (deepagents'
  `effective_limit` approach). The marker explains how to continue (read with a
  larger `offset` or smaller `limit`).
- **Binary:** if the raw bytes exceed the cap, return
  `error="Binary file exceeds maximum preview size of 512000 bytes"` (deepagents
  wording) instead of an unbounded base64 blob; otherwise base64 as today.
- The existing "Line offset N exceeds file length" behaviour is unchanged.

**Accepted tradeoff:** the full file is still pulled into the web process via
`get_archive`; this bounds the _response payload_, not server RAM. This is the
deliberate simplification (chosen over server-side windowing).

### 2. `edit_file` — better errors

The match logic (try literal, then CRLF, then LF variants) is unchanged. On zero
matches, before raising `string_not_found`, detect the common EOF-newline
mismatch (port of deepagents' `perform_string_replacement`):

- if `old` ends with `\n`, `len(old) > 1`, and the file content with its trailing
  newline removed ends with `old.removesuffix("\n")`:
  - if the stripped key appears exactly once → raise a `ValueError` instructing
    the caller to retry with the trailing newline removed from `old` (and from
    `new` if it also ends with one);
  - if the stripped key is ambiguous (appears more than once) → raise a
    `ValueError` instructing the caller to drop the trailing newline **and** add
    surrounding context for a unique match.
- otherwise keep `string_not_found`.

Also: include the occurrence count in the multiple-occurrences error
(e.g. `"String appears 3 times in file. Use replace_all=True ..."`) since the
count is already computed.

These messages are returned via the existing `FsEditResponse.error` field and
surfaced verbatim by the daiv client.

### 3. `find_paths` / `fs_glob` — deterministic order

Sort glob matches lexicographically by path before returning, matching
`BaseSandbox.glob`'s `sorted(...)`. This makes client-side truncation stable
(no arbitrary files dropped due to `find` traversal order).

### 4. `write_file(create_only=False)` + `fs/write` create-only

- Add a gated `create_only: bool = False` parameter to `write_file`. When `True`,
  run a `[ -e path ]` existence probe first and, if the path exists, raise/return
  the conflict before writing. (TOCTOU window between probe and write is inherent
  and acknowledged, as in deepagents.)
- `fs/write` passes `create_only=True` and, on conflict, returns
  `FsWriteResponse(ok=False, error="Cannot write to {path} because it already exists. Read and then make an edit, or write to a new path.")`
  (deepagents' exact message).
- `edit_file`'s write-back keeps the default `create_only=False`, so edit
  continues to overwrite the file it just read.

**DAIV-side impact:** none required for correctness — the daiv client already
handles `not resp.ok` and surfaces the error to the model. Caveat to watch after
rollout: any flow that intentionally full-file-rewrites via `write` (rather than
`edit`) will now error and must switch to `edit` (or delete-then-write). Per
deepagents conventions, write is create-only, so this should not exist.

## Testing

Unit tests (extend `tests/unit_tests/test_main.py` and `test_sessions.py`,
mirroring existing style):

- text read over the cap → content truncated to the cap and ends with the marker;
- read under the cap → content unchanged (no marker);
- binary over the cap → `Binary file exceeds maximum preview size` error;
  binary under the cap → base64 content as today;
- edit EOF-newline, unique stripped key → retry-without-newline guidance;
- edit EOF-newline, ambiguous stripped key → drop-newline-and-add-context guidance;
- edit multiple occurrences without `replace_all` → message includes the count;
- glob results returned in sorted order;
- `fs/write` to an existing path → conflict error with the exact message;
- `fs/write` to a new path → succeeds;
- `edit_file` write-back still overwrites an existing file (regression guard for
  the gated `create_only`).

Validate with `make test` (all tests pass). Update `CHANGELOG.md` per the existing
conventions.

## Out-of-band follow-ups (not in this change)

- Optional: tidy daiv client `awrite` to surface the conflict message without the
  redundant `Failed to write file 'X': ` prefix.
- Optional (future): if server RAM under huge reads becomes a real concern,
  revisit server-side windowing.
