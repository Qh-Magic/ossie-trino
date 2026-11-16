from __future__ import annotations

import argparse
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from socketserver import TCPServer
import sys

from .api import CompilationError
from .compiler import Compiler
from .execution import execution_payload
from .request import query_from_mapping
from .trino import TrinoClient, TrinoError


class SemanticGateway:
    def __init__(self, compiler: Compiler, client: TrinoClient, *, trino_user: str, bearer_token: str):
        if not bearer_token:
            raise ValueError("bearer token must not be empty")
        self._compiler = compiler
        self._client = client
        self._trino_user = trino_user
        self._bearer_token = bearer_token

    def health(self) -> dict:
        identity = self._compiler.model_identity
        return {
            "status": "ok",
            "model": {"name": identity.name, "version": identity.version, "sha256": identity.sha256},
        }

    def query(self, body: bytes, authorization: str | None) -> tuple[int, dict]:
        expected = f"Bearer {self._bearer_token}"
        if authorization is None or not hmac.compare_digest(authorization, expected):
            return 401, {"code": "UNAUTHORIZED", "message": "valid bearer token required"}
        try:
            document = json.loads(body.decode("utf-8"))
            request = query_from_mapping(document)
            compiled = self._compiler.compile(request)
            executed = self._client.execute(compiled.sql)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return 400, {"code": "INVALID_JSON", "location": "$", "message": str(error)}
        except CompilationError as error:
            return 400, {"code": error.code, "location": error.location, "message": error.message}
        except TrinoError as error:
            status = 504 if error.code == "EXCEEDED_TIME_LIMIT" else 422 if error.code == "TRINO_RESULT_LIMIT" else 502
            return status, {"code": error.code, "queryId": error.query_id, "message": error.message}
        except ValueError as error:
            return 400, {"code": "INVALID_REQUEST", "location": "$", "message": str(error)}
        return 200, execution_payload(
            self._compiler,
            compiled,
            executed,
            trino_user=self._trino_user,
            request_sha256=hashlib.sha256(body).hexdigest(),
        )


def create_server(
    host: str,
    port: int,
    gateway: SemanticGateway,
    *,
    max_body_bytes: int = 1_048_576,
) -> ThreadingHTTPServer:
    if max_body_bytes <= 0:
        raise ValueError("max_body_bytes must be positive")

    class Handler(BaseHTTPRequestHandler):
        server_version = "OssieGateway/0.1"

        def do_GET(self) -> None:
            if self.path != "/healthz":
                self._send(404, {"code": "NOT_FOUND", "message": "route not found"})
                return
            self._send(200, gateway.health())

        def do_POST(self) -> None:
            if self.path != "/v1/query":
                self._send(404, {"code": "NOT_FOUND", "message": "route not found"})
                return
            if self.headers.get_content_type() != "application/json":
                self._send(415, {"code": "UNSUPPORTED_MEDIA_TYPE", "message": "Content-Type must be application/json"})
                return
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else -1
            except ValueError:
                length = -1
            if length < 0:
                self._send(411, {"code": "LENGTH_REQUIRED", "message": "a valid Content-Length is required"})
                return
            if length > max_body_bytes:
                self._send(413, {"code": "REQUEST_TOO_LARGE", "message": "request body exceeds the configured limit"})
                return
            status, payload = gateway.query(self.rfile.read(length), self.headers.get("Authorization"))
            self._send(status, payload)

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    class GatewayHTTPServer(ThreadingHTTPServer):
        def server_bind(self) -> None:
            TCPServer.server_bind(self)
            self.server_name = str(self.server_address[0])
            self.server_port = int(self.server_address[1])

    return GatewayHTTPServer((host, port), Handler)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ossie-gateway")
    parser.add_argument("--model", required=True)
    parser.add_argument("--trino-url", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--catalog")
    parser.add_argument("--schema")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--token-env", default="OSSIE_GATEWAY_TOKEN")
    parser.add_argument("--max-body-bytes", type=int, default=1_048_576)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--query-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--max-rows", type=int, default=10_000)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    token = os.environ.get(arguments.token_env)
    if not token:
        print(
            json.dumps({"code": "INVALID_CONFIGURATION", "message": f"{arguments.token_env} must be set"}),
            file=sys.stderr,
        )
        return 2
    try:
        compiler = Compiler.from_file(arguments.model)
        client = TrinoClient(
            arguments.trino_url,
            arguments.user,
            catalog=arguments.catalog,
            schema=arguments.schema,
            timeout=arguments.timeout,
            query_timeout=arguments.query_timeout_seconds,
            max_retries=arguments.retries,
            max_rows=arguments.max_rows,
        )
        server = create_server(
            arguments.host,
            arguments.port,
            SemanticGateway(compiler, client, trino_user=arguments.user, bearer_token=token),
            max_body_bytes=arguments.max_body_bytes,
        )
    except (OSError, ValueError, json.JSONDecodeError, CompilationError) as error:
        print(json.dumps({"code": "INVALID_CONFIGURATION", "message": str(error)}), file=sys.stderr)
        return 2
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
