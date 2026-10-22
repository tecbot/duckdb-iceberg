"""Run duckdb-go v2.20000.0 against an isolated REST catalog and selected extensions."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.mock_rest_catalog.server import MockServer

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--extensions", type=Path, required=True)
parser.add_argument("--fork", action="store_true")
args = parser.parse_args()
with tempfile.TemporaryDirectory() as directory, MockServer(Path(directory)) as server:
    result = subprocess.run(
        ["go", "test", "-v", "-count=1", "."],
        cwd=Path(__file__).parent,
        env={
            **os.environ,
            "ICEBERG_TEST_URI": server.uri,
            "ICEBERG_TEST_EXTENSIONS": str(args.extensions.resolve()),
            "ICEBERG_TEST_FORK": "1" if args.fork else "0",
        },
    )
    assert not server.errors, server.errors
    raise SystemExit(result.returncode)
