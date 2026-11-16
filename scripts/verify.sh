#!/bin/sh
set -eu

PYTHONPATH=src python3 -m compileall -q src tests
PYTHONPATH=src python3 scripts/verify_repository_identity.py
PYTHONPATH=src python3 -m unittest discover -s tests -v
