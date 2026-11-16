# Apache Ossie semantic compiler and gateway for Trino

`ossie-trino` compiles Apache Ossie semantic requests into Trino `SELECT` statements. It includes a constrained Trino protocol client and an authenticated semantic HTTP gateway. It does not load DuckDB or database extensions.

The compiler fails closed on unsafe fan-out traversal, ambiguous join paths, mixed metric grains, unknown references, and unsupported expressions. This is an early implementation of the evolving Apache Ossie specification, not a claim of full conformance.

## Install

Python 3.11 or later is required.

```sh
python3 -m pip install -e .
```

Install the optional YAML dependency when models use YAML.

```sh
python3 -m pip install -e '.[yaml]'
```

## TPCH quickstart

The included model and request use Trino's built-in TPCH tiny catalog.

```sh
docker compose -f examples/trino-tpch/compose.yaml up -d

until docker compose -f examples/trino-tpch/compose.yaml exec -T trino \
  trino --execute 'SELECT count(*) FROM tpch.tiny.orders' >/dev/null 2>&1; do
  sleep 2
done

ossie-trino query \
  --model examples/trino-tpch/model.json \
  --request examples/trino-tpch/request.json \
  --trino-url http://localhost:8080 \
  --user analyst
```

The result includes rows, column metadata, generated SQL, the Trino query ID, and audit metadata with exact model and request SHA-256 fingerprints.

## Compile a semantic request

```python
from ossie_trino import Compiler, Query

compiler = Compiler.from_file("sales.json")
result = compiler.compile(Query(
    metrics=("revenue",),
    dimensions=("customers.country",),
    filter="orders.status = 'paid' OR orders.status = 'settled'",
))

print(result.sql)
```

The public compiler has no target selector. Every result is Trino SQL.

```sh
ossie-trino compile --model sales.json --request request.json
```

The request file uses the Ossie Layer 3 shape.

```json
{
  "metrics": ["revenue"],
  "dimensions": ["customers.country"],
  "filter": "orders.status = 'paid' OR orders.status = 'settled'",
  "having": [{"metric": "revenue", "op": "gt", "value": 100}],
  "limit": 100
}
```

## Semantic gateway

The gateway fixes one model at startup and accepts only semantic request JSON. It does not accept arbitrary SQL.

```sh
export OSSIE_GATEWAY_TOKEN='replace-with-a-secret'

ossie-gateway \
  --model examples/trino-tpch/model.json \
  --trino-url http://localhost:8080 \
  --user ossie-gateway
```

```sh
curl --fail-with-body http://127.0.0.1:8081/v1/query \
  -H 'Authorization: Bearer replace-with-a-secret' \
  -H 'Content-Type: application/json' \
  --data-binary @examples/trino-tpch/request.json
```

`POST /v1/query` requires a Bearer token and enforces request-body, execution-time, retry, and result-row limits. `GET /healthz` exposes the loaded model identity. Put TLS and an identity-aware proxy in front of the service before exposing it beyond a trusted host.

## Safety model

- Models are validated against the bundled Apache Ossie 0.3 draft schema.
- The planner follows declared many-to-one relationships and rejects unsafe reverse fan-out.
- Different metric grains are aggregated independently and stitched with null-safe joins.
- Layer 3 filters are parsed into an owned AST. Raw query SQL is not accepted.
- The Trino client accepts only compiler-style `SELECT` and `WITH`, follows same-origin pagination, retries bounded transient responses, and cancels results that exceed the configured row cap.
- Aggregate division casts the dividend to `DOUBLE` to preserve fractional semantics. This has the usual binary floating-point and 2^53 integer-precision boundary.

Static compilation relies on model key declarations. Validate that source data satisfies declared primary and unique keys before production use.

## Verification

```sh
./scripts/verify.sh
python3 scripts/verify_wheel.py
```

The `test` workflow runs on Python 3.11, 3.12, and 3.13. The manually triggered `live-trino` workflow starts the official Trino 476 container and compares generated queries with hand-written TPCH baselines. It also verifies the CLI, protocol client, result cap, timeout, audit metadata, and authenticated gateway.

The Taiwan Traditional Chinese research report is available at [docs/research/trino-ossie.md](docs/research/trino-ossie.md).

## License

Apache License 2.0. See [LICENSE](LICENSE).
