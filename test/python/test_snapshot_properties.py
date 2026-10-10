"""Snapshot summary properties shared by DuckDB, PyIceberg, and Spark."""

from uuid import uuid4

import pytest

from duckdb_unittest import DuckDBUnittestRunner

pa = pytest.importorskip("pyarrow")
pytest.importorskip("pyiceberg")


@pytest.mark.requires_spark(">=3.5")
def test_duckdb_properties_read_by_other_engines(
    rest_catalog, spark_con, unittest_binary, unittest_test_config, print_unittest_stdin
):
    name = "duckdb_properties_" + uuid4().hex
    try:
        with DuckDBUnittestRunner(
            unittest_binary, test_config=unittest_test_config, print_stdin=print_unittest_stdin
        ) as test:
            test.statement_ok(
                "set iceberg_snapshot_properties = "
                "'{\"application.transaction-id\":\"txn-001\",\"Tag\":\"upper\",\"tag\":\"lower\",\"Operation\":\"custom\"}'"
            )
            test.statement_ok(f"create table my_datalake.default.{name} as select 42::integer as id")
            test.statement_ok("reset iceberg_snapshot_properties")

        table = rest_catalog.load_table(f"default.{name}")
        summary = table.current_snapshot().summary
        assert summary.operation.value == "append"
        assert summary.additional_properties["application.transaction-id"] == "txn-001"
        assert summary.additional_properties["Tag"] == "upper"
        assert summary.additional_properties["tag"] == "lower"
        assert summary.additional_properties["Operation"] == "custom"
        assert summary.additional_properties["total-records"] == "1"
        assert table.scan().to_arrow().to_pylist() == [{"id": 42}]

        rows = spark_con.sql(f"select operation, summary from default.{name}.snapshots").collect()
        assert len(rows) == 1
        assert rows[0].operation == "append"
        assert rows[0].summary["application.transaction-id"] == "txn-001"
        assert rows[0].summary["Tag"] == "upper"
        assert rows[0].summary["tag"] == "lower"
        assert rows[0].summary["Operation"] == "custom"
        assert rows[0].summary["total-records"] == "1"
        assert spark_con.sql(f"select id from default.{name}").first().id == 42
    finally:
        rest_catalog.drop_table(f"default.{name}")


def test_foreign_properties_survive_duckdb_metadata_copy(
    rest_catalog, unittest_binary, unittest_test_config, print_unittest_stdin
):
    name = "foreign_properties_" + uuid4().hex
    table = rest_catalog.create_table(f"default.{name}", schema=pa.schema([("id", pa.int32())]))
    try:
        table.append(
            pa.table({"id": pa.array([1], type=pa.int32())}),
            snapshot_properties={
                "application.transaction-id": "foreign",
                "Tag": "upper",
                "tag": "lower",
                "Operation": "custom",
                "Total-Records": "opaque",
            },
        )
        foreign_id = table.current_snapshot().snapshot_id
        with DuckDBUnittestRunner(
            unittest_binary, test_config=unittest_test_config, print_stdin=print_unittest_stdin
        ) as test:
            test.send("require json")
            test.statement_ok("begin")
            # ALTER copies the table metadata into the transaction's table state.
            test.statement_ok(f"alter table my_datalake.default.{name} add column payload varchar")
            test.statement_ok(f"insert into my_datalake.default.{name} (id) values (2)")
            test.statement_ok("commit")
            # Scan-plan metadata passes through the generated summary parser, internal model,
            # table metadata copies, and snapshot serialization.
            test.query(
                "TTTTTT",
                f"""
                select snap->'summary'->>'application.transaction-id',
                       snap->'summary'->>'Tag', snap->'summary'->>'tag',
                       snap->'summary'->>'Operation', snap->'summary'->>'Total-Records',
                       snap->'summary'->>'total-records'
                from (
                    select unnest(json_extract(metadata::JSON, '$.snapshots[*]')) as snap
                    from (select metadata from iceberg_scan_plan('my_datalake.default.{name}') limit 1)
                )
                where json_extract_string(snap, '$."snapshot-id"')::BIGINT = {foreign_id}
                """,
                [("foreign", "upper", "lower", "custom", "opaque", "1")],
            )
            test.query("I", f"select count(*) from my_datalake.default.{name}", [(2,)])

        table.refresh()
        assert len(table.snapshots()) == 2
        assert "application.transaction-id" not in table.current_snapshot().summary.additional_properties
        original = table.snapshot_by_id(foreign_id).summary.additional_properties
        assert original["application.transaction-id"] == "foreign"
        assert original["Tag"] == "upper"
        assert original["tag"] == "lower"
        assert original["Operation"] == "custom"
        assert original["Total-Records"] == "opaque"
    finally:
        rest_catalog.drop_table(f"default.{name}")


