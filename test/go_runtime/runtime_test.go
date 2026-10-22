package runtime_test

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	_ "github.com/duckdb/duckdb-go/v2"
)

func open(t *testing.T) *sql.DB {
	t.Helper()
	if os.Getenv("ICEBERG_TEST_URI") == "" {
		t.Skip("Run python run.py with an isolated test catalog")
	}
	db, err := sql.Open("duckdb", "?allow_unsigned_extensions=true")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { db.Close() })
	for _, name := range []string{"httpfs", "avro", "iceberg"} {
		path := strings.ReplaceAll(filepath.Join(os.Getenv("ICEBERG_TEST_EXTENSIONS"), name+".duckdb_extension"), "'", "''")
		if _, err := db.Exec("LOAD '" + path + "'"); err != nil {
			t.Fatal(err)
		}
	}
	var version string
	if err := db.QueryRow("SELECT version()").Scan(&version); err != nil || version != "v2.0.0" {
		t.Fatalf("engine=%s error=%v", version, err)
	}
	query := fmt.Sprintf("ATTACH '' AS c(TYPE ICEBERG, URI '%s', AUTHORIZATION_TYPE 'none', ACCESS_DELEGATION_MODE 'none', STAGE_CREATE_TABLES true); CREATE SCHEMA IF NOT EXISTS c.ns", os.Getenv("ICEBERG_TEST_URI"))
	if _, err := db.Exec(query); err != nil {
		t.Fatal(err)
	}
	return db
}

func TestReadThenWrite(t *testing.T) {
	for _, retries := range []int{0, 4} {
		t.Run(fmt.Sprint(retries), func(t *testing.T) {
			db := open(t)
			ctx := context.Background()
			table := fmt.Sprintf("c.ns.go_read_%d", retries)
			must := func(conn *sql.Conn, query string) {
				t.Helper()
				if _, err := conn.ExecContext(ctx, query); err != nil {
					t.Fatal(err)
				}
			}
			var cons []*sql.Conn
			for i := 0; i < 3; i++ {
				con, err := db.Conn(ctx)
				if err != nil {
					t.Fatal(err)
				}
				cons = append(cons, con)
				defer con.Close()
			}
			count := func(conn *sql.Conn, expected int) {
				t.Helper()
				var got int
				err := conn.QueryRowContext(ctx, "SELECT count(*) FROM "+table+" WHERE a=7").Scan(&got)
				if err != nil || got != expected {
					t.Fatalf("count=%d expected=%d error=%v", got, expected, err)
				}
			}
			must(cons[0], fmt.Sprintf("CREATE TABLE %s(a INT,b INT) WITH ('commit.retry.num-retries'='%d'); INSERT INTO %s VALUES(0,0)", table, retries, table))
			must(cons[0], "BEGIN")
			count(cons[0], 0)
			must(cons[1], "INSERT INTO "+table+" VALUES(7,7)")
			count(cons[2], 1)
			count(cons[0], 0)
			must(cons[0], "INSERT INTO "+table+" SELECT 7,7 WHERE NOT EXISTS(SELECT 1 FROM "+table+" WHERE a=7)")
			_, err := cons[0].ExecContext(ctx, "COMMIT")
			if retries == 0 {
				if err == nil || !strings.Contains(err.Error(), "409") {
					t.Fatalf("expected conflict, got %v", err)
				}
				count(cons[2], 1)
			} else {
				if err != nil {
					t.Fatal(err)
				}
				count(cons[2], 2)
			}
		})
	}
}

func TestForkSnapshots(t *testing.T) {
	if os.Getenv("ICEBERG_TEST_FORK") != "1" {
		t.Skip("Fork-only snapshot properties and empty INSERT behavior")
	}
	db := open(t)
	_, err := db.Exec(`CREATE TABLE c.ns.go_properties(id INT); SET iceberg_snapshot_properties='{"probe":"go-runtime"}'; INSERT INTO c.ns.go_properties SELECT 1 WHERE false`)
	if err != nil {
		t.Fatal(err)
	}
	var snapshots, rows int
	if err = db.QueryRow("SELECT count(*) FROM iceberg_snapshots('c.ns.go_properties') WHERE summary['probe']='go-runtime' AND operation='append'").Scan(&snapshots); err != nil {
		t.Fatal(err)
	}
	if err = db.QueryRow("SELECT count(*) FROM c.ns.go_properties").Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if snapshots != 1 || rows != 0 {
		t.Fatalf("snapshots=%d rows=%d", snapshots, rows)
	}
}
