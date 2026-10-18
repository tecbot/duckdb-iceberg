"""Verify and publish native Iceberg artifacts using Python 3.14."""

import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build/release"
ASSETS = ROOT / "build/release-assets"
TARGET = json.loads((ROOT / ".github/release-target.json").read_text())
PLATFORMS = {"linux_amd64", "linux_arm64", "osx_arm64"}


def run(*args, **kwargs):
    kwargs.setdefault("check", True)
    return subprocess.run(args, text=True, **kwargs)


def git(*args):
    return run("git", *args, capture_output=True).stdout.strip()


def sql(text):
    return run(
        str(BUILD / "duckdb"),
        "-unsigned",
        "-csv",
        "-noheader",
        "-c",
        text,
        capture_output=True,
    ).stdout.strip()


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def test(path, config=None):
    config_value = json.loads(Path(config).read_text()) if config else {}
    load = (
        f"LOAD {quote(BUILD / 'extension/avro/avro.duckdb_extension')}; "
        f"LOAD {quote(BUILD / 'extension/iceberg/iceberg.duckdb_extension')}; "
    )
    config_value["on_init"] = load + config_value.get("on_init", "")
    config_value["statically_loaded_extensions"] = [
        "core_functions",
        "parquet",
        "json",
        "httpfs",
        "icu",
    ]
    with tempfile.TemporaryDirectory() as directory:
        config_value.setdefault("settings", []).append(
            {"name": "extension_directory", "value": directory}
        )
        config_path = Path(directory) / "config.json"
        config_path.write_text(json.dumps(config_value))
        result = run(
            str(BUILD / "test/unittest"),
            "--test-dir",
            str(ROOT),
            "--autoloading",
            "all",
            "--test-config",
            str(config_path),
            str(path),
            capture_output=True,
            check=False,
            env={**os.environ, "LOCAL_EXTENSION_REPO": str(BUILD / "repository")},
        )
    output = result.stdout + result.stderr
    print(output)
    result.check_returncode()
    if not re.search(r"All tests passed \([1-9][0-9]* assertion", output):
        raise RuntimeError(f"Test did not execute assertions: {path}")


def verify():
    assert sys.version_info[:2] == (3, 14)
    assert git("-C", "duckdb", "rev-parse", "HEAD") == TARGET["duckdb_commit"]
    platform = os.environ["DUCKDB_PLATFORM"]
    assert platform in PLATFORMS
    assert sql("SELECT version()") == TARGET["duckdb_version"]
    assert sql("PRAGMA platform") == platform
    assert (
        sql("SELECT loaded FROM duckdb_extensions() WHERE extension_name='iceberg'")
        == "false"
    )
    artifact = BUILD / "extension/iceberg/iceberg.duckdb_extension"
    footer = artifact.read_bytes()[-512:]
    field = lambda start: footer[start : start + 32].rstrip(b"\0").decode()
    assert field(192) == platform
    assert field(160) == (
        TARGET["duckdb_commit"][:10]
        if TARGET["prerelease"]
        else TARGET["duckdb_version"]
    )
    assert field(128) == git("log", "-1", "--format=%h")
    with tempfile.TemporaryDirectory() as directory:
        table = Path(directory) / "table"
        result = sql(
            "SET autoinstall_known_extensions=false; SET autoload_known_extensions=false; "
            f"LOAD {quote(BUILD / 'extension/avro/avro.duckdb_extension')}; "
            f"LOAD {quote(artifact)}; "
            'SET SESSION iceberg_snapshot_properties=\'{"release.probe":"loaded-file"}\'; '
            f"COPY (SELECT 42 AS id) TO {quote(table)} (FORMAT ICEBERG); "
            "SELECT content::JSON->'snapshots'->0->'summary'->>'release.probe' "
            f"FROM read_text({quote(table / 'metadata/*.metadata.json')});"
        )
        assert result == "loaded-file", result
    test("test/sql/local/snapshot_properties_setting.test")
    test("test/sql/copy/copy_snapshot_properties.test")
    run(
        sys.executable,
        "test/catalog_idempotency.py",
        "--duckdb",
        str(BUILD / "duckdb"),
        "--avro-extension",
        str(BUILD / "extension/avro/avro.duckdb_extension"),
        "--iceberg-extension",
        str(artifact),
    )
    test("test/sql/local/iceberg_scans/iceberg_column_stats_truncated_utf8.test")
    test("test/sql/copy/utf8_string_bounds.test")
    test("test/sql/local/iceberg_scans/iceberg_metadata.test")
    test("test/sql/local/iceberg_scans/iceberg_refs.test")
    library = (
        BUILD
        / "src"
        / ("libduckdb.dylib" if sys.platform == "darwin" else "libduckdb.so")
    )
    run(
        sys.executable,
        "test/manifest_io_errors.py",
        "--duckdb-library",
        str(library),
        "--avro-extension",
        str(BUILD / "extension/avro/avro.duckdb_extension"),
        "--iceberg-extension",
        str(artifact),
    )


