from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from zipfile import ZipFile


with tempfile.TemporaryDirectory(prefix="ossie-trino-wheel-") as directory:
    destination = Path(directory)
    subprocess.run(
        [sys.executable, "-m", "pip", "wheel", ".", "--no-deps", "--wheel-dir", str(destination)],
        check=True,
    )
    wheels = list(destination.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one wheel, found {len(wheels)}")
    with ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
    required = {
        "ossie_trino/_schema.py",
        "ossie_trino/ossie-schema-0.3.json",
    }
    missing = required - names
    if missing:
        raise SystemExit(f"wheel is missing package data: {sorted(missing)}")
    forbidden = [name for name in names if name.startswith("ossie_starrocks/") or "/_starrocks" in name]
    if forbidden:
        raise SystemExit(f"wheel contains opposite-target modules: {sorted(forbidden)}")
