from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from ._model import BoundModel, ModelError, load_document
from ._trino import render_query
from .api import CompilationError, CompiledQuery, FieldFilter, MetricFilter, ModelIdentity, OutputColumn, Query
from .planning import plan_query

class Compiler:
    def __init__(self, model: BoundModel, *, model_sha256: str):
        self._model = model
        self._model_identity = ModelIdentity(model.name, model.version, model_sha256)

    @property
    def model_identity(self) -> ModelIdentity:
        return self._model_identity

    @classmethod
    def from_file(cls, path: str | Path) -> "Compiler":
        source = Path(path)
        format = "yaml" if source.suffix.lower() in {".yaml", ".yml"} else "json"
        content = source.read_bytes()
        return cls._from_document(
            content.decode("utf-8"),
            format=format,
            model_sha256=hashlib.sha256(content).hexdigest(),
        )

    @classmethod
    def from_text(cls, document: str, *, format: Literal["json", "yaml"]) -> "Compiler":
        return cls._from_document(
            document,
            format=format,
            model_sha256=hashlib.sha256(document.encode("utf-8")).hexdigest(),
        )

    @classmethod
    def _from_document(
        cls,
        document: str,
        *,
        format: Literal["json", "yaml"],
        model_sha256: str,
    ) -> "Compiler":
        try:
            return cls(
                load_document(document, format),
                model_sha256=model_sha256,
            )
        except ModelError as error:
            raise CompilationError(error.code, error.location, str(error)) from error

    def compile(self, query: Query) -> CompiledQuery:
        plan = plan_query(self._model, query)
        return render_query(self._model, plan)

__all__ = ["CompilationError", "CompiledQuery", "Compiler", "FieldFilter", "MetricFilter", "ModelIdentity", "OutputColumn", "Query"]
