"""Load the lease-watch CSVs into Postgres (schema "lease") for Metabase.

The weekly lease-watch run writes runs.csv, vehicles.csv, offers.csv and
quotes.csv to the lease-watch folder on the quicken-data share. Every
INTERVAL_SECONDS this reloads any file whose modification time changed,
replacing that table's rows in one transaction.

The folder is a subfolder, so the Quicken loader (which only reads *.csv at the
top of the share) never sees these files.
"""

import csv
import logging
import os
import sys
import time
from pathlib import Path

import psycopg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lease-loader")

DATA_DIR = Path(os.environ.get("LEASE_DIR", "/data/lease-watch"))
INTERVAL = int(os.environ.get("INTERVAL_SECONDS", "900"))
SCHEMA_SQL = Path(os.environ.get("LEASE_SQL", "/app/sql/lease.sql"))
TABLES = ("runs", "vehicles", "offers", "quotes")


def table_columns(conn, table):
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'lease' AND table_name = %s ORDER BY ordinal_position",
        (table,)).fetchall()
    return {name: dtype for name, dtype in rows}


def clean(value, dtype):
    v = (value or "").strip()
    if v == "":
        return None
    if dtype == "boolean":
        return v.lower() in ("true", "yes", "y", "1")
    if dtype in ("numeric", "integer"):
        return v.replace("$", "").replace(",", "")
    return v


def load_file(conn, table, path):
    cols = table_columns(conn, table)
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        use = [c for c in (reader.fieldnames or []) if c in cols]
        unknown = [c for c in (reader.fieldnames or []) if c not in cols]
        if unknown:
            log.warning("%s: ignoring unknown columns %s", path.name, unknown)
        rows = [tuple(clean(r.get(c), cols[c]) for c in use) for r in reader]
    with conn.transaction():
        conn.execute(f"TRUNCATE lease.{table}")
        with conn.cursor() as cur:
            with cur.copy(f"COPY lease.{table} ({', '.join(use)}) FROM STDIN") as copy:
                for r in rows:
                    copy.write_row(r)
        conn.execute("INSERT INTO lease.load_log (file, rows) VALUES (%s, %s)", (path.name, len(rows)))
    return len(rows)


def main():
    seen = {}
    while True:
        try:
            with psycopg.connect("", autocommit=True) as conn:
                conn.execute(SCHEMA_SQL.read_text())
                for table in TABLES:
                    path = DATA_DIR / f"{table}.csv"
                    if not path.exists():
                        continue
                    mtime = path.stat().st_mtime
                    if seen.get(table) == mtime:
                        continue
                    time.sleep(3)                       # skip a file still being copied in
                    if path.stat().st_mtime != mtime:
                        continue
                    n = load_file(conn, table, path)
                    seen[table] = mtime
                    log.info("Loaded %s: %d rows", path.name, n)
        except Exception as e:  # keep the previous data and retry next round
            log.error("Lease load failed, keeping previous data: %s", e)
        sys.stdout.flush()
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
