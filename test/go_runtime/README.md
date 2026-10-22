# DuckDB Go runtime checks

Run these tests with Go 1.24 or later and Python 3.14. The module pins
`github.com/duckdb/duckdb-go/v2 v2.20000.0`, which bundles DuckDB 2.0.0.

```sh
(cd test/go_runtime && go mod download)
python test/go_runtime/run.py --extensions /absolute/path/to/extensions --fork
```

The extension directory contains canonical `httpfs.duckdb_extension`,
`avro.duckdb_extension`, and `iceberg.duckdb_extension` files for the host platform
and DuckDB 2.0.0. Use the official HTTPFS file and the fork release's Avro and
Iceberg files. Omit `--fork` when testing the official Iceberg alternative.

The runner starts a temporary loopback REST catalog and removes its local
warehouse on exit. It does not connect to an external service. The tests compare
read-base conflicts with and without append retries. The fork run also checks
captured properties on a zero-row INSERT snapshot. See the
[v12 assessment](../../docs/releases/v12.md) for the observed outcomes and limits.
