> [!WARNING]
> This extension is experimental. APIs and behavior may change, and some catalog-specific features are not yet supported.

# DuckDB extension for Apache Iceberg

This repository contains DuckDB's Apache Iceberg extension. It adds support for reading and writing Iceberg tables, inspecting Iceberg metadata, and attaching Iceberg REST catalogs.

User-facing documentation is available on the [Iceberg extension page](https://duckdb.org/docs/extensions/iceberg).

## Custom snapshot summary properties

Use `iceberg_snapshot_properties` to attach application data to each new snapshot:

```sql
SET SESSION iceberg_snapshot_properties = '{"application.transaction-id":"txn-001"}';
INSERT INTO my_catalog.my_schema.my_table VALUES (42);
RESET iceberg_snapshot_properties;
```

The setting belongs to one connection. Its default is `'{}'`. Each `SET` replaces the complete map. `RESET` clears the map. `SET GLOBAL` is rejected. `COMMIT` and `ROLLBACK` do not clear the setting. If you use a connection pool, reset the setting before you return the connection to the pool, including after an error.

The value must be a JSON object with string values. SQL `NULL`, invalid JSON, other JSON types, non-string values, and duplicate keys are rejected. Names are case-sensitive. For example, `Tag` and `tag` are distinct keys. The extension reserves `operation` and all keys in its snapshot metric registry, including metrics that are absent from the current write. A failed `SET` leaves the previous value unchanged.

Each write copies the current properties when it registers a pending snapshot during execution. A prepared statement uses the setting at `EXECUTE`. Later `SET` or `RESET` statements do not change pending snapshots. Two writes in one transaction can use different maps. A commit retry uses the original copy.

The properties and generated metrics are published in the same snapshot summary. They apply to `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `CREATE TABLE AS`, and maintenance actions that create snapshots. They also apply to `COPY ... (FORMAT ICEBERG)`. COPY publishes its table during finalization, so a later SQL rollback does not remove it. Reads, metadata-only operations, and writes that create no snapshot do not publish properties. New snapshots do not inherit custom properties from their parent.

These properties do not provide automatic deduplication. If a commit result is unknown, the application must inspect snapshot history before it decides to repeat a write.

## Development setup

Clone the repository with its DuckDB and extension tooling submodules:

```shell
git clone --recurse-submodules https://github.com/duckdb/duckdb-iceberg.git
cd duckdb-iceberg
```

If the repository is already cloned, initialize the submodules with:

```shell
git submodule update --init --recursive
```

### Prerequisites

Building requires a C++ toolchain, CMake, and [vcpkg](https://vcpkg.io/en/getting-started.html). The repository supplies its own vcpkg manifest and ports; pass the vcpkg toolchain file when building.

Catalog-backed tests and test-data generation have additional requirements:

- Python 3 and the packages in `scripts/requirements.txt`
- Java compatible with the selected PySpark runtime
- Docker and Docker Compose for the local REST catalogs
- Git for catalog repositories cloned under `.catalogs/`

The catalog data targets create `.venv-spark4` and install the pinned Python requirements automatically. `make install_requirements` installs those requirements into the currently active Python environment instead.

## Building

Set the vcpkg toolchain path for each invocation or export it in your shell:

```shell
export VCPKG_TOOLCHAIN_PATH=/path/to/vcpkg/scripts/buildsystems/vcpkg.cmake
make debug
```

Common build targets are:

```shell
make debug       # Debug build with assertions and sanitizers
make release     # Optimized release build; also the default `make` target
make reldebug    # Optimized build with debug information
make relassert   # Optimized build with assertions
```

For a release build, the resulting DuckDB shell and loadable extension are:

```text
build/release/duckdb
build/release/extension/iceberg/iceberg.duckdb_extension
```

The top-level `Makefile` delegates standard build, test, and formatting targets to `extension-ci-tools/makefiles/duckdb_extension.Makefile`. It builds Iceberg together with the required test extensions configured in `extension_config.cmake`.

## Running tests

Build the matching configuration before running its tests. The default test target uses the release test binary:

> [!CAUTION]
> Tests are not assumed to be isolated from one another. Catalog-backed tests in particular can mutate shared catalogs, warehouses, schemas, tables, and generated files. Do not run test files or suites concurrently unless you have verified that they use independent resources and cannot affect each other. This includes separate unittest processes, pytest workers such as `pytest-xdist`, and overlapping local or CI jobs.

```shell
make release
make test
```

Other configurations can be tested explicitly:

```shell
make debug
make test_debug

make reldebug
make test_reldebug
```

Use `T` to narrow a Make-based run, or invoke the sqllogictest runner directly:

```shell
make test_debug T=test/sql/local/iceberg_scans/

./build/debug/test/unittest \
  test/sql/local/iceberg_scans/iceberg_scan.test
```

Run formatting checks and fixes with:

```shell
make format-check
make format-fix
```

## Local REST catalogs

The catalog targets start local services, generate compatible Iceberg test data, and record the selected catalog in `.catalogs/.active_catalog`.

| Catalog | Start services | Start and generate data | Notes |
| --- | --- | --- | --- |
| Apache Iceberg REST fixture | `make fixture` | `make fixture-data` | Uses the Compose setup in `scripts/` |
| Apache Iceberg REST fixture (latest) | `make fixture-latest` | `make fixture-latest-data` | Tracks `apache/iceberg-rest-fixture:latest` in a separate compatibility lane |
| Apache Gravitino | `make gravitino` | `make gravitino-data` | Uses the standalone Gravitino REST server and SeaweedFS Compose setup in `scripts/gravitino/`; may add `seaweedfs` to `/etc/hosts` with `sudo` |
| Lakekeeper | `make lakekeeper` | `make lakekeeper-data` | Clones a pinned revision, applies the repository patch, and may add `seaweedfs` to `/etc/hosts` with `sudo` |
| Nessie | `make nessie` | `make nessie-data` | Uses Nessie's `catalog-auth-s3` Compose setup |
| Apache Polaris | `make polaris` | `make polaris-data` | Clones the `release/1.4.x` branch and uses its RustFS quickstart |
| Test-only v2/v3 mock | `make mock` | Not required | Python standard library and local warehouse; [SQL testing and skip policy](scripts/mock_rest_catalog/README.md) |

Starting a catalog stops the catalog currently named in `.catalogs/.active_catalog`. To stop one explicitly, use `make <catalog>-stop`. Catalog clones, runtime state, and generated data are kept in ignored directories.

To generate only the data needed by one registered generator, pass a pytest `-k` expression through `TEST`:

```shell
TEST=all_types_table make fixture-data
```

The fixture also supports generation for local file-based tests:

```shell
make fixture-data-local
```

### Catalog-backed sqllogictests

Each REST catalog has a config in `test/configs/`. A config initializes credentials and the `my_datalake` attachment, statically loads required extensions, sets `CATALOG_TEST_CONFIG_SETUP`, and lists catalog-specific skips where necessary.

After starting a catalog, resolve its config from `.catalogs/.active_catalog` by running the helper directly:

```shell
./build/debug/test/unittest --order lex \
  "$PWD/test/sql/local/catalog_test_config_setup/*" \
  --test-config "$(scripts/catalog_test_config.sh)"
```

To run one file:

```shell
./build/debug/test/unittest \
  --test-config "$(scripts/catalog_test_config.sh)" \
  test/sql/local/catalog_test_config_setup/catalog_agnostic/create/test_create_table.test
```

`scripts/catalog_test_config.sh` accepts `fixture`, `fixture-latest`, `gravitino`, `lakekeeper`, `nessie`, `polaris`, or `mock`. It reports an error for a missing, empty, local-only, or unknown active-catalog marker.

For the mock, run the catalog-agnostic SQL suite excluding `.test_slow`, with capability flags and skips controlled by `test/configs/mock.json`. You can pass that config directly after `make mock`; see the [mock catalog instructions](scripts/mock_rest_catalog/README.md). It has no Spark generator or Python integration profile. Stop it with `make mock-stop`.

### Catalog-backed Python tests

The Python integration tests also use the active catalog and require a built unittest binary:

```shell
python3 -m pytest -vv test/python \
  --unittest-binary ./build/debug/test/unittest \
  --test-python-verbosity verbose
```

Verbose mode prints the resolved catalog and Spark runtime and explains capability-based skips. Cloud-only tests under `test/python/cloud/` are excluded unless that directory is requested explicitly.

## Test data generation

The generator under `scripts/data_generators/` uses PySpark and Iceberg to populate the active catalog. Generator cases live under `scripts/data_generators/tests/`, and intermediate results are written below `data/generated/intermediates/`.

See [scripts/data_generators/README.md](scripts/data_generators/README.md) for instructions on adding generator cases, catalog connections, skips, and expected failures.

## Acknowledgements

This extension was initially developed as part of a customer project for [RelationalAI](https://relational.ai/), who agreed to open source it. We thank RelationalAI for supporting open source development and enabling us to share the extension with the community.
