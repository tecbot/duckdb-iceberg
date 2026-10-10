> **Disclaimer:** This extension is currently in an experimental state. Feel free to try it out, but be aware that things may not work as expected

# DuckDB extension for Apache Iceberg 

This repository contains a DuckDB extension that adds support for [Apache Iceberg](https://iceberg.apache.org/). In its current state, the extension offers some basics features that allow listing snapshots and reading specific snapshots
of an iceberg tables.

## Custom snapshot summary properties

This compatibility branch targets DuckDB v1.5.6.

Use `iceberg_snapshot_properties` to attach application data to each new snapshot:

```sql
SET SESSION iceberg_snapshot_properties = '{"application.transaction-id":"txn-001"}';
INSERT INTO my_catalog.my_schema.my_table VALUES (42);
RESET iceberg_snapshot_properties;
```

The setting belongs to one connection. Its default is `'{}'`. Each `SET` replaces the complete map. `RESET` clears the map. `SET GLOBAL` is rejected. `COMMIT` and `ROLLBACK` do not clear the setting. If you use a connection pool, reset the setting before you return the connection to the pool, including after an error.

The value must be a JSON object with string values. SQL `NULL`, invalid JSON, other JSON types, non-string values, and duplicate keys are rejected. Names are case-sensitive. For example, `Tag` and `tag` are distinct keys. The extension reserves `operation` and all keys in its snapshot metric registry, including metrics that are absent from the current write. A failed `SET` leaves the previous value unchanged.

Each write copies the current properties when it registers a pending snapshot during execution. A prepared statement uses the setting at `EXECUTE`. Later `SET` or `RESET` statements do not change pending snapshots. Two writes in one transaction can use different maps. DuckDB v1.5.6 returns commit conflicts to the application. For an application retry, set the original properties again before you execute the new write.

The properties and generated metrics are published in the same snapshot summary. They apply to `INSERT`, `UPDATE`, `DELETE`, `MERGE`, and `CREATE TABLE AS`. They also apply to `COPY ... (FORMAT ICEBERG)`. COPY publishes its table during finalization, so a later SQL rollback does not remove it. Reads, metadata-only operations, and writes that create no snapshot do not publish properties. New snapshots do not inherit custom properties from their parent.

These properties do not provide automatic deduplication. If a commit result is unknown, the application must inspect snapshot history before it decides to repeat a write.

## Documentation

See the [Iceberg page in the DuckDB documentation](https://duckdb.org/docs/extensions/iceberg).

## Developer guide

### Dependencies

This extension has several dependencies. Currently, the main way to install them is through vcpkg. To install vcpkg, 
check out the docs [here](https://vcpkg.io/en/getting-started.html). Note that this extension contains a custom vcpkg port
that overrides the existing 'avro-cpp' port of vcpkg. The reason for this is that the other versions of avro-cpp have
some issue that seems to cause issues with the avro files produced by the spark iceberg extension.

### Test data generation

To generate test data, the script in 'scripts/test_data_generator' is used to have spark generate some test data. This is 
based on pyspark 3.5, which you can install through pip. 

### Building the extension

To build the extension with vcpkg, you can build this extension using:

```shell
VCPKG_TOOLCHAIN_PATH='<path_to_your_vcpkg_repo>/scripts/buildsystems/vcpkg.cmake' make
```

This will build both the separate loadable extension and a duckdb binary with the extension pre-loaded:
```shell
./build/release/duckdb
./build/release/extension/iceberg/iceberg.duckdb_extension
```

### Running tests

#### Generating test data

To generate the test data, run: 
```shell
make data
```

**Note** that the script requires python3, pyspark and duckdb-python to be installed. Make sure that the correct versions for pyspark (3.5.0), java and scala (2.12) are installed.

running `python3 -m pip install duckdb "pyspark[sql]==3.5.0"` should do the trick.

#### Running unit tests

```shell
make test 
```

#### Running the local S3 test server

Running the S3 test cases requires the minio test server to be running and populated with `scripts/upload_iceberg_to_s3_test_server.sh`.
Note that this requires to have run `make data` before and also to have the aws cli and docker compose installed.

### Local catalog setup

The Makefile provides targets to spin up local Iceberg catalogs for development and testing. Each target clones the catalog repo (if needed) and starts the service:

```shell
make fixture      # Apache Iceberg REST Fixture (Docker)
make nessie       # Nessie catalog (Docker)
make lakekeeper   # Lakekeeper catalog (Docker)
make polaris      # Apache Polaris catalog (Gradle/local)
```

For starting the service AND generating data (to run tests that need it):

```shell
make fixture-data   
make nessie-data    
make lakekeeper-data
make polaris-data   
```

Should you need to generate data for only one test (a test found under *scripts/data_generators/tests*), you can pass the test name as an argument, like so: `TEST=all_types_table make fixture-data`. The script will now only generate the needed data for that single test, which is faster.

**Prerequisites:** Docker and Docker Compose are required for Fixture, Nessie, and Lakekeeper. Polaris requires Java/Gradle and builds from source — the build is skipped automatically if it has already completed. To force a clean rebuild of Polaris, run `make polaris-rebuild`.

Fixture also has a local variant that generates data for local file-based testing instead of REST:

```shell
make fixture-data-local
```

## Acknowledgements

This extension was initially developed as part of a customer project for [RelationalAI](https://relational.ai/),
who have agreed to open source the extension. We would like to thank RelationalAI for their support
and their commitment to open source enabling us to share this extension with the community.
