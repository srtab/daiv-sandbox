"""Emit the daiv-sandbox wire schemas as a single sorted JSON document.

Used by the daiv repo's ``test_schema_consistency.py`` to detect drift between
the two sides. To refresh the dump:

    cd ~/work/personal/daiv-sandbox
    uv run --all-extras python scripts/dump_schemas.py \\
        > ~/work/personal/daiv/daiv/core/sandbox/schemas.dump.json
"""

import json

from daiv_sandbox.schemas import (
    EgressConfigRequest,
    EgressConfigResponse,
    EgressPolicy,
    EgressRule,
    EgressSecret,
    FsDeleteRequest,
    FsDeleteResponse,
    FsEditRequest,
    FsEditResponse,
    FsEntry,
    FsGlobRequest,
    FsGlobResponse,
    FsGrepMatch,
    FsGrepRequest,
    FsGrepResponse,
    FsLsRequest,
    FsLsResponse,
    FsReadRequest,
    FsReadResponse,
    FsWriteRequest,
    FsWriteResponse,
    RunRequest,
    RunResponse,
    RunResult,
    StartSessionRequest,
    StartSessionResponse,
)

_TYPES = [
    RunRequest,
    RunResponse,
    RunResult,
    StartSessionRequest,
    StartSessionResponse,
    FsDeleteRequest,
    FsDeleteResponse,
    FsEditRequest,
    FsEditResponse,
    FsEntry,
    FsGlobRequest,
    FsGlobResponse,
    FsGrepMatch,
    FsGrepRequest,
    FsGrepResponse,
    FsLsRequest,
    FsLsResponse,
    FsReadRequest,
    FsReadResponse,
    FsWriteRequest,
    FsWriteResponse,
    EgressRule,
    EgressSecret,
    EgressPolicy,
    EgressConfigRequest,
    EgressConfigResponse,
]


def main() -> None:
    import sys

    schemas = {cls.__name__: cls.model_json_schema() for cls in _TYPES}
    sys.stdout.write(json.dumps(schemas, indent=2, sort_keys=True))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
