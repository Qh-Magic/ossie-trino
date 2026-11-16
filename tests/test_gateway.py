from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ossie_trino.compiler import Compiler
from ossie_trino.gateway import SemanticGateway, create_server
from ossie_trino.trino import TrinoColumn, TrinoResult


ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "examples/trino-tpch/model.json"


class FakeTrinoClient:
    def __init__(self):
        self.sql: str | None = None

    def execute(self, sql: str) -> TrinoResult:
        self.sql = sql
        return TrinoResult(
            query_id="query_123",
            columns=(TrinoColumn("customers.mktsegment", "varchar"),),
            rows=(("AUTOMOBILE",),),
        )


class GatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = FakeTrinoClient()
        self.compiler = Compiler.from_file(MODEL)
        self.gateway = SemanticGateway(
            self.compiler,
            self.client,  # type: ignore[arg-type]
            trino_user="gateway-user",
            bearer_token="test-secret",
        )

    def test_authenticated_query_returns_result_and_audit(self) -> None:
        body = b'{"metrics":["revenue"],"dimensions":["customers.mktsegment"],"limit":1}'
        status, payload = self.gateway.query(body, "Bearer test-secret")

        self.assertEqual(status, 200)
        self.assertEqual(payload["queryId"], "query_123")
        self.assertEqual(payload["audit"]["requestSha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(payload["audit"]["trinoUser"], "gateway-user")
        self.assertEqual(payload["audit"]["modelName"], "trino_tpch_quickstart")
        self.assertEqual(payload["sql"], self.client.sql)

    def test_invalid_token_is_rejected_before_execution(self) -> None:
        status, payload = self.gateway.query(b"{}", "Bearer wrong")

        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "UNAUTHORIZED")
        self.assertIsNone(self.client.sql)

    def test_http_boundary_and_health_endpoint(self) -> None:
        server = create_server("127.0.0.1", 0, self.gateway, max_body_bytes=256)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with urlopen(f"{base}/healthz") as response:
                health = json.load(response)
            self.assertEqual(health["model"]["name"], "trino_tpch_quickstart")

            request = Request(
                f"{base}/v1/query",
                data=b'{"metrics":["revenue"],"limit":1}',
                headers={"Authorization": "Bearer test-secret", "Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(request) as response:
                payload = json.load(response)
            self.assertEqual(payload["queryId"], "query_123")

            oversized = Request(
                f"{base}/v1/query",
                data=b"x" * 257,
                headers={"Authorization": "Bearer test-secret", "Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as caught:
                urlopen(oversized)
            self.assertEqual(caught.exception.code, 413)
            caught.exception.read()
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
