from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).parents[1]


class ExampleTests(unittest.TestCase):
    def test_trino_tpch_quickstart_compiles(self):
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "ossie_trino.cli",
                "compile",
                "--model",
                str(ROOT / "examples/trino-tpch/model.json"),
                "--request",
                str(ROOT / "examples/trino-tpch/request.json"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertIn('FROM "tpch"."tiny"."orders" AS t0', completed.stdout)
        self.assertIn('LEFT JOIN "tpch"."tiny"."customer" AS t1', completed.stdout)
        self.assertIn('GROUP BY t1."mktsegment"', completed.stdout)
        self.assertIn("LIMIT 10", completed.stdout)

    def test_trino_tpch_files_are_json_objects(self):
        for name in ("model.json", "request.json"):
            value = json.loads((ROOT / "examples/trino-tpch" / name).read_text(encoding="utf-8"))
            self.assertIsInstance(value, dict)


if __name__ == "__main__":
    unittest.main()
