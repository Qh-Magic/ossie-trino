from __future__ import annotations

import re

from ._model import BoundModel
from ._portable_sql import render_query as render_portable_query
from .api import CompilationError, CompiledQuery
from .planning import MultiStageQueryPlan, QueryPlan


_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TOKEN_REWRITES = {
    "DATETIME": "TIMESTAMP",
    "DAYOFYEAR": "DAY_OF_YEAR",
    "IFNULL": "COALESCE",
}
_STRING_EDGE_FUNCTIONS = frozenset({"LEFT", "RIGHT"})


def render_query(
    model: BoundModel,
    plan: QueryPlan | MultiStageQueryPlan,
) -> CompiledQuery:
    """Render a proven semantic plan as ordinary Trino SQL.

    Planning is dialect-independent. The private base renderer first produces
    the compiler-owned SQL subset, then this tokenizer rewrites only identifiers
    and known dialect tokens. String literals are never rewritten.
    """

    compiled = render_portable_query(model, plan)
    return CompiledQuery(
        sql=_to_trino_sql(compiled.sql),
        columns=compiled.columns,
        dependencies=compiled.dependencies,
    )


def _to_trino_sql(sql: str) -> str:
    output: list[str] = []
    position = 0
    while position < len(sql):
        character = sql[position]
        if character == "'":
            end = _quoted_end(sql, position, "'")
            output.append(sql[position:end])
            position = end
            continue
        if character == "`":
            end = _quoted_end(sql, position, "`")
            identifier = sql[position + 1 : end - 1].replace("``", "`")
            output.append('"' + identifier.replace('"', '""') + '"')
            position = end
            continue
        match = _TOKEN.match(sql, position)
        if match is None:
            output.append(character)
            position += 1
            continue
        token = match.group(0)
        upper = token.upper()
        next_position = match.end()
        call_start = _next_non_space_position(sql, next_position)
        if upper == "DIVIDE" and call_start < len(sql) and sql[call_start] == "(":
            call_end = _parenthesized_end(sql, call_start)
            arguments = _split_arguments(sql[call_start + 1 : call_end - 1])
            if len(arguments) != 2:
                raise CompilationError("INTERNAL_ERROR", "$.sql", "invalid DIVIDE call")
            dividend = _to_trino_sql(arguments[0])
            divisor = _to_trino_sql(arguments[1])
            output.append(f"(CAST({dividend} AS DOUBLE) / {divisor})")
            position = call_end
            continue
        if upper == "LOCATE" and call_start < len(sql) and sql[call_start] == "(":
            call_end = _parenthesized_end(sql, call_start)
            arguments = _split_arguments(sql[call_start + 1 : call_end - 1])
            if len(arguments) != 2:
                raise CompilationError("INTERNAL_ERROR", "$.sql", "invalid LOCATE call")
            substring = _to_trino_sql(arguments[0])
            string = _to_trino_sql(arguments[1])
            output.append(f"STRPOS({string}, {substring})")
            position = call_end
            continue
        if upper in _STRING_EDGE_FUNCTIONS and call_start < len(sql) and sql[call_start] == "(":
            call_end = _parenthesized_end(sql, call_start)
            arguments = _split_arguments(sql[call_start + 1 : call_end - 1])
            if len(arguments) != 2:
                raise CompilationError("INTERNAL_ERROR", "$.sql", f"invalid {upper} call")
            string = _to_trino_sql(arguments[0])
            width = _to_trino_sql(arguments[1])
            if upper == "LEFT":
                output.append(f"SUBSTRING({string}, 1, {width})")
            else:
                output.append(f"SUBSTRING({string}, -({width}))")
            position = call_end
            continue
        output.append(_TOKEN_REWRITES.get(upper, token))
        position = next_position
    return "".join(output)


def _quoted_end(sql: str, start: int, quote: str) -> int:
    position = start + 1
    while position < len(sql):
        if sql[position] != quote:
            position += 1
            continue
        if position + 1 < len(sql) and sql[position + 1] == quote:
            position += 2
            continue
        return position + 1
    raise CompilationError("INTERNAL_ERROR", "$.sql", "renderer emitted an unterminated quote")


def _next_non_space_position(sql: str, position: int) -> int:
    while position < len(sql) and sql[position].isspace():
        position += 1
    return position


def _parenthesized_end(sql: str, start: int) -> int:
    depth = 0
    position = start
    while position < len(sql):
        if sql[position] in {"'", "`"}:
            position = _quoted_end(sql, position, sql[position])
            continue
        if sql[position] == "(":
            depth += 1
        elif sql[position] == ")":
            depth -= 1
            if depth == 0:
                return position + 1
        position += 1
    raise CompilationError("INTERNAL_ERROR", "$.sql", "renderer emitted an unterminated call")


def _split_arguments(arguments: str) -> tuple[str, ...]:
    parts: list[str] = []
    start = 0
    depth = 0
    position = 0
    while position < len(arguments):
        if arguments[position] in {"'", "`"}:
            position = _quoted_end(arguments, position, arguments[position])
            continue
        if arguments[position] == "(":
            depth += 1
        elif arguments[position] == ")":
            depth -= 1
        elif arguments[position] == "," and depth == 0:
            parts.append(arguments[start:position].strip())
            start = position + 1
        position += 1
    parts.append(arguments[start:].strip())
    return tuple(parts)
