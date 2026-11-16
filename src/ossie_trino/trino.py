from __future__ import annotations

from dataclasses import dataclass
import json
import math
import time
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


class TrinoError(Exception):
    def __init__(self, code: str, message: str, query_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.query_id = query_id


@dataclass(frozen=True)
class TrinoColumn:
    name: str
    type: str


@dataclass(frozen=True)
class TrinoResult:
    query_id: str
    columns: tuple[TrinoColumn, ...]
    rows: tuple[tuple[object, ...], ...]


class TrinoClient:
    def __init__(
        self,
        url: str,
        user: str,
        *,
        source: str = "ossie-trino",
        catalog: str | None = None,
        schema: str | None = None,
        timeout: float = 30.0,
        query_timeout: float = 300.0,
        max_retries: int = 3,
        max_rows: int = 10_000,
        _open: Callable[..., object] = urlopen,
        _sleep: Callable[[float], None] = time.sleep,
    ):
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            raise ValueError("Trino URL must be an absolute http or https URL")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        if not math.isfinite(query_timeout) or query_timeout <= 0:
            raise ValueError("query_timeout must be a positive finite number")
        if not isinstance(max_retries, int) or isinstance(max_retries, bool) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        if not isinstance(max_rows, int) or isinstance(max_rows, bool) or max_rows <= 0:
            raise ValueError("max_rows must be a positive integer")
        self._origin = (parts.scheme.lower(), parts.netloc.lower())
        self._statement_url = urljoin(url.rstrip("/") + "/", "v1/statement")
        self._headers = {
            "Content-Type": "text/plain; charset=utf-8",
            "X-Trino-User": self._header("user", user),
            "X-Trino-Source": self._header("source", source),
            "X-Trino-Session": f"query_max_run_time={self._duration(query_timeout)}",
        }
        if catalog is not None:
            self._headers["X-Trino-Catalog"] = self._header("catalog", catalog)
        if schema is not None:
            self._headers["X-Trino-Schema"] = self._header("schema", schema)
        self._timeout = timeout
        self._max_retries = max_retries
        self._max_rows = max_rows
        self._open = _open
        self._sleep = _sleep

    def execute(self, sql: str) -> TrinoResult:
        statement = sql.lstrip()
        if not statement:
            raise ValueError("SQL must not be empty")
        if statement.split(None, 1)[0].upper() not in {"SELECT", "WITH"}:
            raise ValueError("TrinoClient executes only compiler-style SELECT or WITH statements")
        page = self._request(self._statement_url, sql.encode("utf-8"))
        query_id = self._query_id(page)
        columns: tuple[TrinoColumn, ...] = ()
        rows: list[tuple[object, ...]] = []

        while True:
            page_query_id = page.get("id")
            if page_query_id is not None and page_query_id != query_id:
                raise TrinoError("TRINO_PROTOCOL_ERROR", "query id changed between pages", query_id)
            self._raise_query_error(page, query_id)
            if "columns" in page:
                page_columns = self._columns(page["columns"], query_id)
                if columns and page_columns != columns:
                    raise TrinoError("TRINO_PROTOCOL_ERROR", "column metadata changed between pages", query_id)
                columns = page_columns
            rows.extend(self._rows(page.get("data", []), query_id))
            next_uri = page.get("nextUri")
            if next_uri is not None and (
                not isinstance(next_uri, str) or self._origin_of(next_uri) != self._origin
            ):
                raise TrinoError("TRINO_PROTOCOL_ERROR", "nextUri must use the configured Trino origin", query_id)
            if len(rows) > self._max_rows:
                if isinstance(next_uri, str):
                    self._cancel(next_uri)
                raise TrinoError(
                    "TRINO_RESULT_LIMIT",
                    f"Trino result exceeded the {self._max_rows} row limit",
                    query_id,
                )
            if next_uri is None:
                break
            page = self._request(next_uri)

        if columns and any(len(row) != len(columns) for row in rows):
            raise TrinoError("TRINO_PROTOCOL_ERROR", "row width does not match column metadata", query_id)
        return TrinoResult(query_id=query_id, columns=columns, rows=tuple(rows))

    def _request(self, url: str, body: bytes | None = None) -> dict:
        request = Request(url, data=body, headers=self._headers, method="POST" if body is not None else "GET")
        for attempt in range(self._max_retries + 1):
            try:
                with self._open(request, timeout=self._timeout) as response:
                    payload = json.load(response)
                break
            except HTTPError as error:
                if error.code not in {429, 502, 503, 504} or attempt == self._max_retries:
                    raise TrinoError("TRINO_HTTP_ERROR", f"Trino returned HTTP {error.code}") from error
                self._sleep(self._retry_delay(error))
            except (URLError, OSError) as error:
                raise TrinoError("TRINO_CONNECTION_ERROR", str(error)) from error
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise TrinoError("TRINO_PROTOCOL_ERROR", "Trino returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise TrinoError("TRINO_PROTOCOL_ERROR", "Trino response must be a JSON object")
        return payload

    def _cancel(self, next_uri: str) -> None:
        request = Request(next_uri, headers=self._headers, method="DELETE")
        try:
            with self._open(request, timeout=self._timeout):
                pass
        except (HTTPError, URLError, OSError):
            pass

    @staticmethod
    def _retry_delay(error: HTTPError) -> float:
        if error.code == 429:
            try:
                requested = float(error.headers.get("Retry-After", "0.1"))
            except (TypeError, ValueError):
                requested = 0.1
            if math.isfinite(requested):
                return min(max(requested, 0.05), 5.0)
        return 0.075

    @staticmethod
    def _query_id(page: dict) -> str:
        query_id = page.get("id")
        if not isinstance(query_id, str) or not query_id:
            raise TrinoError("TRINO_PROTOCOL_ERROR", "Trino response is missing a query id")
        return query_id

    @staticmethod
    def _raise_query_error(page: dict, query_id: str) -> None:
        error = page.get("error")
        if error is None:
            return
        if not isinstance(error, dict):
            raise TrinoError("TRINO_PROTOCOL_ERROR", "invalid Trino error payload", query_id)
        name = error.get("errorName", "TRINO_QUERY_ERROR")
        message = error.get("message", "Trino query failed")
        raise TrinoError(str(name), str(message), query_id)

    @staticmethod
    def _columns(value: object, query_id: str) -> tuple[TrinoColumn, ...]:
        if not isinstance(value, list):
            raise TrinoError("TRINO_PROTOCOL_ERROR", "columns must be an array", query_id)
        columns = []
        for item in value:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("type"), str):
                raise TrinoError("TRINO_PROTOCOL_ERROR", "invalid column metadata", query_id)
            columns.append(TrinoColumn(item["name"], item["type"]))
        return tuple(columns)

    @staticmethod
    def _rows(value: object, query_id: str) -> tuple[tuple[object, ...], ...]:
        if not isinstance(value, list) or any(not isinstance(row, list) for row in value):
            raise TrinoError("TRINO_PROTOCOL_ERROR", "data must be an array of rows", query_id)
        return tuple(tuple(row) for row in value)

    @staticmethod
    def _origin_of(url: str) -> tuple[str, str]:
        parts = urlsplit(url)
        return parts.scheme.lower(), parts.netloc.lower()

    @staticmethod
    def _header(name: str, value: str) -> str:
        if not value or any(character in value for character in "\r\n"):
            raise ValueError(f"Trino {name} must be a non-empty single-line value")
        return value

    @staticmethod
    def _duration(seconds: float) -> str:
        milliseconds = max(1, math.ceil(seconds * 1000))
        if milliseconds % 1000 == 0:
            return f"{milliseconds // 1000}s"
        return f"{milliseconds}ms"