def catalog():
    catalog_type = TARGET["catalog"]
    config = f"test/configs/{catalog_type}.json"
    try:
        run("make", catalog_type)
        if catalog_type == "fixture":
            deadline = time.monotonic() + 180
            while True:
                try:
                    with urllib.request.urlopen(
                        "http://127.0.0.1:8181/v1/config", timeout=2
                    ) as response:
                        if response.status == 200:
                            break
                except (urllib.error.URLError, OSError):
                    if time.monotonic() > deadline:
                        raise RuntimeError("Fixture did not become ready")
                    time.sleep(2)
        directory = Path(
            "test/sql/local/catalog_test_config_setup/catalog_agnostic/transactions"
        )
        for name in TARGET["catalog_tests"]:
            test(directory / name, config)
    finally:
        run("make", catalog_type + "-stop")


def package():
    platform = os.environ["DUCKDB_PLATFORM"]
    assert platform in PLATFORMS
    ASSETS.mkdir(parents=True, exist_ok=True)
    name = f"iceberg-{TARGET['duckdb_version']}-{platform}.duckdb_extension"
    destination = ASSETS / name
    shutil.copyfile(BUILD / "extension/iceberg/iceberg.duckdb_extension", destination)
    manifest = {
        "duckdb_version": TARGET["duckdb_version"],
        "duckdb_commit": TARGET["duckdb_commit"],
        "extension_commit": git("rev-parse", "HEAD"),
        "ci_tools_commit": git("-C", "extension-ci-tools", "rev-parse", "HEAD"),
        "platform": platform,
        "file": name,
        "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
        "signed": False,
    }
    avro_name = f"avro-{TARGET['duckdb_version']}-{platform}.duckdb_extension"
    avro_destination = ASSETS / avro_name
    shutil.copyfile(BUILD / "extension/avro/avro.duckdb_extension", avro_destination)
    manifest["dependencies"] = [
        {
            "file": avro_name,
            "sha256": hashlib.sha256(avro_destination.read_bytes()).hexdigest(),
            "extension_version": avro_destination.read_bytes()[-384:-352]
            .rstrip(b"\0")
            .decode(),
        }
    ]
    (ASSETS / f"{platform}.json").write_text(json.dumps(manifest, indent=2) + "\n")

    bundle()


