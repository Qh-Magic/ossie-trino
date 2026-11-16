from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from .compiler import CompilationError, Compiler
from .execution import execution_payload
from .request import query_from_mapping
from .trino import TrinoClient, TrinoError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ossie-trino")
    commands = parser.add_subparsers(dest="command", required=True)
    compile_parser = commands.add_parser("compile")
    _add_semantic_inputs(compile_parser)
    query_parser = commands.add_parser("query", help="compile and execute a semantic request on Trino")
    _add_semantic_inputs(query_parser)
    query_parser.add_argument("--trino-url", required=True)
    query_parser.add_argument("--user", required=True)
    query_parser.add_argument("--catalog")
    query_parser.add_argument("--schema")
    query_parser.add_argument("--timeout", type=float, default=30.0)
    query_parser.add_argument("--query-timeout-seconds", type=float, default=300.0)
    query_parser.add_argument("--retries", type=int, default=3)
    query_parser.add_argument("--max-rows", type=int, default=10_000)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        query = _read_query(arguments.request)
        compiler = Compiler.from_file(arguments.model)
        result = compiler.compile(query)
        if arguments.command == "query":
            executed = TrinoClient(
                arguments.trino_url,
                arguments.user,
                catalog=arguments.catalog,
                schema=arguments.schema,
                timeout=arguments.timeout,
                query_timeout=arguments.query_timeout_seconds,
                max_retries=arguments.retries,
                max_rows=arguments.max_rows,
            ).execute(result.sql)
    except (CompilationError, OSError, json.JSONDecodeError, ValueError, TrinoError) as error:
        if isinstance(error, CompilationError):
            print(json.dumps({"code": error.code, "location": error.location, "message": error.message}), file=sys.stderr)
            return 2
        if isinstance(error, TrinoError):
            print(
                json.dumps({"code": error.code, "queryId": error.query_id, "message": error.message}),
                file=sys.stderr,
            )
            return 3
        else:
            print(json.dumps({"code": "INVALID_INPUT", "location": "$", "message": str(error)}), file=sys.stderr)
        return 2
    if arguments.command == "compile":
        print(result.sql)
    else:
        print(json.dumps(execution_payload(
            compiler,
            result,
            executed,
            trino_user=arguments.user,
            request_sha256=_file_sha256(arguments.request),
        )))
    return 0


def _add_semantic_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", required=True)
    parser.add_argument("--request", required=True)


def _file_sha256(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_query(path: str):
    request = json.loads(Path(path).read_text(encoding="utf-8"))
    return query_from_mapping(request)


if __name__ == "__main__":
    raise SystemExit(main())
