#!/bin/bash
# Runs once, the first time the Postgres container starts with an empty volume.
set -euo pipefail
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname finance <<SQL
CREATE DATABASE metabase;
CREATE SCHEMA IF NOT EXISTS finance;
CREATE ROLE finance_reader LOGIN PASSWORD '${PG_READER_PASSWORD}';
GRANT CONNECT ON DATABASE finance TO finance_reader;
GRANT USAGE ON SCHEMA finance TO finance_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA finance GRANT SELECT ON TABLES TO finance_reader;
SQL