def bundle():
    for manifest_path in sorted(ASSETS.glob("*.json")):
        manifest = json.loads(manifest_path.read_text())
        assert manifest["duckdb_version"] == TARGET["duckdb_version"]
        assert manifest["duckdb_commit"] == TARGET["duckdb_commit"]
        assert manifest["platform"] in PLATFORMS
        entries = [manifest, *manifest["dependencies"]]
        paths = []
        for item in entries:
            path = ASSETS / item["file"]
            assert path.name == item["file"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
            paths.append(path)
        archive_manifest = json.loads(json.dumps(manifest))
        archive_manifest["packaging_commit"] = git("rev-parse", "HEAD")
        archive_manifest["file"] = "iceberg.duckdb_extension"
        assert len(archive_manifest["dependencies"]) == 1
        archive_manifest["dependencies"][0]["file"] = "avro.duckdb_extension"
        archive = (
            ASSETS
            / f"iceberg-{manifest['duckdb_version']}-{manifest['platform']}.tar.gz"
        )
        data = (json.dumps(archive_manifest, indent=2) + "\n").encode()
        with tarfile.open(archive, "w:gz") as output:
            output.add(paths[0], arcname="iceberg.duckdb_extension")
            output.add(paths[1], arcname="avro.duckdb_extension")
            info = tarfile.TarInfo("manifest.json")
            info.size = len(data)
            output.addfile(info, io.BytesIO(data))
        with tarfile.open(archive, "r:gz") as output:
            for item in [archive_manifest, *archive_manifest["dependencies"]]:
                assert (
                    hashlib.sha256(output.extractfile(item["file"]).read()).hexdigest()
                    == item["sha256"]
                )


def publish():
    manifests = [json.loads(path.read_text()) for path in ASSETS.glob("*.json")]
    assert len(manifests) == len(PLATFORMS)
    assert {item["platform"] for item in manifests} == PLATFORMS
    commit = git("rev-parse", "HEAD")
    for item in manifests:
        assert item["extension_commit"] == commit
        assert item["duckdb_commit"] == TARGET["duckdb_commit"]
        assert item["duckdb_version"] == TARGET["duckdb_version"]
        path = ASSETS / item["file"]
        assert path.name == item["file"]
        assert hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
        for dependency in item["dependencies"]:
            path = ASSETS / dependency["file"]
            assert path.name == dependency["file"]
            assert hashlib.sha256(path.read_bytes()).hexdigest() == dependency["sha256"]
    checksums = ASSETS / "SHA256SUMS"
    checksums.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in sorted(ASSETS.iterdir())
            if path.is_file() and path != checksums
        )
    )
    tag = os.environ["GITHUB_REF_NAME"]
    expected_tag = f"iceberg-snapshot-properties-duckdb-{TARGET['duckdb_version'].removeprefix('v')}-v{TARGET['release_revision']}"
    assert tag == expected_tag, tag
    assert git("rev-list", "-n", "1", tag) == commit
    notes = ASSETS.parent / "release-notes.md"
    notes.write_text(
        f"This release adds custom snapshot summary properties. It addresses "
        f"[duckdb/duckdb-iceberg#1249](https://github.com/duckdb/duckdb-iceberg/issues/1249).\n\n"
        "This revision adds catalog mutation idempotency. When a catalog advertises "
        "`idempotency-key-lifetime`, mutations send a UUIDv7 key and reuse it across "
        "transport retries and OAuth refresh. Different mutations and restaged conflict "
        "retries use new keys. Retry expiry reports an unknown outcome and preserves "
        "potentially committed data files. Catalogs without the capability keep their "
        "existing behavior. See [fork issue #6](https://github.com/tecbot/duckdb-iceberg/issues/6).\n\n"
        "This release also backports `iceberg_refs()` to DuckDB 1.5. It lists branch "
        "and tag heads with their optional retention settings. Join its `main` row "
        "to `iceberg_snapshots()` to find the main head when another branch has "
        "a higher snapshot sequence number. Metadata without `refs` returns "
        "`main` at the current snapshot; metadata without either returns no rows. "
        "For catalog table names, both functions use the transaction's cached "
        "metadata file across concurrent commits. "
        "See [fork issue #5](https://github.com/tecbot/duckdb-iceberg/issues/5).\n\n"
        "This release also omits optional string bounds that contain invalid UTF-8. "
        "Filters read the affected data file instead of excluding it with an invalid bound. "
        "Other valid statistics remain available for pruning. New writes retain "
        "string bound truncation at complete UTF-8 code points. "
        "See [tecbot/duckdb-iceberg#4](https://github.com/tecbot/duckdb-iceberg/issues/4).\n\n"
        "This release also prevents a connection failure during a manifest read from "
        "aborting the host process. The operation returns an I/O error, and cleanup "
        "finishes without throwing from the scan destructor. "
        "See [tecbot/duckdb-iceberg#3](https://github.com/tecbot/duckdb-iceberg/issues/3).\n\n"
        "This release also fixes the first MERGE or INSERT after ADD COLUMN on a table "
        "that has no snapshots. The writer uses the schema selected during binding. "
        "A changed cached table state produces a transaction error. "
        "See [tecbot/duckdb-iceberg#2](https://github.com/tecbot/duckdb-iceberg/issues/2).\n\n"
        "Every INSERT creates an append snapshot, including an INSERT that produces zero rows. "
        "Empty appends capture the session snapshot properties, write a new manifest list without new data files, "
        "and use the normal append conflict policy. COMMIT publishes pending snapshots; ROLLBACK discards them.\n\n"
        f"DuckDB runtime: `{TARGET['duckdb_version']}`.\n"
        f"DuckDB source: `{TARGET['duckdb_commit']}`.\n"
        f"Extension source: `{commit}`.\n\n"
        "Platforms: Linux AMD64 and ARM64 (glibc 2.28 or later), and macOS ARM64.\n"
        "The binaries are unsigned. Enable `allow_unsigned_extensions` when you start DuckDB. "
        "Download and extract the platform archive. It contains `avro.duckdb_extension`, `iceberg.duckdb_extension`, and `manifest.json`. Load the extracted Avro file before you load Iceberg. "
        "DuckDB requires these canonical file names to find the extension entry points. If you download the separate binaries, rename them before you load them. "
        "Verify the files with `SHA256SUMS`. Platform JSON files record the source commits and binary hashes.\n\n"
        'Use `SET SESSION iceberg_snapshot_properties = \'{"application.transaction-id":"txn-001"}\';` '
        "before a write. Use `RESET iceberg_snapshot_properties;` to clear the setting. "
        "See the README at this tag for the full behavior and limits.\n\n"
        + (
            "This is a development build. It requires the exact DuckDB 2.0 development commit listed above. "
            "It does not target a stable DuckDB 2.0 release.\n"
            if TARGET["prerelease"]
            else ""
        )
    )
    args = [
        "gh",
        "release",
        "create",
        tag,
        "--verify-tag",
        "--title",
        f"Snapshot properties for DuckDB {TARGET['duckdb_version']}",
        "--notes-file",
        str(notes),
    ]
    if TARGET["prerelease"]:
        args.append("--prerelease")
    args.extend(str(path) for path in sorted(ASSETS.iterdir()) if path.is_file())
    run(*args)


if __name__ == "__main__":
    os.chdir(ROOT)
    {
        "verify": verify,
        "catalog": catalog,
        "package": package,
        "bundle": bundle,
        "publish": publish,
    }[sys.argv[1]]()
