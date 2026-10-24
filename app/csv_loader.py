"""Loader for Quicken Business & Personal / Quicken Simplifi CSV exports.

The web versions of Quicken export transactions as CSV rather than QIF. This
module maps those exports onto the same DuckDB schema the QIF loader uses, and
adds the columns the CSV carries that QIF does not: the account name, the
Business/Personal tag, tags, notes, and the "Exclusion" flag.

Column matching is case-insensitive and tolerant of extra whitespace, so small
differences between export versions do not break loading. Unknown columns are
ignored.
"""

import csv
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from .qif_loader import _create_tables

logger = logging.getLogger(__name__)

# Canonical field -> accepted header names (lower-cased, whitespace-collapsed)
COLUMN_ALIASES = {
    "date": ["date", "transaction date", "posted date"],
    "account": ["account", "account name"],
    "payee": ["payee", "description", "merchant"],
    "category": ["category"],
    "amount": ["amount"],
    "memo": ["notes", "note", "memo"],
    "tags": ["tags", "tag"],
    "business_personal": ["business/personal", "business / personal", "business or personal"],
    "exclusion": ["exclusion", "excluded", "exclude"],
    "number": ["check number", "check #", "check no", "number"],
    "cleared": ["status", "cleared"],
    "original_payee": ["original statement", "original payee", "statement name"],
}

REQUIRED = ("date", "amount")

DATE_FORMATS = (
    "%b %d, %Y",   # Oct 23, 2026  (Simplifi / Business & Personal)
    "%B %d, %Y",   # October 23, 2026
    "%m/%d/%Y",
    "%m/%d/%y",
    "%Y-%m-%d",
    "%d-%b-%Y",
)

# Categories Quicken uses for money moving between your own accounts
TRANSFER_CATEGORIES = {"transfer", "transfers", "credit card payment", "balance adjustment"}


def _norm(header: str) -> str:
    return re.sub(r"\s+", " ", (header or "").strip().lower())


def _map_columns(fieldnames: List[str]) -> Dict[str, str]:
    """Return canonical field -> actual header in this file."""
    by_norm = {_norm(h): h for h in fieldnames if h is not None}
    mapping = {}
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in by_norm:
                mapping[field] = by_norm[alias]
                break
    return mapping


def _parse_date(value: str) -> Optional[str]:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    logger.warning(f"Unrecognized date format: {value!r}")
    return None


def _parse_amount(value: str) -> Optional[float]:
    """Parse amounts like -12.34, $1,234.56, ($45.00), -$45.00."""
    value = (value or "").strip()
    if not value:
        return None
    negative = value.startswith("(") and value.endswith(")")
    cleaned = re.sub(r"[^\d.\-]", "", value)
    if cleaned in ("", "-", "."):
        return None
    try:
        amount = float(cleaned)
    except ValueError:
        logger.warning(f"Unrecognized amount: {value!r}")
        return None
    return -abs(amount) if negative else amount


def _parse_bool(value: str) -> bool:
    return _norm(value) in {"true", "yes", "y", "1", "excluded", "exclude"}


def _account_type(name: str) -> str:
    """Best-effort account type from the account name (CSV has no type column)."""
    n = (name or "").lower()
    if re.search(r"card|visa|mastercard|amex|platinum|discover|cash\(tm\)|blue business", n):
        return "CCard"
    if re.search(r"brokerage|invest|ira|401|roth|robinhood|fidelity|coinbase|kraken", n):
        return "Invst"
    if re.search(r"loan|mortgage|lightstream", n):
        return "Oth L"
    return "Bank"


def resolve_csv_path(path: str) -> Path:
    """Accept a file, or a folder and use the newest .csv in it."""
    p = Path(path)
    if p.is_dir():
        csvs = sorted(p.glob("*.csv"), key=lambda f: f.stat().st_mtime, reverse=True)
        if not csvs:
            raise FileNotFoundError(f"No .csv files in folder: {path}")
        logger.info(f"Using newest export in folder: {csvs[0].name}")
        return csvs[0]
    return p


