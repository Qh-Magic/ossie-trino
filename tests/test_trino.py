from __future__ import annotations

from io import BytesIO
import json
from email.message import Message
import unittest
from urllib.error import HTTPError

from ossie_trino import TrinoClient, TrinoError


class _Response(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class _Open:
    def __init__(self, *pages: dict | Exception):
        self.pages = list(pages)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return _Response(json.dumps(page).encode("utf-8"))


class TrinoClientTests(unittest.TestCase):
    def test_executes_statement_and_collects_paginated_rows(self):
        open_request = _Open(
            {
                "id": "query-1",
                "columns": [{"name": "d0", "type": "varchar"}],
                "data": [["TW"]],
                "nextUri": "http://trino.test:8080/v1/statement/query-1/1",
            },
            {"id": "query-1", "data": [["JP"]]},
        )
        client = TrinoClient(
            "http://trino.test:8080",
            "analyst",
            timeout=7,
            _open=open_request,
        )

        result = client.execute('SELECT "country" FROM "customers"')

        self.assertEqual("query-1", result.query_id)
        self.assertEqual(("d0", "varchar"), (result.columns[0].name, result.columns[0].type))
        self.assertEqual((("TW",), ("JP",)), result.rows)
        first, second = open_request.requests
        self.assertEqual("POST", first[0].method)
        self.assertEqual(b'SELECT "country" FROM "customers"', first[0].data)
        self.assertEqual("analyst", first[0].get_header("X-trino-user"))
        self.assertEqual("300s", first[0].get_header("X-trino-session").split("=")[1])
        self.assertEqual("GET", second[0].method)
        self.assertEqual(7, second[1])

    def test_surfaces_trino_query_error_with_query_id(self):
        client = TrinoClient(
            "http://trino.test:8080",
            "analyst",
            _open=_Open({
                "id": "query-2",
                "error": {"errorName": "SYNTAX_ERROR", "message": "bad query"},
            }),
        )

        with self.assertRaises(TrinoError) as raised:
            client.execute("SELECT")

        self.assertEqual("SYNTAX_ERROR", raised.exception.code)
        self.assertEqual("query-2", raised.exception.query_id)
        self.assertEqual("bad query", raised.exception.message)

    def test_rejects_cross_origin_next_uri(self):
        client = TrinoClient(
            "https://trino.test",
            "analyst",
            _open=_Open({
                "id": "query-3",
                "nextUri": "https://attacker.test/steal",
            }),
        )

        with self.assertRaisesRegex(TrinoError, "configured Trino origin"):
            client.execute("SELECT 1")

    def test_rejects_rows_that_do_not_match_columns(self):
        client = TrinoClient(
            "http://trino.test:8080",
            "analyst",
            _open=_Open({
                "id": "query-4",
                "columns": [{"name": "a", "type": "bigint"}],
                "data": [[1, 2]],
            }),
        )

        with self.assertRaisesRegex(TrinoError, "row width"):
            client.execute("SELECT 1")

    def test_rejects_invalid_connection_options(self):
        with self.assertRaisesRegex(ValueError, "absolute http"):
            TrinoClient("file:///tmp/trino", "analyst")
        with self.assertRaisesRegex(ValueError, "single-line"):
            TrinoClient("http://trino.test", "analyst\nX-Trino-Role: admin")
        with self.assertRaisesRegex(ValueError, "positive finite"):
            TrinoClient("http://trino.test", "analyst", timeout=0)
        with self.assertRaisesRegex(ValueError, "query_timeout"):
            TrinoClient("http://trino.test", "analyst", query_timeout=float("inf"))
        with self.assertRaisesRegex(ValueError, "positive integer"):
            TrinoClient("http://trino.test", "analyst", max_rows=0)

    def test_rejects_non_query_statements(self):
        client = TrinoClient("http://trino.test", "analyst", _open=_Open())

        with self.assertRaisesRegex(ValueError, "only compiler-style"):
            client.execute("DELETE FROM orders")

    def test_retries_transient_http_failures(self):
        headers = Message()
        headers["Retry-After"] = "0.2"
        open_request = _Open(
            HTTPError("http://trino.test", 503, "unavailable", {}, None),
            HTTPError("http://trino.test", 429, "busy", headers, None),
            {"id": "query-5", "columns": [{"name": "x", "type": "bigint"}], "data": [[1]]},
        )
        delays = []
        client = TrinoClient(
            "http://trino.test",
            "analyst",
            _open=open_request,
            _sleep=delays.append,
        )

        result = client.execute("SELECT 1")

        self.assertEqual(((1,),), result.rows)
        self.assertEqual([0.075, 0.2], delays)
        self.assertEqual(3, len(open_request.requests))

    def test_stops_after_configured_retry_limit(self):
        failure = HTTPError("http://trino.test", 502, "gateway", {}, None)
        client = TrinoClient(
            "http://trino.test",
            "analyst",
            max_retries=1,
            _open=_Open(failure, failure),
            _sleep=lambda _: None,
        )

        with self.assertRaisesRegex(TrinoError, "HTTP 502"):
            client.execute("SELECT 1")

    def test_cancels_query_when_result_limit_is_exceeded(self):
        open_request = _Open(
            {
                "id": "query-6",
                "columns": [{"name": "x", "type": "bigint"}],
                "data": [[1], [2]],
                "nextUri": "http://trino.test/v1/statement/query-6/1",
            },
            {},
        )
        client = TrinoClient(
            "http://trino.test",
            "analyst",
            max_rows=1,
            _open=open_request,
        )

        with self.assertRaises(TrinoError) as raised:
            client.execute("SELECT x FROM numbers")

        self.assertEqual("TRINO_RESULT_LIMIT", raised.exception.code)
        self.assertEqual("query-6", raised.exception.query_id)
        self.assertEqual("DELETE", open_request.requests[1][0].method)

    def test_formats_subsecond_query_timeout_for_trino(self):
        open_request = _Open({"id": "query-7", "data": [[1]]})
        client = TrinoClient(
            "http://trino.test",
            "analyst",
            query_timeout=0.0011,
            _open=open_request,
        )

        client.execute("SELECT 1")

        self.assertEqual(
            "query_max_run_time=2ms",
            open_request.requests[0][0].get_header("X-trino-session"),
        )


if __name__ == "__main__":
    unittest.main()
