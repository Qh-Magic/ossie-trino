from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

Scalar = str | int | Decimal | bool | date | datetime | None
Operator = Literal["eq", "ne", "lt", "le", "gt", "ge", "is_null", "is_not_null"]

class CompilationError(Exception):
    def __init__(self, code: str, location: str, message: str, related: tuple[str, ...] = ()):
        super().__init__(message)
        self.code, self.location, self.message, self.related = code, location, message, related

@dataclass(frozen=True)
class FieldFilter:
    field: str
    op: Operator
    value: Scalar = None

@dataclass(frozen=True)
class MetricFilter:
    metric: str
    op: Operator
    value: Scalar = None

@dataclass(frozen=True)
class Query:
    metrics: tuple[str, ...] = ()
    dimensions: tuple[str, ...] = ()
    where: tuple[FieldFilter, ...] = ()
    having: tuple[MetricFilter, ...] = ()
    limit: int | None = None
    filter: str | None = None

@dataclass(frozen=True)
class OutputColumn:
    semantic_name: str
    sql_alias: str

@dataclass(frozen=True)
class CompiledQuery:
    sql: str
    columns: tuple[OutputColumn, ...]
    dependencies: tuple[str, ...]

@dataclass(frozen=True)
class ModelIdentity:
    name: str
    version: str
    sha256: str
