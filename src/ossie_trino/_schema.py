from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from typing import Any

from jsonschema import Draft202012Validator


@lru_cache(maxsize=1)
def _validator() -> Draft202012Validator:
    schema_path = files("ossie_trino").joinpath("ossie-schema-0.3.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_03_document(document: dict[str, Any]) -> tuple[str, str] | None:
    errors = sorted(_validator().iter_errors(document), key=lambda error: tuple(str(part) for part in error.absolute_path))
    if not errors:
        return None
    error = errors[0]
    location = "$"
    for part in error.absolute_path:
        location += f"[{part}]" if isinstance(part, int) else f".{part}"
    return location, error.message
