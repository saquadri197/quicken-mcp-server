"""Load the newest Quicken/Simplifi CSV export into Postgres for Metabase.

Runs forever: every INTERVAL_SECONDS it looks at DATA_DIR, and when the newest
.csv is new (by name and modification time) and no longer being written, it
replaces finance.transactions with that file's rows in one transaction.
Parsing reuses app/csv_loader.py from this repo, so the dashboards and the
Quicken MCP server read exports the same way.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg

sys.path.insert(0, os.environ.get("REPO_DIR", "/app/src"))  # this repo, mounted read-only
from app.csv_loader import parse_csv, resolve_csv_path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("loader")

DATA_DIR = os.environ.get("DATA_DIR", "/data")
INTERVAL = int(os.environ.get("INTERVAL_SECONDS", "900"))
SCHEMA_SQL = Path(os.environ.get("SCHEMA_SQL", "/app/sql/schema.sql"))

COLUMNS = ("tx_id", "date", "account", "account_type", "payee", "original_payee", "category",
           "amount", "memo", "tags", "business_personal", "excluded", "is_transfer", "one_off",
           "number", "cleared")


def newest_file():
    p = resolve_csv_path(DATA_DIR)
    st = p.stat()
    return p, datetime.fromtimestamp(st.st_mtime, tz=timezone.utc), st.st_size


def already_loaded(conn, name, mtime) -> bool:
    row = conn.execute(
        "SELECT 1 FROM finance.load_log WHERE file = %s AND file_mtime = %s LIMIT 1",
        (name, mtime)).fetchone()
    return row is not None


def load(conn, path: Path, mtime) -> int:
    data = parse_csv(str(path))
    rows = [tuple(t[c] for c in COLUMNS) for t in data["transactions"]]
    with conn.transaction():
        conn.execute(SCHEMA_SQL.read_text())
        conn.execute("TRUNCATE finance.transactions")
        with conn.cursor() as cur:
            with cur.copy(f"COPY finance.transactions ({', '.join(COLUMNS)}) FROM STDIN") as copy:
                for r in rows:
                    copy.write_row(r)
        conn.execute("INSERT INTO finance.load_log (file, file_mtime, rows) VALUES (%s, %s, %s)",
                     (path.name, mtime, len(rows)))
    return len(rows)


def run_once(conn):
    path, mtime, size = newest_file()
    with conn.transaction():
        conn.execute(SCHEMA_SQL.read_text())  # make sure tables exist before checking the log
    if already_loaded(conn, path.name, mtime):
        return
    time.sleep(5)  # skip a file that is still being copied in
    if newest_file() != (path, mtime, size):
        log.info("%s is still changing; will try next round", path.name)
        return
    n = load(conn, path, mtime)
    log.info("Loaded %s: %d transactions", path.name, n)


def main():
    dsn = ""  # from PGHOST / PGUSER / PGPASSWORD / PGDATABASE
    while True:
        try:
            with psycopg.connect(dsn, autocommit=True) as conn:
                run_once(conn)
        except Exception as e:  # keep the previous data and retry next round
            log.error("Load failed, keeping previous data: %s", e)
        sys.stdout.flush()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
