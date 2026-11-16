from __future__ import annotations

import argparse
import difflib
from pathlib import Path
import re
import tomllib


FILES = tuple(
    line for line in (Path(__file__).resolve().parents[1] / "shared-core-files.txt").read_text().splitlines()
    if line
)


def package_name(root: Path) -> str:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    return project["name"].replace("-", "_")


def normalized(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if path.name == "_schema.py":
        text = re.sub(r'files\("ossie_(?:starrocks|trino)"\)', 'files("ossie_target")', text)
    return text.splitlines(keepends=True)


parser = argparse.ArgumentParser()
parser.add_argument("other", type=Path)
arguments = parser.parse_args()
root = Path(__file__).resolve().parents[1]
other = arguments.other.resolve()
left_package = package_name(root)
right_package = package_name(other)

failed = False
for relative in FILES:
    left = root / "src" / left_package / relative
    right = other / "src" / right_package / relative
    difference = list(difflib.unified_diff(normalized(left), normalized(right), fromfile=str(left), tofile=str(right)))
    if difference:
        failed = True
        print("".join(difference), end="")
if failed:
    raise SystemExit("shared semantic core differs")
print(f"shared semantic core matches {other}")