@pytest.mark.requires_spark(">=4.0")
@pytest.mark.parametrize(
    "format_version",
    [2, pytest.param(3, marks=pytest.mark.requires_capabilities("format_v3"))],
)
def test_empty_insert_matches_spark_append(
    format_version, rest_catalog, spark_con, unittest_binary, unittest_test_config, print_unittest_stdin
):
    name = "empty_append_" + uuid4().hex
    created = False
    try:
        with DuckDBUnittestRunner(
            unittest_binary, test_config=unittest_test_config, print_stdin=print_unittest_stdin
        ) as test:
            test.statement_ok("set session iceberg_use_metadata_log = false")
            test.statement_ok(
                f"create table my_datalake.default.{name} (id integer) " f"with ('format-version' = {format_version})"
            )
            test.statement_ok(f"insert into my_datalake.default.{name} select 0 where false")
            test.statement_ok(f"insert into my_datalake.default.{name} values (42)")
            test.statement_ok("set iceberg_snapshot_properties = '{\"application.transaction-id\":\"empty\"}'")
            test.statement_ok(f"insert into my_datalake.default.{name} select 0 where false")
            test.statement_ok("reset iceberg_snapshot_properties")
            test.query("I", f"select id from my_datalake.default.{name}", [(42,)])
        created = True

        table = rest_catalog.load_table(f"default.{name}")
        snapshots = table.snapshots()
        assert len(snapshots) == 3
        first, populated, empty = snapshots
        assert first.parent_snapshot_id is None
        assert empty.parent_snapshot_id == populated.snapshot_id
        assert empty.sequence_number == populated.sequence_number + 1
        assert len({snapshot.manifest_list for snapshot in snapshots}) == 3
        assert first.manifests(table.io) == []
        assert len(empty.manifests(table.io)) == 1
        assert empty.summary.operation.value == "append"
        assert empty.summary.additional_properties["application.transaction-id"] == "empty"
        assert empty.summary.additional_properties["total-records"] == "1"
        for key in ["added-records", "added-data-files"]:
            assert int(empty.summary.additional_properties.get(key, "0")) == 0
        assert table.scan().to_arrow().to_pylist() == [{"id": 42}]

        spark_rows = spark_con.sql(f"select id from default.{name}").collect()
        assert [row.id for row in spark_rows] == [42]
        spark_summary = (
            spark_con.sql(f"select summary from default.{name}.snapshots where snapshot_id = {empty.snapshot_id}")
            .first()
            .summary
        )
        assert spark_summary["application.transaction-id"] == "empty"

        # The reference writer must also publish an append when its input is empty.
        spark_con.sql(f"select id from default.{name} where false").writeTo(f"default.{name}").append()
        table.refresh()
        reference = table.current_snapshot()
        assert len(table.snapshots()) == 4
        assert reference.parent_snapshot_id == empty.snapshot_id
        assert reference.manifest_list != empty.manifest_list
        assert reference.summary.operation.value == empty.summary.operation.value
        assert reference.summary.additional_properties["total-records"] == "1"
        assert table.scan().to_arrow().to_pylist() == [{"id": 42}]
        if format_version == 3:
            assert first.added_rows == empty.added_rows == reference.added_rows == 0
            assert first.first_row_id == 0
            assert empty.first_row_id == reference.first_row_id == 1
            assert table.metadata.next_row_id == 1
    finally:
        if created:
            rest_catalog.drop_table(f"default.{name}")
