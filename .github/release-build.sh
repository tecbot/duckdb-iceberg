#!/usr/bin/env bash
set -euo pipefail
python -c 'import sys; assert sys.version_info[:2] == (3, 14)'
expected_commit="$(python -c 'import json; print(json.load(open(".github/release-target.json"))["duckdb_commit"])')"
actual_commit="$(git -C duckdb rev-parse HEAD)"
test "$actual_commit" = "$expected_commit"
version="$(python -c 'import json; print(json.load(open(".github/release-target.json"))["duckdb_version"])')"
if [[ "$version" == v2.* ]]; then
    export EXT_FLAGS="-DDUCKDB_EXPLICIT_VERSION=$version"
else
    export OVERRIDE_GIT_DESCRIBE="$version"
fi
make release
