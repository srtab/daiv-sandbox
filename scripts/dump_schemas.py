"""Emit the daiv-sandbox wire schemas as a single sorted JSON document.

Used by the daiv repo's ``test_schema_consistency.py`` to detect drift between
the two sides. To refresh the dump:

    cd ~/work/personal/daiv-sandbox
    uv run --all-extras python scripts/dump_schemas.py \\
        > ~/work/personal/daiv/daiv/core/sandbox/schemas.dump.json
"""

import json

from daiv_sandbox.schemas import (
    ApplyMutationsRequest,
    ApplyMutationsResponse,
    MutationResult,
    PutMutation,
    RunRequest,
    RunResponse,
    RunResult,
    SeedSessionRequest,
    StartSessionRequest,
    StartSessionResponse,
)

_TYPES = [
    ApplyMutationsRequest,
    ApplyMutationsResponse,
    MutationResult,
    PutMutation,
    RunRequest,
    RunResponse,
    RunResult,
    SeedSessionRequest,
    StartSessionRequest,
    StartSessionResponse,
]


def main() -> None:
    import sys

    schemas = {cls.__name__: cls.model_json_schema() for cls in _TYPES}
    sys.stdout.write(json.dumps(schemas, indent=2, sort_keys=True))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
