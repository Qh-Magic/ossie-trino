from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import Mock, patch

from ossie_trino import TrinoColumn, TrinoResult
from ossie_trino.cli import main
from tests.fixtures import sales_model


class CliTests(unittest.TestCase):
    def test_cli_compiles_and_executes_trino_query(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_path = root / "model.json"
            request_path = root / "request.json"
            model_path.write_text(json.dumps(sales_model()), encoding="utf-8")
            request_path.write_text(json.dumps({"metrics": ["revenue"]}), encoding="utf-8")
            expected_model_sha256 = hashlib.sha256(model_path.read_bytes()).hexdigest()
            expected_request_sha256 = hashlib.sha256(request_path.read_bytes()).hexdigest()
            client = Mock()
            client.execute.return_value = TrinoResult(
                query_id="query-7",
                columns=(TrinoColumn("m0", "decimal(38,2)"),),
                rows=(("255.00",),),
            )
            output = StringIO()
            with patch("ossie_trino.cli.TrinoClient", return_value=client), redirect_stdout(output):
                exit_code = main([
                    "query",
                    "--model", str(model_path),
                    "--request", str(request_path),
                    "--trino-url", "http://trino.test:8080",
                    "--user", "analyst",
                ])

        payload = json.loads(output.getvalue())
        self.assertEqual(0, exit_code)
        self.assertEqual("query-7", payload["queryId"])
        self.assertEqual([["255.00"]], payload["rows"])
        self.assertEqual(["orders"], payload["dependencies"])
        self.assertIn('SUM(t0."amount")', payload["sql"])
        self.assertEqual("sales", payload["audit"]["modelName"])
        self.assertEqual("0.2.0.dev0", payload["audit"]["modelVersion"])
        self.assertEqual("analyst", payload["audit"]["trinoUser"])
        self.assertEqual(
            expected_model_sha256,
            payload["audit"]["modelSha256"],
        )
        self.assertEqual(
            expected_request_sha256,
            payload["audit"]["requestSha256"],
        )
        client.execute.assert_called_once()

    def test_cli_matches_library_surface(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_path = root / "model.json"
            request_path = root / "request.json"
            model_path.write_text(json.dumps(sales_model()), encoding="utf-8")
            request_path.write_text(
                json.dumps({"metrics": ["revenue"], "dimensions": ["customers.country"]}),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ossie_trino.cli",
                    "compile",
                    "--model",
                    str(model_path),
                    "--request",
                    str(request_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("LEFT JOIN \"default_catalog\".\"sales\".\"customers\"", completed.stdout)
        self.assertEqual("", completed.stderr)

    def test_cli_returns_diagnostic_for_invalid_operator_type(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_path = root / "model.json"
            request_path = root / "request.json"
            model_path.write_text(json.dumps(sales_model()), encoding="utf-8")
            request_path.write_text(
                json.dumps({
                    "metrics": ["revenue"],
                    "where": [{"field": "orders.status", "op": ["eq"], "value": "paid"}],
                }),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            completed = subprocess.run(
                [sys.executable, "-m", "ossie_trino.cli", "compile", "--model", str(model_path), "--request", str(request_path)],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
        self.assertEqual(2, completed.returncode)
        self.assertEqual("", completed.stdout)
        self.assertIn('"code": "INVALID_FILTER"', completed.stderr)

    def test_cli_accepts_dimension_only_request_without_metrics_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_path = root / "model.json"
            request_path = root / "request.json"
            model_path.write_text(json.dumps(sales_model()), encoding="utf-8")
            request_path.write_text(
                json.dumps({"dimensions": ["customers.country"]}),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ossie_trino.cli",
                    "compile",
                    "--model",
                    str(model_path),
                    "--request",
                    str(request_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("GROUP BY t0.\"country\"", completed.stdout)
        self.assertEqual("", completed.stderr)

    def test_cli_accepts_layer3_filter_expression(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_path = root / "model.json"
            request_path = root / "request.json"
            model_path.write_text(json.dumps(sales_model()), encoding="utf-8")
            request_path.write_text(
                json.dumps({
                    "metrics": ["revenue"],
                    "filter": "orders.status = 'complete' OR orders.status = 'pending'",
                }),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ossie_trino.cli",
                    "compile",
                    "--model",
                    str(model_path),
                    "--request",
                    str(request_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn("WHERE ((t0.\"status\" = 'complete') OR (t0.\"status\" = 'pending'))", completed.stdout)
        self.assertEqual("", completed.stderr)

    def test_cli_emits_trino_sql(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_path = root / "model.json"
            request_path = root / "request.json"
            model_path.write_text(json.dumps(sales_model()), encoding="utf-8")
            request_path.write_text(
                json.dumps({"metrics": ["revenue"], "dimensions": ["customers.country"]}),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "ossie_trino.cli",
                    "compile",
                    "--model",
                    str(model_path),
                    "--request",
                    str(request_path),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=environment,
            )
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn('LEFT JOIN "default_catalog"."sales"."customers"', completed.stdout)
        self.assertNotIn('`', completed.stdout)


if __name__ == "__main__":
    unittest.main()
