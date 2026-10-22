"""Compare an official or fork extension through the same DuckDB C API.

Uses an isolated HTTP catalog and local warehouse. Official builds report the
observed gaps without claiming they passed. --expect-fork requires every check.
"""

import argparse
import json
from pathlib import Path
import sys
import tempfile

from manifest_io_errors import API

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.mock_rest_catalog.server import MockServer


def run(args):
    results = {}
    with tempfile.TemporaryDirectory() as directory, MockServer(Path(directory)) as server:
        api = API(args.duckdb_library)
        try:

            def query(sql):
                status, error, _, rows = api.query(sql)
                assert status == 0, error
                return rows

            for name in ("httpfs", "avro", "iceberg"):
                path = getattr(args, name + "_extension")
                load = str(path.resolve()).replace("'", "''") if path else name
                query("LOAD '" + load + "'")
            query(
                f"ATTACH '' AS c(TYPE ICEBERG, URI '{server.uri}', AUTHORIZATION_TYPE 'none', "
                "ACCESS_DELEGATION_MODE 'none', STAGE_CREATE_TABLES true); CREATE SCHEMA c.ns; "
                "CREATE TABLE c.ns.compact(id INTEGER)"
            )
            for index in range(6):
                query(f"INSERT INTO c.ns.compact VALUES ({index * 2}), ({index * 2 + 1})")
            rewritten = query("CALL iceberg_rewrite_data_files('c.ns.compact')")
            results["compaction"] = rewritten == [6] and query("SELECT count(*) FROM c.ns.compact") == [12]
            query("CREATE TABLE c.ns.merged(id INTEGER) WITH ('commit.manifest.min-count-to-merge'='2')")
            for index in range(3):
                query(f"INSERT INTO c.ns.merged VALUES({index})")
            results["manifest_merging"] = query(
                "SELECT count(DISTINCT manifest_path) FROM iceberg_metadata('c.ns.merged')"
            ) == [2] and query("SELECT count(*) FROM c.ns.merged") == [3]
            results["refs"] = query("SELECT snapshot_id FROM iceberg_refs('c.ns.merged') WHERE name='main'") == [
                server.catalog.tables[(("ns",), "merged")]["metadata"]["current-snapshot-id"]
            ]
            query("CREATE TABLE c.ns.empty_insert(id INTEGER); INSERT INTO c.ns.empty_insert VALUES(1)")
            query("INSERT INTO c.ns.empty_insert SELECT 1 WHERE false")
            results["empty_insert_snapshot"] = query("SELECT count(*) FROM iceberg_snapshots('c.ns.empty_insert')") == [
                2
            ]
            status, _, _, _ = api.query("SET iceberg_snapshot_properties='{\"probe\":\"captured\"}'")
            results["snapshot_properties"] = status == 0
            if status == 0:
                query("INSERT INTO c.ns.empty_insert VALUES(2)")
                results["snapshot_properties"] = query(
                    "SELECT count(*) FROM iceberg_snapshots('c.ns.empty_insert') WHERE summary['probe']='captured'"
                ) == [1]
                query("RESET iceberg_snapshot_properties")
            path = "data/persistent/utf8_trunc_bounds"
            checks = {
                "utf8_equality": (f"SELECT id FROM iceberg_scan('{path}') WHERE value='bar'", [2]),
                "utf8_join": (f"SELECT a.id FROM iceberg_scan('{path}') a JOIN (SELECT 'baz' v) b ON a.value=b.v", [3]),
                "utf8_cte": (
                    f"WITH c AS MATERIALIZED (SELECT * FROM iceberg_scan('{path}')) SELECT id FROM c WHERE value='bar'",
                    [2],
                ),
                "utf8_not_null": (
                    f"SELECT id FROM iceberg_scan('{path}') WHERE value IS NOT NULL ORDER BY id",
                    [1, 2, 3],
                ),
                "utf8_top_n": (f"SELECT id FROM iceberg_scan('{path}') ORDER BY value LIMIT 1", [2]),
            }
            for name, (sql, expected) in checks.items():
                results[name] = query(sql) == expected
            assert not server.errors, server.errors
        finally:
            api.close()
    print(json.dumps(results, indent=2))
    if args.expect_fork:
        assert all(results.values()), results
    else:
        assert all(results[name] for name in ("compaction", "manifest_merging", "refs")), results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-library", type=Path, required=True)
    for name in ("httpfs", "avro", "iceberg"):
        parser.add_argument("--" + name + "-extension", type=Path)
    parser.add_argument("--expect-fork", action="store_true")
    run(parser.parse_args())
