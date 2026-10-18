"""Exercise manifest connection failures through the C API in child processes."""

import argparse
import ctypes as c
import http.server
import json
from pathlib import Path
import socket
import subprocess
import sys
import threading
import urllib.request


ROOT = Path(__file__).resolve().parents[1]


class Result(c.Structure):
    _fields_ = [
        ("column_count", c.c_uint64),
        ("row_count", c.c_uint64),
        ("rows_changed", c.c_uint64),
        ("columns", c.c_void_p),
        ("error", c.c_char_p),
        ("internal_data", c.c_void_p),
    ]


class API:
    def __init__(self, library):
        self.lib = c.CDLL(str(library))
        handle = c.c_void_p
        pointer = c.POINTER(handle)
        result = c.POINTER(Result)
        functions = {
            "create_config": ([pointer], c.c_int),
            "set_config": ([handle, c.c_char_p, c.c_char_p], c.c_int),
            "destroy_config": ([pointer], None),
            "open_ext": ([c.c_char_p, pointer, handle, c.POINTER(c.c_char_p)], c.c_int),
            "connect": ([handle, pointer], c.c_int),
            "disconnect": ([pointer], None),
            "close": ([pointer], None),
            "query": ([handle, c.c_char_p, result], c.c_int),
            "destroy_result": ([result], None),
            "result_error": ([result], c.c_char_p),
            "result_error_type": ([result], c.c_int),
            "row_count": ([result], c.c_uint64),
            "value_int64": ([result, c.c_uint64, c.c_uint64], c.c_int64),
            "prepare": ([handle, c.c_char_p, pointer], c.c_int),
            "prepare_error": ([handle], c.c_char_p),
            "execute_prepared": ([handle, result], c.c_int),
            "destroy_prepare": ([pointer], None),
        }
        for name, (arguments, returns) in functions.items():
            function = getattr(self.lib, "duckdb_" + name)
            function.argtypes = arguments
            function.restype = returns
        config = handle()
        assert self.lib.duckdb_create_config(c.byref(config)) == 0
        try:
            assert self.lib.duckdb_set_config(config, b"allow_unsigned_extensions", b"true") == 0
            self.database = handle()
            error = c.c_char_p()
            assert self.lib.duckdb_open_ext(None, c.byref(self.database), config, c.byref(error)) == 0, error.value
        finally:
            self.lib.duckdb_destroy_config(c.byref(config))
        self.connection = handle()
        assert self.lib.duckdb_connect(self.database, c.byref(self.connection)) == 0

    def query(self, sql):
        result = Result()
        try:
            status = self.lib.duckdb_query(self.connection, sql.encode(), c.byref(result))
            error = self.lib.duckdb_result_error(c.byref(result))
            error_type = self.lib.duckdb_result_error_type(c.byref(result))
            rows = []
            if status == 0:
                rows = [
                    self.lib.duckdb_value_int64(c.byref(result), 0, row)
                    for row in range(self.lib.duckdb_row_count(c.byref(result)))
                ]
            return status, error.decode() if error else "", error_type, rows
        finally:
            self.lib.duckdb_destroy_result(c.byref(result))

    def prepare(self, sql):
        statement = c.c_void_p()
        try:
            status = self.lib.duckdb_prepare(self.connection, sql.encode(), c.byref(statement))
            error = self.lib.duckdb_prepare_error(statement)
            if status != 0:
                return status, error.decode() if error else ""
            # DuckDB 2.0 can defer manifest reads until execution of the prepared statement.
            result = Result()
            try:
                status = self.lib.duckdb_execute_prepared(statement, c.byref(result))
                error = self.lib.duckdb_result_error(c.byref(result))
                assert self.lib.duckdb_result_error_type(c.byref(result)) == 28
                return status, error.decode() if error else ""
            finally:
                self.lib.duckdb_destroy_result(c.byref(result))
        finally:
            self.lib.duckdb_destroy_prepare(c.byref(statement))

    def close(self):
        self.lib.duckdb_disconnect(c.byref(self.connection))
        self.lib.duckdb_close(c.byref(self.database))


