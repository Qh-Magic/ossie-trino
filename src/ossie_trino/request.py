from __future__ import annotations

from .api import CompilationError, FieldFilter, MetricFilter, Query


def query_from_mapping(request: object) -> Query:
    if not isinstance(request, dict):
        raise CompilationError("INVALID_REQUEST", "$", "request must be an object")
    return Query(
        metrics=_strings(request.get("metrics", []), "$.metrics"),
        dimensions=_strings(request.get("dimensions", []), "$.dimensions"),
        filter=request.get("filter"),
        where=tuple(
            _field_filter(item, index)
            for index, item in enumerate(_objects(request.get("where", []), "$.where"))
        ),
        having=tuple(
            _metric_filter(item, index)
            for index, item in enumerate(_objects(request.get("having", []), "$.having"))
        ),
        limit=request.get("limit"),
    )


def _strings(value: object, location: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise CompilationError("INVALID_REQUEST", location, "expected an array of strings")
    return tuple(value)


def _objects(value: object, location: str) -> list[dict]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise CompilationError("INVALID_REQUEST", location, "expected an array of objects")
    return value


def _field_filter(item: dict, index: int) -> FieldFilter:
    try:
        return FieldFilter(item["field"], item["op"], item.get("value"))
    except KeyError as error:
        raise CompilationError("INVALID_REQUEST", f"$.where[{index}]", f"missing {error.args[0]}") from error


def _metric_filter(item: dict, index: int) -> MetricFilter:
    try:
        return MetricFilter(item["metric"], item["op"], item.get("value"))
    except KeyError as error:
        raise CompilationError("INVALID_REQUEST", f"$.having[{index}]", f"missing {error.args[0]}") from error
