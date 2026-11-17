import sqlite3

from app.config import DATABASE_PATH


_READY_PATHS: set[str] = set()


def connect():
    """Open a connection with the schema guaranteed, even when the app
    lifespan did not run (tests, one-off scripts)."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATABASE_PATH, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout = 10000")
    db.execute("PRAGMA foreign_keys = ON")
    key = str(DATABASE_PATH)
    if key not in _READY_PATHS:
        db.executescript(SCHEMA)
        _READY_PATHS.add(key)
    return db


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    input_sha256 TEXT NOT NULL,
    accepted INTEGER NOT NULL,
    quarantined INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS creditors (
    creditor_code TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    invoice_no TEXT PRIMARY KEY,
    supplier_code TEXT NOT NULL,
    sku TEXT NOT NULL DEFAULT '',
    tax_rate TEXT NOT NULL DEFAULT '',
    invoice_date TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT NOT NULL UNIQUE,
    supplier_code TEXT NOT NULL,
    sku TEXT NOT NULL DEFAULT '',
    tax_rate TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER NOT NULL,
    received_date TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS claims (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    claim_id TEXT NOT NULL UNIQUE,
    invoice_no TEXT NOT NULL,
    creditor_code TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    paid_cents INTEGER NOT NULL DEFAULT 0,
    origin_assignment_id TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_claims_invoice_creditor
    ON claims (invoice_no, creditor_code);
CREATE TABLE IF NOT EXISTS assignments (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id TEXT NOT NULL UNIQUE,
    invoice_no TEXT NOT NULL,
    transferor_code TEXT NOT NULL,
    transferee_code TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    parent_assignment_id TEXT,
    source_claims TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assignments_invoice
    ON assignments (invoice_no);
CREATE TABLE IF NOT EXISTS payments (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id TEXT NOT NULL UNIQUE,
    invoice_no TEXT NOT NULL,
    payee_code TEXT NOT NULL,
    original_supplier_code TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    allocations TEXT NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_invoice
    ON payments (invoice_no);
CREATE TABLE IF NOT EXISTS request_attempts (
    request_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    error_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def init_db():
    db = connect()
    try:
        db.executescript(SCHEMA)
    finally:
        db.close()


def record_run(manifest: dict) -> bool:
    init_db()
    db = connect()
    try:
        before = db.total_changes
        db.execute("INSERT OR IGNORE INTO runs VALUES (?, ?, ?, ?)", (manifest["run_id"], manifest["input_sha256"], manifest["accepted"], manifest["quarantined"]))
        return db.total_changes > before
    finally:
        db.close()


def run_count() -> int:
    init_db()
    db = connect()
    try:
        return db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    finally:
        db.close()
