"""Exercise real catalog HTTP requests, replay, authorization, and expiry with a local REST stub.

Run with Python 3.14: python test/catalog_idempotency.py --duckdb build/reldebug/duckdb
The stub owns a temporary local warehouse. No external catalog or credentials are used.
"""

import argparse
import copy
from functools import partial
import hashlib
import hmac
import re
import json
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


class Catalog:
    def __init__(self, root, lifetime="PT30M", fault="503", multi=True, fault_target="both"):
        self.root = root
        self.lifetime = lifetime
        self.fault = fault
        self.multi = multi
        self.fault_target = fault_target
        self.tables = {}
        self.namespaces = []
        self.requests = []
        self.results = {}
        self.injected = set()
        self.applied = {}
        self.token_requests = 0

    def save(self, name, metadata):
        path = self.root / (str(uuid.uuid4()) + ".metadata.json")
        path.write_text(json.dumps(metadata))
        self.tables[name] = {"metadata-location": str(path), "metadata": metadata}
        return self.tables[name]

    def create(self, body):
        name = body["name"]
        if name in self.tables:
            return 409, {"error": {"message": "Already exists", "type": "AlreadyExistsException", "code": 409}}
        (self.root / name / "metadata").mkdir(parents=True, exist_ok=True)
        metadata = {
            "format-version": 2,
            "table-uuid": str(uuid.uuid4()),
            "location": str(self.root / name),
            "last-sequence-number": 0,
            "last-updated-ms": int(time.time() * 1000),
            "last-column-id": max(f["id"] for f in body["schema"]["fields"]),
            "schemas": [body["schema"]],
            "current-schema-id": body["schema"]["schema-id"],
            "partition-specs": [{"spec-id": 0, "fields": []}],
            "default-spec-id": 0,
            "last-partition-id": 999,
            "properties": body.get("properties", {}),
            "snapshots": [],
            "snapshot-log": [],
            "metadata-log": [],
            "sort-orders": [{"order-id": 0, "fields": []}],
            "default-sort-order-id": 0,
        }
        return 200, self.save(name, metadata)

    def update(self, name, body):
        metadata = copy.deepcopy(self.tables[name]["metadata"])
        for requirement in body.get("requirements", []):
            if requirement["type"] == "assert-ref-snapshot-id":
                actual = metadata.get("refs", {}).get(requirement["ref"], {}).get("snapshot-id")
                if actual != requirement.get("snapshot-id"):
                    return 409, {"error": {"message": "Stale ref", "type": "CommitFailedException", "code": 409}}
        for update in body["updates"]:
            action = update["action"]
            if action == "add-snapshot":
                metadata["snapshots"].append(update["snapshot"])
                metadata["last-sequence-number"] = update["snapshot"]["sequence-number"]
            elif action == "set-snapshot-ref":
                metadata.setdefault("refs", {})[update["ref-name"]] = {
                    k: v for k, v in update.items() if k not in ("action", "ref-name")
                }
                if update["ref-name"] == "main":
                    metadata["current-snapshot-id"] = update["snapshot-id"]
                    metadata["snapshot-log"].append(
                        {"snapshot-id": update["snapshot-id"], "timestamp-ms": int(time.time() * 1000)}
                    )
            elif action == "add-schema":
                metadata["schemas"].append(update["schema"])
                metadata["last-column-id"] = update.get("last-column-id", metadata["last-column-id"])
            elif action == "set-current-schema":
                metadata["current-schema-id"] = (
                    update["schema-id"] if update["schema-id"] != -1 else metadata["schemas"][-1]["schema-id"]
                )
            elif action == "set-properties":
                metadata["properties"].update(update["updates"])
            elif action == "remove-properties":
                for key in update["removals"]:
                    metadata["properties"].pop(key, None)
            elif action in (
                "assign-uuid",
                "upgrade-format-version",
                "set-location",
                "set-default-spec",
                "set-default-sort-order",
            ):
                fields = {
                    "assign-uuid": "uuid",
                    "upgrade-format-version": "format-version",
                    "set-location": "location",
                    "set-default-spec": "spec-id",
                    "set-default-sort-order": "sort-order-id",
                }
                target = {
                    "assign-uuid": "table-uuid",
                    "set-default-spec": "default-spec-id",
                    "set-default-sort-order": "default-sort-order-id",
                }.get(action, fields[action])
                metadata[target] = update[fields[action]]
            elif action == "add-spec":
                metadata["partition-specs"].append(update["spec"])
            elif action == "add-sort-order":
                metadata["sort-orders"].append(update["sort-order"])
            else:
                raise AssertionError("Unimplemented stub update: " + action)
        metadata["last-updated-ms"] = int(time.time() * 1000)
        return 200, self.save(name, metadata)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def respond(self, status, body):
        payload = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def partial_response(self, result):
        payload = json.dumps(result).encode()
        self.send_response(409 if self.server.catalog.fault == "partial409" else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload) + 100))
        self.end_headers()
        self.wfile.write(payload[:10])
        self.wfile.flush()
        self.connection.shutdown(socket.SHUT_RDWR)
        self.connection.close()

    def valid_signature(self, raw):
        auth = self.headers["Authorization"]
        match = re.fullmatch(r"AWS4-HMAC-SHA256 Credential=([^,]+), SignedHeaders=([^,]+), Signature=([a-f0-9]+)", auth)
        if not match:
            return False
        credential, signed, signature = match.groups()
        if "idempotency-key" not in signed.split(";"):
            return False
        scope = credential.split("/", 1)[1]
        canonical_headers = "".join(
            name + ":" + " ".join(self.headers[name].split()) + "\n" for name in signed.split(";")
        )
        parsed = urlparse(self.path)
        payload = self.headers.get("x-amz-content-sha256", hashlib.sha256(raw).hexdigest())
        canonical = "\n".join((self.command, parsed.path, parsed.query, canonical_headers, signed, payload))
        string_to_sign = "\n".join(
            ("AWS4-HMAC-SHA256", self.headers["x-amz-date"], scope, hashlib.sha256(canonical.encode()).hexdigest())
        )
        key = b"AWS4stub"
        for part in scope.split("/"):
            key = hmac.new(key, part.encode(), hashlib.sha256).digest()
        return hmac.compare_digest(signature, hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest())

    def dispatch(self):
        state = self.server.catalog
        path = urlparse(self.path).path
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.loads(raw) if raw and not path.endswith("/tokens") else {}
        if path.endswith("/tokens"):
            state.token_requests += 1
            self.respond(200, {"access_token": "stub-refreshed", "token_type": "Bearer", "expires_in": 3600})
            return
        key = self.headers.get("Idempotency-Key")
        method = self.command
        state.requests.append(
            {
                "method": method,
                "path": path,
                "key": key,
                "body": hashlib.sha256(raw).hexdigest(),
                "snapshot": any(
                    u["action"] == "add-snapshot"
                    for change in body.get("table-changes", [body])
                    for u in change.get("updates", [])
                ),
                "at": time.monotonic(),
                "signed": (
                    self.valid_signature(raw)
                    if self.headers.get("Authorization", "").startswith("AWS4-HMAC-SHA256")
                    else False
                ),
            }
        )
        if method == "GET":
            if path.endswith("/config"):
                result = {"defaults": {}, "overrides": {}}
                if state.lifetime is not None:
                    result["idempotency-key-lifetime"] = state.lifetime
                if state.multi:
                    result["endpoints"] = [
                        "POST /v1/{prefix}/transactions/commit",
                        "POST /v1/{prefix}/namespaces/{namespace}/tables/{table}",
                        "POST /v1/{prefix}/namespaces",
                        "GET /v1/{prefix}/namespaces",
                        "GET /v1/{prefix}/namespaces/{namespace}/tables",
                        "POST /v1/{prefix}/namespaces/{namespace}/tables",
                        "DELETE /v1/{prefix}/namespaces/{namespace}/tables/{table}",
                        "DELETE /v1/{prefix}/namespaces/{namespace}",
                    ]
                else:
                    result["endpoints"] = [
                        "POST /v1/{prefix}/namespaces/{namespace}/tables/{table}",
                        "POST /v1/{prefix}/namespaces",
                        "GET /v1/{prefix}/namespaces",
                        "GET /v1/{prefix}/namespaces/{namespace}/tables",
                        "POST /v1/{prefix}/namespaces/{namespace}/tables",
                        "DELETE /v1/{prefix}/namespaces/{namespace}/tables/{table}",
                        "DELETE /v1/{prefix}/namespaces/{namespace}",
                    ]
                self.respond(200, result)
            elif "/tables/" in path:
                (
                    self.respond(200, state.tables[path.split("/")[-1]])
                    if path.split("/")[-1] in state.tables
                    else self.respond(
                        404, {"error": {"message": "Missing table", "type": "NoSuchTableException", "code": 404}}
                    )
                )
            elif path.endswith("/tables"):
                self.respond(200, {"identifiers": [{"namespace": ["ns"], "name": n} for n in state.tables]})
            elif path.endswith("/namespaces"):
                self.respond(200, {"namespaces": state.namespaces})
            else:
                (
                    self.respond(200, {"namespace": ["ns"], "properties": {}})
                    if ["ns"] in state.namespaces
                    else self.respond(
                        404,
                        {"error": {"message": "Missing namespace", "type": "NoSuchNamespaceException", "code": 404}},
                    )
                )
            return
        if method == "HEAD":
            self.respond(200, {})
            return
        operation = (method, path, hashlib.sha256(raw).hexdigest())
        is_commit = path.endswith("/transactions/commit") or "/tables/" in path and method == "POST"
        changes = body.get("table-changes", [body])
        has_snapshot = any(u["action"] == "add-snapshot" for change in changes for u in change.get("updates", []))
        target = (
            "commit"
            if is_commit and has_snapshot
            else "metadata" if is_commit else "create" if path.endswith("/tables") else "namespace"
        )
        if key and key in state.results:
            status, result = state.results[key]
            if state.fault in ("partial", "partial409") and target == "commit":
                self.partial_response(result)
            else:
                self.respond(status, result)
            return
        if state.fault in ("oauth", "oauth-expiry") and target == "create" and target not in state.injected:
            state.injected.add(target)
            if state.fault == "oauth-expiry":
                time.sleep(0.35)
            self.respond(401, {"error": {"message": "Refresh token", "type": "NotAuthorizedException", "code": 401}})
            return
        if state.fault == "conflict" and target == "commit" and "commit" not in state.injected:
            state.injected.add("commit")
            self.respond(409, {"error": {"message": "Retry commit", "type": "CommitFailedException", "code": 409}})
            return
        if method == "DELETE":
            if "/tables/" in path:
                state.tables.pop(path.split("/")[-1])
            status, result = 204, {}
        elif path.endswith("/transactions/commit"):
            status, result = 200, {}
            for change in body["table-changes"]:
                status, result = state.update(change["identifier"]["name"], change)
                if status != 200:
                    break
            if status == 200:
                result = {}
        elif path.endswith("/tables"):
            status, result = state.create(body)
        elif "/tables/" in path:
            status, result = state.update(path.split("/")[-1], body)
        elif path.endswith("/namespaces"):
            state.namespaces.append(body["namespace"])
            status, result = 200, {"namespace": body["namespace"], "properties": {}}
        else:
            raise AssertionError(path)
        state.applied[operation] = state.applied.get(operation, 0) + 1
        if key:
            state.results[key] = (status, copy.deepcopy(result))
        if (
            target in ("create", "commit")
            and target not in state.injected
            and state.fault in ("408", "500", "502", "503", "504", "drop", "expiry")
            and state.fault_target in ("both", target)
        ):
            state.injected.add(target)
            if state.fault == "drop":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            if state.fault == "expiry":
                time.sleep(0.35)
            status = int(state.fault) if state.fault.isdigit() else 503
            self.respond(
                status,
                {
                    "error": {
                        "message": "Response lost after apply",
                        "type": "ServiceUnavailableException",
                        "code": status,
                    }
                },
            )
            return
        if state.fault in ("partial", "partial409") and target == "commit":
            self.partial_response(result)
        else:
            self.respond(status, result)

    do_GET = dispatch
    do_HEAD = dispatch
    do_POST = dispatch
    do_DELETE = dispatch


