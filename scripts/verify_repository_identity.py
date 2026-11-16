from __future__ import annotations

import inspect
from pathlib import Path
import tomllib

from ossie_trino import Compiler


ROOT = Path(__file__).resolve().parents[1]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
require(project["name"] == "ossie-trino", "distribution must be ossie-trino")
require(set(project["scripts"]) == {"ossie-trino", "ossie-gateway"}, "unexpected console scripts")
require("target" not in inspect.signature(Compiler.compile).parameters, "Compiler.compile must be single-target")

for required in (
    "src/ossie_trino/_portable_sql.py",
    "src/ossie_trino/_trino.py",
    "src/ossie_trino/trino.py",
    "src/ossie_trino/gateway.py",
    ".github/workflows/live-trino.yml",
):
    require((ROOT / required).exists(), f"required Trino artifact is missing: {required}")

for forbidden in (
    "src/ossie_trino/_starrocks.py",
    "src/ossie_starrocks",
    ".github/workflows/live-starrocks.yml",
    "scripts/live_starrocks.py",
):
    require(not (ROOT / forbidden).exists(), f"opposite-target artifact must be absent: {forbidden}")
