"""Check a read-then-write conflict through three connections to one database.

Use commit.retry.num-retries=0 to observe the catalog assertion before append
restaging. With --append-retries, also demonstrate the upstream append policy:
the stale request is rejected, then a regenerated append commits without rerunning
its SELECT. The test uses an isolated local HTTP catalog and the DuckDB C API.
"""

import argparse
import ctypes as c
import json
from pathlib import Path
import sys
import tempfile

from manifest_io_errors import API

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.mock_rest_catalog.server import MockServer


def run(args):
    with tempfile.TemporaryDirectory() as directory, MockServer(Path(directory)) as server:
        api = API(args.duckdb_library)
        connections = [api.connection]
        try:
            for name in ("httpfs", "avro", "iceberg"):
                path = getattr(args, name + "_extension")
                load = str(path.resolve()).replace("'", "''") if path else name
                status, error, _, _ = api.query("LOAD '" + load + "'")
                assert status == 0, error
            for _ in range(3):
                handle = c.c_void_p()
                assert api.lib.duckdb_connect(api.database, c.byref(handle)) == 0
                connections.append(handle)

            def query(connection, sql, expected=None):
                api.connection = connections[connection]
                status, error, _, rows = api.query(sql)
                assert status == 0, error
                if expected is not None:
                    assert rows == expected, (sql, rows, expected)

            retries = 4 if args.append_retries else 0
            endpoint_option = "ENDPOINT" if args.legacy_catalog else "URI"
            query(
                0,
                f"ATTACH '' AS c(TYPE ICEBERG, {endpoint_option} '{server.uri}', "
                "AUTHORIZATION_TYPE 'none', ACCESS_DELEGATION_MODE 'none', STAGE_CREATE_TABLES true); "
                "CREATE SCHEMA c.ns; "
                f"CREATE TABLE c.ns.t(a INTEGER, b INTEGER) WITH ('commit.retry.num-retries'='{retries}'); "
                "INSERT INTO c.ns.t VALUES(0,0)",
            )
            query(1, "BEGIN")
            query(1, "SELECT count(*) FROM c.ns.t WHERE a=7", [0])
            base = server.catalog.tables[(("ns",), "t")]["metadata"]["current-snapshot-id"]
            query(2, "INSERT INTO c.ns.t VALUES(7,7)")
            query(3, "SELECT count(*) FROM c.ns.t WHERE a=7", [1])
            query(1, "SELECT count(*) FROM c.ns.t WHERE a=7", [0])
            query(1, "INSERT INTO c.ns.t SELECT 7,7 WHERE NOT EXISTS (SELECT 1 FROM c.ns.t WHERE a=7)")
            api.connection = connections[1]
            status, error, _, _ = api.query("COMMIT")
            if args.append_retries:
                assert status == 0, error
            else:
                assert status != 0 and "409" in error, error
            query(3, "SELECT count(*) FROM c.ns.t WHERE a=7", [2 if args.append_retries else 1])
            events = [json.loads(line) for line in server.journal.read_text().splitlines()]
            rejected = [event for event in events if event["status"] == 409]
            assert len(rejected) == 1, rejected
            changes = rejected[0]["request"].get("table-changes", [rejected[0]["request"]])
            assertions = [
                requirement["snapshot-id"]
                for change in changes
                for requirement in change["requirements"]
                if requirement["type"] == "assert-ref-snapshot-id"
            ]
            assert assertions == [base], assertions
            assert not server.errors, server.errors
            print(
                "PASS: stale read base asserted and rejected; "
                + (
                    "append restaging commits the precomputed row (2 rows)"
                    if args.append_retries
                    else "409 reaches caller (1 row)"
                )
            )
        finally:
            for handle in connections[1:]:
                api.lib.duckdb_disconnect(c.byref(handle))
            api.connection = connections[0]
            api.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-library", type=Path, required=True)
    for name in ("httpfs", "avro", "iceberg"):
        parser.add_argument("--" + name + "-extension", type=Path)
    parser.add_argument("--legacy-catalog", action="store_true")
    parser.add_argument("--append-retries", action="store_true")
    run(parser.parse_args())