def run_case(
    binary,
    auth="none",
    lifetime="PT30M",
    fault="503",
    multi=True,
    conflict=False,
    fault_target="both",
    load_sql="",
    endpoint_option="ENDPOINT",
    retries=2,
    unkeyed_attempts=2,
):
    with tempfile.TemporaryDirectory(prefix="iceberg-idempotency-") as directory:
        state = Catalog(Path(directory), lifetime, fault, multi, fault_target)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.catalog = state
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        endpoint = "http://127.0.0.1:" + str(server.server_port)
        options = "AUTHORIZATION_TYPE 'none'"
        before = ""
        if auth == "oauth2":
            options = f"AUTHORIZATION_TYPE 'oauth2', TOKEN 'stub-initial', CLIENT_ID 'stub', CLIENT_SECRET 'stub', OAUTH2_SERVER_URI {quote(endpoint + '/tokens')}"
        elif auth.startswith("sigv4"):
            before = "CREATE SECRET stub_credentials (TYPE S3, KEY_ID 'stub', SECRET 'stub', REGION 'us-east-1');"
            options = "AUTHORIZATION_TYPE 'sigv4', SECRET 'stub_credentials', SIGV4_REGION 'us-east-1', SIGV4_SERVICE 'execute-api'"
            if auth == "sigv4-legacy":
                before += "SET iceberg_via_aws_sdk_for_catalog_interactions=true;"
        query = f"""
            {load_sql}
            SET http_retries={retries};
            SET http_retry_wait_ms=1;
            SET http_retry_backoff=1;
            {before}
            ATTACH '' AS c (TYPE ICEBERG, {endpoint_option} {quote(endpoint)}, {options},
                ACCESS_DELEGATION_MODE 'none', STAGE_CREATE_TABLES false);
            CREATE SCHEMA c.ns;
            CREATE TABLE c.ns.t (id INTEGER);
            INSERT INTO c.ns.t VALUES (42);
            SELECT count(*) AS rows FROM c.ns.t;
            SELECT count(*) AS snapshots FROM iceberg_snapshots('c.ns.t');
            DROP TABLE c.ns.t;
            DROP SCHEMA c.ns;
        """
        try:
            result = subprocess.run(
                [str(binary), "-unsigned", "-json", "-c", query], capture_output=True, text=True, timeout=45
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        mutations = [r for r in state.requests if r["method"] in ("POST", "DELETE")]
        if lifetime in ("", "PT0S", "P1M", "-PT1S", "PT999999999999999999999S"):
            assert (
                not mutations and result.returncode != 0 and "idempotency-key-lifetime" in result.stderr
            ), result.stderr
            print("PASS invalid lifetime=" + repr(lifetime), flush=True)
            return
        assert mutations, "No catalog mutations executed: " + result.stderr
        for request in state.requests:
            if request["method"] in ("GET", "HEAD") or lifetime is None:
                assert request["key"] is None, request
            else:
                assert request["key"] and uuid.UUID(request["key"]).version == 7, request
                if auth.startswith("sigv4"):
                    assert request["signed"], "Idempotency-Key was not signed"
        if fault in ("expiry", "oauth-expiry", "partial", "partial409"):
            assert result.returncode != 0, result.stderr
            if fault not in ("partial", "partial409"):
                assert "lifetime" in result.stderr, result.stderr
            creates = [r for r in mutations if r["path"].endswith("/tables")]
            if fault_target == "commit":
                commits = [r for r in mutations if r.get("snapshot")]
                if fault in ("partial", "partial409"):
                    assert len(commits) == retries + 1, commits
                    assert len({r["body"] for r in commits}) == 1, commits
                    assert len({r["key"] for r in commits}) == 1, commits
                else:
                    assert len({r["body"] for r in commits}) == len(commits), commits
                assert len(state.tables["t"]["metadata"]["snapshots"]) == 1
                check = subprocess.run(
                    [
                        str(binary),
                        "-unsigned",
                        "-json",
                        "-c",
                        load_sql
                        + "SELECT sum(id)::BIGINT AS total FROM iceberg_scan("
                        + quote(state.tables["t"]["metadata-location"])
                        + ");",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                assert check.returncode == 0 and json.loads(check.stdout) == [{"total": 42}], (
                    check.stdout,
                    check.stderr,
                )
            else:
                assert len(creates) == 1, creates
        elif lifetime is None and fault == "503":
            assert result.returncode != 0, "Unkeyed retry unexpectedly succeeded"
            assert len([r for r in mutations if r["path"].endswith("/tables")]) == unkeyed_attempts
        else:
            assert result.returncode == 0, result.stderr
            rows = [
                json.loads(line)
                for line in result.stdout.splitlines()
                if line.startswith("[") and "Success" not in line
            ]
            assert rows == [[{"rows": 1}], [{"snapshots": 1}]], rows
            if fault in ("408", "500", "502", "503", "504", "drop", "oauth"):
                groups = {}
                for request in mutations:
                    groups.setdefault((request["method"], request["path"], request["body"]), []).append(request)
                repeated = [requests for requests in groups.values() if len(requests) > 1]
                assert repeated, "Fault did not trigger a retry"
                for requests in repeated:
                    assert len({r["key"] for r in requests}) == 1, requests
                unique = [requests[0]["key"] for requests in groups.values()]
                assert len(set(unique)) == len(unique), "Different mutations reused a key"
            assert all(count == 1 for count in state.applied.values()), state.applied
            if fault == "oauth":
                assert state.token_requests == 1, state.token_requests
            if conflict:
                commits = [r for r in mutations if r.get("snapshot")]
                assert len(commits) >= 2 and commits[-1]["key"] != commits[-2]["key"], commits
                assert commits[-1]["body"] != commits[-2]["body"], "409 retry did not restage its body"
        print(f"PASS auth={auth} lifetime={lifetime} fault={fault} multi={multi} target={fault_target}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb", type=Path, required=True)
    parser.add_argument("--conflict-retry", action="store_true", help="Also check the 2.0 engine's 409 restaging")
    parser.add_argument("--avro-extension", type=Path)
    parser.add_argument("--iceberg-extension", type=Path)
    args = parser.parse_args()
    binary = args.duckdb.resolve()
    load_sql = "".join(
        "LOAD " + quote(path.resolve()) + ";" for path in (args.avro_extension, args.iceberg_extension) if path
    )
    run = partial(
        run_case,
        binary,
        load_sql=load_sql,
        endpoint_option="URI" if args.conflict_retry else "ENDPOINT",
        unkeyed_attempts=1 if args.conflict_retry else 2,
    )
    for auth in ("none", "oauth2", "sigv4") if args.conflict_retry else ("none", "oauth2", "sigv4", "sigv4-legacy"):
        run(auth=auth)
    for status in ("408", "500", "502", "504"):
        run(fault=status)
    run(multi=False)
    run(fault="drop")
    run(auth="oauth2", fault="oauth")
    run(lifetime=None, fault="none")
    run(lifetime=None)
    run(lifetime="PT0.2S", fault="expiry")
    run(lifetime="PT0.2S", fault="expiry", fault_target="commit")
    run(lifetime="PT0.2S", auth="oauth2", fault="oauth-expiry")
    for lifetime in ("PT24H", "P1DT1H2M3.123456S"):
        run(lifetime=lifetime, fault="none")
    for lifetime in ("", "PT0S", "P1M", "-PT1S", "PT999999999999999999999S"):
        run(lifetime=lifetime, fault="none")
    run(fault="partial", fault_target="commit")
    run(fault="partial", fault_target="commit", retries=0)
    run(fault="partial409", fault_target="commit")
    if args.conflict_retry:
        run(fault="conflict", conflict=True)
        run(fault="conflict", conflict=True, multi=False)


if __name__ == "__main__":
    main()
