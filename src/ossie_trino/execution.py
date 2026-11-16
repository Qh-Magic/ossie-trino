from __future__ import annotations

from .api import CompiledQuery
from .compiler import Compiler
from .trino import TrinoResult


def execution_payload(
    compiler: Compiler,
    compiled: CompiledQuery,
    executed: TrinoResult,
    *,
    trino_user: str,
    request_sha256: str,
) -> dict:
    identity = compiler.model_identity
    return {
        "queryId": executed.query_id,
        "columns": [{"name": column.name, "type": column.type} for column in executed.columns],
        "rows": executed.rows,
        "sql": compiled.sql,
        "dependencies": compiled.dependencies,
        "audit": {
            "modelName": identity.name,
            "modelVersion": identity.version,
            "modelSha256": identity.sha256,
            "requestSha256": request_sha256,
            "target": "trino",
            "trinoUser": trino_user,
            "queryId": executed.query_id,
            "dependencies": compiled.dependencies,
            "sql": compiled.sql,
        },
    }
