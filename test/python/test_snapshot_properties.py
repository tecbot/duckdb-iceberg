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
