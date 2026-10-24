"""Tests for the Quicken Business & Personal / Simplifi CSV loader."""

import os
import time

import duckdb
import pytest

from app.csv_loader import _parse_amount, _parse_date, load_csv_to_duckdb, resolve_csv_path
from app.mcp_tools import QuickenMCPTools

SAMPLE = '''Date,Payee,Category,Amount,Account,Business/Personal,Exclusion,Tags,Notes
"Oct 21, 2026",Quicken,Software,-59.87,Platinum Card,Personal,,,
"Oct 15, 2026",Gainwell,Personal Income:Paycheck,"3,492.21",360 Checking,Personal,,,
"Oct 14, 2026",Adobe,Software,-21.19,Blue Business Cash(TM),HM Syed Enterprise LLC,,,
"Oct 10, 2026",American Express,Credit Card Payment,-500.00,360 Checking,Personal,,,
"Oct 10, 2026",American Express,Credit Card Payment,500.00,Platinum Card,Personal,,,
"Oct 05, 2026",To Savings,360 Savings,-1000.00,360 Checking,Personal,,,
"Oct 05, 2026",From Checking,360 Checking,1000.00,360 Savings,Personal,,,
"Sep 28, 2026",Coinbase,Uncategorized,-102000,360 Checking,Personal,Excluded,,
"not a date",Junk,Misc,-1,360 Checking,Personal,,,
'''


@pytest.fixture
def db(tmp_path):
    (tmp_path / "export.csv").write_text("﻿" + SAMPLE, encoding="utf-8")  # with BOM
    conn = duckdb.connect(":memory:")
    stats = load_csv_to_duckdb(str(tmp_path / "export.csv"), conn)
    assert stats == {"accounts": 4, "categories": 3, "transactions": 8}
    return conn


@pytest.mark.parametrize("raw,expected", [
    ("Oct 21, 2026", "2026-10-21"),
    ("10/21/2026", "2026-10-21"),
    ("2026-10-21", "2026-10-21"),
    ("garbage", None),
])
def test_parse_date(raw, expected):
    assert _parse_date(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("-59.87", -59.87),
    ("3,492.21", 3492.21),
    ("$1,000.00", 1000.0),
    ("-$45.00", -45.0),
    ("($45.00)", -45.0),
    ("", None),
])
def test_parse_amount(raw, expected):
    assert _parse_amount(raw) == expected


def test_transfers_and_exclusions_flagged(db):
    transfers = db.execute("SELECT COUNT(*) FROM transactions WHERE is_transfer").fetchone()[0]
    excluded = db.execute("SELECT COUNT(*) FROM transactions WHERE excluded").fetchone()[0]
    # 2 card-payment legs + 2 account-to-account legs
    assert transfers == 4
    assert excluded == 1


def test_cash_flow_by_tag(db):
    rows = QuickenMCPTools(db).get_cash_flow()["rows"]
    personal = [r for r in rows if r["business_personal"] == "Personal" and r["month"] == "2026-10"][0]
    business = [r for r in rows if r["business_personal"] == "HM Syed Enterprise LLC"][0]
    assert personal["income"] == pytest.approx(3492.21)
    assert personal["spending"] == pytest.approx(59.87)
    assert business["spending"] == pytest.approx(21.19)
    assert isinstance(personal["transaction_count"], int)


def test_list_transactions_filters(db):
    tools = QuickenMCPTools(db)
    assert tools.list_transactions(account="blue business")["count"] == 1
    assert tools.list_transactions(business_personal="hm syed")["count"] == 1
    assert tools.list_transactions(include_transfers=False)["count"] == 3


def test_run_sql_guards(db):
    db.execute("SET enable_external_access = false")
    tools = QuickenMCPTools(db)
    assert tools.run_sql("WITH x AS (SELECT 1 AS a) SELECT a FROM x")["success"]
    assert tools.run_sql("SELECT created FROM (SELECT 1 AS created)")["success"]
    assert not tools.run_sql("SELECT 1; DROP TABLE accounts")["success"]
    assert not tools.run_sql("COPY transactions TO 'out.csv'")["success"]
    assert not tools.run_sql("SELECT * FROM read_csv('/etc/passwd')")["success"]


def test_folder_uses_newest_csv(tmp_path):
    old, new = tmp_path / "old.csv", tmp_path / "new.csv"
    old.write_text(SAMPLE)
    new.write_text(SAMPLE)
    past = time.time() - 3600
    os.utime(old, (past, past))
    assert resolve_csv_path(str(tmp_path)) == new


def test_missing_required_column(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("Payee,Category\nx,y\n")
    with pytest.raises(ValueError, match="missing required"):
        load_csv_to_duckdb(str(bad), duckdb.connect(":memory:"))