def child(args):
    api = API(args.duckdb_library)
    try:
        for name in ("httpfs", "avro", "iceberg"):
            path = getattr(args, name + "_extension")
            load = str(path.resolve()).replace("'", "''") if path else name
            status, error, _, _ = api.query("LOAD '" + load + "'")
            assert status == 0, error
        for sql in (
            f"SET threads={args.threads}",
            "SET http_retries=0",
            "SET http_timeout=2",
        ):
            assert api.query(sql)[0] == 0
        sql = f"SELECT id FROM iceberg_scan('{args.endpoint}', version='1', allow_moved_paths=true) ORDER BY id"
        if args.phase == "prepare":
            status, error = api.prepare(sql)
        else:
            status, error, error_type, _ = api.query(sql)
            assert error_type == 28, (error_type, error)  # DUCKDB_ERROR_IO
        assert status == 1 and "IO Error" in error, (status, error)
        assert args.failed_file in error, error
        assert api.query("SELECT 1")[3] == [1]
        request = urllib.request.Request(args.endpoint + "/restore", method="POST")
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
        status, error, _, rows = api.query(sql)
        assert status == 0 and rows == [42, 43], (status, error, rows)
        print(
            json.dumps(
                {
                    "phase": args.phase,
                    "threads": args.threads,
                    "failed_file": args.failed_file,
                    "recovered_rows": rows,
                }
            )
        )
    finally:
        api.close()


class FaultServer(http.server.ThreadingHTTPServer):
    def __init__(self):
        super().__init__(("127.0.0.1", 0), FaultHandler)
        self.failed_file = ""
        self.requests = []
        self.lock = threading.Lock()


class FaultHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        assert self.path == "/restore"
        with self.server.lock:
            self.server.failed_file = ""
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self):
        self.serve(head=True)

    def do_GET(self):
        self.serve(head=False)

    def serve(self, head):
        with self.server.lock:
            fail = self.path == "/metadata/" + self.server.failed_file
            self.server.requests.append((self.path, fail))
        if fail:
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_RDWR)
            return
        fixture = ROOT / "data/persistent/manifest_io_errors"
        path = (fixture / self.path.lstrip("/")).resolve()
        if not path.is_relative_to(fixture.resolve()) or not path.is_file():
            self.send_error(404)
            return
        data = path.read_bytes()
        size = len(data)
        start, end = 0, size - 1
        byte_range = self.headers.get("Range")
        if byte_range:
            first, last = byte_range.removeprefix("bytes=").split("-")
            start = int(first)
            end = min(int(last) if last else end, end)
        self.send_response(206 if byte_range else 200)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        if byte_range:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if not head:
            self.wfile.write(data[start : end + 1])


def main(args):
    server = FaultServer()
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    failures = []
    try:
        for failed_file in ("manifest-b.avro", "manifest-list.avro"):
            for phase in ("prepare", "query"):
                for threads in (1, 4):
                    with server.lock:
                        server.failed_file = failed_file
                        server.requests.clear()
                    command = [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "--duckdb-library",
                        str(args.duckdb_library.resolve()),
                        "--endpoint",
                        f"http://127.0.0.1:{server.server_port}",
                        "--failed-file",
                        failed_file,
                        "--phase",
                        phase,
                        "--threads",
                        str(threads),
                    ]
                    for name in ("httpfs", "avro", "iceberg"):
                        path = getattr(args, name + "_extension")
                        if path:
                            command.extend(["--" + name + "-extension", str(path.resolve())])
                    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
                    with server.lock:
                        requests = list(server.requests)
                    assert any(failed for _, failed in requests), (
                        failed_file,
                        requests,
                        result.stderr,
                    )
                    if failed_file == "manifest-b.avro":
                        assert (
                            "/metadata/manifest-list.avro",
                            False,
                        ) in requests, requests
                        assert (
                            "/metadata/manifest-a.avro",
                            False,
                        ) in requests, requests
                    if result.returncode:
                        failures.append(
                            f"{failed_file}/{phase}/threads={threads}: exit {result.returncode}\n{result.stderr}"
                        )
                    else:
                        print(result.stdout.strip())
        assert not failures, "\n".join(failures)
        print("All 8 manifest connection failure and recovery cases passed.")
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duckdb-library", required=True, type=Path)
    for name in ("httpfs", "avro", "iceberg"):
        parser.add_argument("--" + name + "-extension", type=Path)
    parser.add_argument("--endpoint", help=argparse.SUPPRESS)
    parser.add_argument("--failed-file", help=argparse.SUPPRESS)
    parser.add_argument("--phase", choices=("prepare", "query"), help=argparse.SUPPRESS)
    parser.add_argument("--threads", type=int, help=argparse.SUPPRESS)
    arguments = parser.parse_args()
    if arguments.endpoint:
        child(arguments)
    else:
        main(arguments)