def parse_csv(csv_path: str) -> Dict[str, List[Dict]]:
    path = resolve_csv_path(csv_path)
    # utf-8-sig strips the BOM some exports start with
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = _map_columns(reader.fieldnames or [])
        missing = [c for c in REQUIRED if c not in cols]
        if missing:
            raise ValueError(
                f"CSV is missing required column(s) {missing}. Found headers: {reader.fieldnames}"
            )
        logger.info(f"CSV column mapping: {cols}")

        get = lambda row, field: (row.get(cols[field]) or "").strip() if field in cols else ""

        transactions = []
        skipped = 0
        for row in reader:
            date = _parse_date(get(row, "date"))
            amount = _parse_amount(get(row, "amount"))
            if date is None or amount is None:
                skipped += 1
                continue
            account = get(row, "account") or None
            transactions.append({
                "tx_id": len(transactions) + 1,
                "date": date,
                "account": account,
                "account_type": _account_type(account) if account else None,
                "payee": get(row, "payee") or None,
                "original_payee": get(row, "original_payee") or None,
                "category": get(row, "category") or None,
                "amount": amount,
                "memo": get(row, "memo") or None,
                "tags": get(row, "tags") or None,
                "business_personal": get(row, "business_personal") or None,
                "excluded": _parse_bool(get(row, "exclusion")),
                "number": get(row, "number") or None,
                "cleared": get(row, "cleared") or None,
            })
        if skipped:
            logger.warning(f"Skipped {skipped} rows with no usable date or amount")

    account_names = sorted({t["account"] for t in transactions if t["account"]})
    for t in transactions:
        cat = (t["category"] or "").strip()
        t["is_transfer"] = cat.lower() in TRANSFER_CATEGORIES or cat in account_names

    # Accounts: the CSV carries no balances, so balance is the net of this export
    accounts = []
    for i, name in enumerate(account_names, 1):
        rows = [t for t in transactions if t["account"] == name]
        accounts.append({
            "account_id": i,
            "name": name,
            "type": _account_type(name),
            "description": "From CSV export; balance = net of exported transactions, not the bank balance",
            "balance": round(sum(t["amount"] for t in rows), 2),
            "credit_limit": None,
            "note": None,
        })

    # Categories: derive from use; "Parent:Child" keeps its parent
    categories = []
    seen = {}
    for t in transactions:
        cat = t["category"]
        if not cat or t["is_transfer"]:
            continue
        seen.setdefault(cat, []).append(t["amount"])
    for i, (name, amounts) in enumerate(sorted(seen.items()), 1):
        income = name.lower().startswith(("income", "personal income", "business income")) or sum(amounts) > 0
        categories.append({
            "category_id": i,
            "name": name,
            "description": None,
            "expense_category": not income,
            "income_category": income,
            "tax_related": False,
            "tax_schedule": None,
            "parent_category": name.split(":", 1)[0] if ":" in name else None,
        })

    logger.info(f"Parsed {len(accounts)} accounts, {len(categories)} categories, "
                f"{len(transactions)} transactions from {path.name}")
    return {"accounts": accounts, "categories": categories, "transactions": transactions,
            "source": str(path)}


def load_csv_to_duckdb(csv_path: str, db_connection) -> Dict[str, int]:
    data = parse_csv(csv_path)
    _create_tables(db_connection)

    db_connection.execute("DELETE FROM accounts")
    db_connection.executemany(
        "INSERT INTO accounts (account_id, name, type, description, balance, credit_limit, note) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(a["account_id"], a["name"], a["type"], a["description"], a["balance"],
          a["credit_limit"], a["note"]) for a in data["accounts"]],
    )

    db_connection.execute("DELETE FROM categories")
    if data["categories"]:
        db_connection.executemany(
            "INSERT INTO categories (category_id, name, description, expense_category, "
            "income_category, tax_related, tax_schedule, parent_category) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(c["category_id"], c["name"], c["description"], c["expense_category"],
              c["income_category"], c["tax_related"], c["tax_schedule"], c["parent_category"])
             for c in data["categories"]],
        )

    db_connection.execute("DELETE FROM transaction_splits")
    db_connection.execute("DELETE FROM transactions")
    db_connection.executemany(
        "INSERT INTO transactions (tx_id, account_type, account, date, payee, original_payee, memo, "
        "amount, cleared, number, category, tags, business_personal, excluded, is_transfer) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(t["tx_id"], t["account_type"], t["account"], t["date"], t["payee"], t["original_payee"],
          t["memo"], t["amount"], t["cleared"], t["number"], t["category"], t["tags"],
          t["business_personal"], t["excluded"], t["is_transfer"]) for t in data["transactions"]],
    )

    return {
        "accounts": len(data["accounts"]),
        "categories": len(data["categories"]),
        "transactions": len(data["transactions"]),
    }


class CsvReloader:
    """Reloads the database when a newer export lands in the data folder.

    Called before each tool call. Cheap when nothing changed (one directory
    listing). A failed reload keeps the previously loaded data.
    """

    def __init__(self, csv_path: str, db_connection):
        self.csv_path = csv_path
        self.db = db_connection
        self.loaded = None      # (path, mtime, size) currently in the database
        self.loaded_at = None
        self.last_error = None
        self.failed = None      # last file that failed to load; not retried until it changes

    def _current(self):
        p = resolve_csv_path(self.csv_path)
        st = p.stat()
        return (str(p), st.st_mtime, st.st_size)

    def mark_loaded(self):
        self.loaded = self._current()
        self.loaded_at = datetime.now().isoformat(timespec="seconds")

    def maybe_reload(self) -> bool:
        try:
            current = self._current()
        except Exception as e:  # folder emptied, share unmounted, etc.
            self.last_error = str(e)
            return False
        if current == self.loaded or current == self.failed:
            return False
        # Skip a file that is still being written (size changing)
        import time
        time.sleep(0.5)
        try:
            if self._current() != current:
                return False
            self.db.execute("BEGIN TRANSACTION")
            stats = load_csv_to_duckdb(current[0], self.db)
            self.db.execute("COMMIT")
        except Exception as e:
            try:
                self.db.execute("ROLLBACK")
            except Exception:
                pass
            self.failed = current
            self.last_error = f"Reload of {Path(current[0]).name} failed, keeping previous data: {e}"
            logger.error(self.last_error)
            return False
        self.loaded = current
        self.loaded_at = datetime.now().isoformat(timespec="seconds")
        self.last_error = None
        logger.info(f"Reloaded {Path(current[0]).name}: {stats}")
        return True

    def status(self) -> Dict:
        row = self.db.execute(
            "SELECT COUNT(*), MIN(date), MAX(date) FROM transactions").fetchone()
        return {
            "file": Path(self.loaded[0]).name if self.loaded else None,
            "file_modified": datetime.fromtimestamp(self.loaded[1]).isoformat(timespec="seconds") if self.loaded else None,
            "loaded_at": self.loaded_at,
            "transactions": row[0],
            "first_date": str(row[1]) if row[1] else None,
            "last_date": str(row[2]) if row[2] else None,
            "last_error": self.last_error,
        }
