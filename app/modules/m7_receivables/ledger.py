"""Receivable ledger.

The original debt stays frozen on the invoice: invoice_no, supplier and
amount never change after registration, so the three-way review (purchase /
goods receipt / invoice) keeps pointing at the same supplier.

Collection rights live in ``claims``. A claim belongs to exactly one
creditor and is never re-pointed; an assignment shrinks the transferor's
unpaid claim amount and creates a child claim for the transferee. That
supports partial assignment and assignment of a claim that was itself
acquired by assignment.

Payment receipts are immutable. They remember the payee of the moment they
were posted (``payee_code``) and the supplier printed on the invoice
(``original_supplier_code``), so assigning the remaining balance later
cannot rewrite who was already paid.

All balances are integer cents; the DB is the single source of truth.
"""
import hashlib
import json
from decimal import Decimal
import sqlite3
from uuid import uuid4

from app.clock import now_iso
from app.errors import DomainError
from app.store import connect

from .money import clean_code, expected_cents, format_cents, text_to_cents, to_cents


# ------------------------------------------------------------------ helpers
def _rollback(db) -> None:
    try:
        db.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


def _row(db, sql: str, params: tuple = ()):
    return db.execute(sql, params).fetchone()


def _rows(db, sql: str, params: tuple = ()):
    return db.execute(sql, params).fetchall()


def _invoice(db, invoice_no: str):
    row = _row(db, "SELECT * FROM invoices WHERE invoice_no = ?", (invoice_no,))
    if row is None:
        raise DomainError("UNKNOWN_INVOICE", f"发票不存在：{invoice_no}", status=404)
    return row


def _ensure_creditor(db, code: str, name: str = "") -> None:
    code = clean_code(code, "债权人编码")
    existing = _row(db, "SELECT * FROM creditors WHERE creditor_code = ?", (code,))
    if existing is None:
        db.execute(
            "INSERT INTO creditors (creditor_code, name, created_at) VALUES (?, ?, ?)",
            (code, name or "", now_iso()),
        )
    elif name and not existing["name"]:
        db.execute("UPDATE creditors SET name = ? WHERE creditor_code = ?", (name, code))


def _claims_of(db, invoice_no: str, creditor_code: str | None = None):
    sql = "SELECT * FROM claims WHERE invoice_no = ?"
    params: list = [invoice_no]
    if creditor_code is not None:
        sql += " AND creditor_code = ?"
        params.append(creditor_code)
    sql += " ORDER BY seq"
    return _rows(db, sql, tuple(params))


def _unpaid_cents(claim) -> int:
    return int(claim["amount_cents"]) - int(claim["paid_cents"])


def _balance_cents(db, invoice_no: str, creditor_code: str) -> int:
    return sum(_unpaid_cents(row) for row in _claims_of(db, invoice_no, creditor_code))


def _fingerprint(kind: str, invoice_no: str, from_code: str, to_code: str, amount_cents: int, **extra) -> str:
    payload = {
        "kind": kind,
        "invoice_no": invoice_no,
        "from": from_code,
        "to": to_code,
        "amount_cents": amount_cents,
        **extra,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _journal(db, request_id: str):
    return _row(db, "SELECT * FROM requests WHERE request_id = ?", (request_id,))


def _replay(existing, fingerprint: str, kind: str):
    if existing is None:
        return None
    if existing["kind"] != kind or existing["fingerprint"] != fingerprint:
        raise DomainError(
            "REQUEST_CONFLICT",
            "同一请求标识已用于另一笔不同请求",
            status=409,
            details={"request_id": existing["request_id"], "kind": existing["kind"]},
        )
    return {**json.loads(existing["result_json"]), "replayed": True}


def _replay_from_store(request_id: str, fingerprint: str, kind: str):
    db = connect()
    try:
        return _replay(_journal(db, request_id), fingerprint, kind)
    finally:
        db.close()


def _record_applied(kind: str, request_id: str, fingerprint: str, payload: dict) -> None:
    try:
        db_payload = {key: value for key, value in payload.items() if not str(key).startswith("_")}
        _record_attempt(kind, request_id, fingerprint, db_payload)
    except Exception:
        # The ledger already committed; a failed audit write must not turn a
        # successful posting into an error (a retry replays from requests).
        pass


def _record_failed_attempt(kind: str, payload: dict, error: DomainError) -> None:
    """Remember the exact request body so the caller can restore and retry."""
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        return
    error.details.setdefault("request_id", request_id)
    error.details.setdefault("restore_url", f"/api/receivables/requests/{request_id}/restore")
    try:
        db_payload = {key: value for key, value in payload.items() if not str(key).startswith("_")}
        _record_attempt(kind, request_id, _fingerprint_soft(kind, payload), db_payload, error)
    except Exception:
        # Audit must never mask the business error being reported.
        pass


def _fingerprint_soft(kind: str, payload: dict) -> str:
    try:
        invoice_no = str(payload.get("invoice_no") or payload.get("invoice") or "")
        if kind == "assignment":
            source = payload.get("transferor_code") or payload.get("transferor") or payload.get("from_creditor") or payload.get("from") or ""
            target = payload.get("transferee_code") or payload.get("transferee") or payload.get("to_creditor") or payload.get("to") or ""
        else:
            source = target = payload.get("payee_code") or payload.get("payee") or payload.get("creditor_code") or payload.get("creditor") or ""
        amount = payload.get("amount") if payload.get("amount") is not None else payload.get("amount_cny")
        cents = int((Decimal(str(amount).replace(",", "")) * 100).to_integral_value()) if amount not in (None, "") else 0
        return _fingerprint(kind, invoice_no, str(source), str(target), cents)
    except Exception:
        return ""


def _attempt_status(db, request_id: str):
    return _row(db, "SELECT * FROM request_attempts WHERE request_id = ?", (request_id,))


def _record_attempt(kind: str, request_id: str, fingerprint: str, payload: dict, error: DomainError | None = None) -> None:
    """Remember every attempted posting with its original payload.

    On success the row is marked applied; on failure it keeps the request
    body so the caller can restore and resubmit it. The row itself never
    carries ledger effects - only committed transactions do.
    """
    stamp = now_iso()
    status = "failed" if error is not None else "applied"
    error_json = None if error is None else json.dumps({"code": error.code, "message": error.message, "details": error.details}, ensure_ascii=False)
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "INSERT INTO request_attempts (request_id, kind, fingerprint, status, payload_json, error_json, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(request_id) DO UPDATE SET status = excluded.status, error_json = excluded.error_json, updated_at = excluded.updated_at "
            "WHERE request_attempts.status != 'applied'",
            (request_id, kind, fingerprint, status, json.dumps(payload, ensure_ascii=False), error_json, stamp, stamp),
        )
        db.execute("COMMIT")
    except Exception:
        _rollback(db)
        raise
    finally:
        db.close()


def _attempt_dict(row) -> dict | None:
    if row is None:
        return None
    return {
        "request_id": row["request_id"],
        "kind": row["kind"],
        "status": row["status"],
        "request": json.loads(row["payload_json"]),
        "error": json.loads(row["error_json"]) if row["error_json"] else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "restorable": row["status"] == "failed",
    }


def _echo(kind, invoice_no, from_code, to_code, amount_cents, request_id) -> dict:
    return {
        "kind": kind,
        "invoice_no": invoice_no,
        "from_creditor_code": from_code,
        "to_creditor_code": to_code,
        "amount": format_cents(amount_cents),
        "request_id": request_id,
    }


def _claim_dict(row, creditor_name: str | None = None) -> dict:
    item = {
        "claim_id": row["claim_id"],
        "invoice_no": row["invoice_no"],
        "creditor_code": row["creditor_code"],
        "amount": format_cents(int(row["amount_cents"])),
        "paid": format_cents(int(row["paid_cents"])),
        "unpaid": format_cents(_unpaid_cents(row)),
        "origin_assignment_id": row["origin_assignment_id"],
        "version": int(row["version"]),
    }
    if creditor_name is not None:
        item["creditor_name"] = creditor_name
    return item


def _assignment_dict(row) -> dict:
    return {
        "assignment_id": row["assignment_id"],
        "invoice_no": row["invoice_no"],
        "transferor_code": row["transferor_code"],
        "transferee_code": row["transferee_code"],
        "amount": format_cents(int(row["amount_cents"])),
        "parent_assignment_id": row["parent_assignment_id"],
        "source_claims": json.loads(row["source_claims"]),
        "request_id": row["request_id"],
        "created_at": row["created_at"],
    }


def _payment_dict(row) -> dict:
    return {
        "receipt_id": row["receipt_id"],
        "invoice_no": row["invoice_no"],
        "payee_code": row["payee_code"],
        "original_supplier_code": row["original_supplier_code"],
        "amount": format_cents(int(row["amount_cents"])),
        "allocations": json.loads(row["allocations"]),
        "request_id": row["request_id"],
        "created_at": row["created_at"],
        "payee_locked": True,
    }


def _assignment_rows(db, invoice_no: str):
    return _rows(db, "SELECT * FROM assignments WHERE invoice_no = ? ORDER BY seq", (invoice_no,))


def _payment_rows(db, invoice_no: str):
    return _rows(db, "SELECT * FROM payments WHERE invoice_no = ? ORDER BY seq", (invoice_no,))


def _creditor_names(db) -> dict[str, str]:
    return {row["creditor_code"]: row["name"] for row in _rows(db, "SELECT creditor_code, name FROM creditors")}


def _balances(db, invoice_no: str) -> list[dict]:
    names = _creditor_names(db)
    grouped: dict[str, dict] = {}
    for row in _claims_of(db, invoice_no):
        entry = grouped.setdefault(row["creditor_code"], {"code": row["creditor_code"], "claim": 0, "paid": 0, "unpaid": 0, "claim_ids": []})
        entry["claim"] += int(row["amount_cents"])
        entry["paid"] += int(row["paid_cents"])
        entry["unpaid"] += _unpaid_cents(row)
        entry["claim_ids"].append(row["claim_id"])
    return [
        {
            "creditor_code": entry["code"],
            "name": names.get(entry["code"], ""),
            "claim_ids": entry["claim_ids"],
            "claim_amount": format_cents(entry["claim"]),
            "paid": format_cents(entry["paid"]),
            "unpaid": format_cents(entry["unpaid"]),
        }
        for entry in sorted(grouped.values(), key=lambda item: item["code"])
    ]


def _lineage(db, assignment_id: str, assignments: dict, memo: dict, seen: frozenset = frozenset()) -> list[dict]:
    """Longest assignment path that leads to this assignment.

    A transferor may hold claims acquired through several different
    assignments; the longest branch is returned as the display path while
    ``sources`` in each step keeps the full provenance for auditing.
    """
    if assignment_id in memo:
        return memo[assignment_id]
    if assignment_id in seen or assignment_id not in assignments:
        return []
    row = assignments[assignment_id]
    best: list[dict] = []
    for source in json.loads(row["source_claims"]):
        claim = _row(db, "SELECT * FROM claims WHERE claim_id = ?", (source["claim_id"],))
        if claim is None or not claim["origin_assignment_id"]:
            continue
        branch = _lineage(db, claim["origin_assignment_id"], assignments, memo, seen | {assignment_id})
        if len(branch) > len(best):
            best = branch
    memo[assignment_id] = best + [_assignment_dict(row)]
    return memo[assignment_id]


def _chains(db, invoice_no: str) -> list[dict]:
    assignments = {row["assignment_id"]: row for row in _assignment_rows(db, invoice_no)}
    memo: dict[str, list[dict]] = {}
    result = []
    for claim in _claims_of(db, invoice_no):
        path = _lineage(db, claim["origin_assignment_id"], assignments, memo) if claim["origin_assignment_id"] else []
        if path:
            result.append({
                "claim_id": claim["claim_id"],
                "creditor_code": claim["creditor_code"],
                "claim_amount": format_cents(int(claim["amount_cents"])),
                "unpaid": format_cents(_unpaid_cents(claim)),
                "path": path,
                "depth": len(path),
            })
    return result


def _chain_is_valid(db, invoice_no: str) -> bool:
    """Validate provenance links from source claims to child claims.

    Claims never change owner, so a source claim still showing the
    transferor proves the chain was recorded correctly even after later
    transfers or payments shrank the amounts.
    """
    assignments = {row["assignment_id"]: row for row in _assignment_rows(db, invoice_no)}
    for row in assignments.values():
        if row["transferor_code"] == row["transferee_code"]:
            return False
        sources = json.loads(row["source_claims"])
        if not sources or sum(text_to_cents(item["amount"]) for item in sources) != int(row["amount_cents"]):
            return False
        for item in sources:
            claim = _row(db, "SELECT * FROM claims WHERE claim_id = ?", (item["claim_id"],))
            if claim is None or claim["invoice_no"] != invoice_no or claim["creditor_code"] != row["transferor_code"]:
                return False
        children = _rows(db, "SELECT * FROM claims WHERE origin_assignment_id = ?", (row["assignment_id"],))
        if not children or any(child["creditor_code"] != row["transferee_code"] for child in children):
            return False
        # Children shrink when the transferee reassigns or gets paid, so the
        # live sum can only be at most the assigned amount.
        if sum(int(child["amount_cents"]) for child in children) > int(row["amount_cents"]):
            return False
        if row["parent_assignment_id"] is not None:
            parent = assignments.get(row["parent_assignment_id"])
            if parent is None or parent["transferee_code"] != row["transferor_code"]:
                return False
        # Assignment graph must stay acyclic.
        cursor, steps = row["parent_assignment_id"], set()
        while cursor:
            if cursor in steps:
                return False
            steps.add(cursor)
            parent = assignments.get(cursor)
            if parent is None:
                break
            cursor = parent["parent_assignment_id"]
    return True


# ---------------------------------------------------------------- creditors
def register_creditor(payload: dict) -> dict:
    code = clean_code(payload.get("creditor_code") or payload.get("code"), "债权人编码")
    name = str(payload.get("name") or "").strip()
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        _ensure_creditor(db, code, name)
        db.execute("COMMIT")
    except Exception:
        _rollback(db)
        raise
    finally:
        db.close()
    return {"creditor_code": code, "name": name}


# ----------------------------------------------------------------- invoices
def register_invoice(payload: dict) -> dict:
    invoice_no = clean_code(payload.get("invoice_no") or payload.get("invoice"), "发票号")
    supplier = clean_code(payload.get("supplier_code") or payload.get("supplier"), "供应商编码")
    amount_cents = to_cents(payload.get("amount") if payload.get("amount") is not None else payload.get("amount_cny"))
    sku = str(payload.get("sku") or "").strip()
    tax_rate = str(payload.get("tax_rate") or "").strip()
    invoice_date = str(payload.get("invoice_date") or "").strip()
    fingerprint = _fingerprint(
        "invoice", invoice_no, supplier, supplier, amount_cents,
        sku=sku, tax_rate=tax_rate, invoice_date=invoice_date,
    )
    request_id = str(payload.get("request_id") or f"invoice-{fingerprint[:24]}")

    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        replayed = _replay(_journal(db, request_id), fingerprint, "invoice")
        if replayed:
            _rollback(db)
            return replayed
        _ensure_creditor(db, supplier)
        existing = _row(db, "SELECT * FROM invoices WHERE invoice_no = ?", (invoice_no,))
        if existing is not None:
            unchanged = (
                existing["supplier_code"] == supplier
                and int(existing["amount_cents"]) == amount_cents
                and existing["sku"] == sku
                and existing["tax_rate"] == tax_rate
                and existing["invoice_date"] == invoice_date
            )
            if not unchanged:
                raise DomainError(
                    "INVOICE_CONFLICT",
                    "原始发票债务已登记，供应商与金额不可修改",
                    status=409,
                    details={
                        "invoice_no": invoice_no,
                        "original_supplier_code": existing["supplier_code"],
                        "original_amount": format_cents(int(existing["amount_cents"])),
                    },
                )
            result = _invoice_summary(db, invoice_no, replayed=True)
            _rollback(db)
            return result

        db.execute(
            "INSERT INTO invoices (invoice_no, supplier_code, sku, tax_rate, invoice_date, amount_cents, version, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (invoice_no, supplier, sku, tax_rate, invoice_date, amount_cents, now_iso()),
        )
        claim_id = _new_id("CLM")
        db.execute(
            "INSERT INTO claims (claim_id, invoice_no, creditor_code, amount_cents, paid_cents, origin_assignment_id, version, created_at) "
            "VALUES (?, ?, ?, ?, 0, NULL, 1, ?)",
            (claim_id, invoice_no, supplier, amount_cents, now_iso()),
        )
        result = _invoice_summary(db, invoice_no, replayed=False, claim_id=claim_id)
        db.execute(
            "INSERT INTO requests (request_id, kind, fingerprint, result_json, created_at) VALUES (?, 'invoice', ?, ?, ?)",
            (request_id, fingerprint, json.dumps({**result, "replayed": False}, ensure_ascii=False), now_iso()),
        )
        db.execute("COMMIT")
        return result
    except Exception:
        _rollback(db)
        raise
    finally:
        db.close()


def _invoice_summary(db, invoice_no: str, replayed: bool, claim_id: str | None = None) -> dict:
    invoice = _invoice(db, invoice_no)
    claims = _claims_of(db, invoice_no)
    result = {
        "invoice_no": invoice_no,
        "supplier_code": invoice["supplier_code"],
        "original_supplier_code": invoice["supplier_code"],
        "sku": invoice["sku"],
        "tax_rate": invoice["tax_rate"],
        "invoice_date": invoice["invoice_date"],
        "amount": format_cents(int(invoice["amount_cents"])),
        "paid": format_cents(sum(int(row["paid_cents"]) for row in claims)),
        "unpaid": format_cents(sum(_unpaid_cents(row) for row in claims)),
        "original_supplier_locked": True,
        "replayed": replayed,
    }
    if claim_id is not None:
        result["claim_id"] = claim_id
    return result


# ----------------------------------------------------------------- receipts
def register_receipt(payload: dict) -> dict:
    receipt_id = clean_code(payload.get("receipt_id") or payload.get("doc_no"), "验收单号")
    supplier = clean_code(payload.get("supplier_code") or payload.get("supplier"), "供应商编码")
    amount_cents = to_cents(payload.get("amount") if payload.get("amount") is not None else payload.get("amount_cny"))
    sku = str(payload.get("sku") or "").strip()
    tax_rate = str(payload.get("tax_rate") or "").strip()
    received_date = str(payload.get("received_date") or "").strip()
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        existing = _row(db, "SELECT * FROM receipts WHERE receipt_id = ?", (receipt_id,))
        if existing is not None:
            unchanged = (
                existing["supplier_code"] == supplier
                and int(existing["amount_cents"]) == amount_cents
                and existing["sku"] == sku
                and existing["tax_rate"] == tax_rate
            )
            if not unchanged:
                raise DomainError("RECEIPT_CONFLICT", "验收单已登记且内容不一致", status=409, details={"receipt_id": receipt_id})
            result = {"receipt_id": receipt_id, "supplier_code": supplier, "sku": sku, "tax_rate": tax_rate, "amount": format_cents(amount_cents), "received_date": existing["received_date"], "replayed": True}
            _rollback(db)
            return result
        _ensure_creditor(db, supplier)
        db.execute(
            "INSERT INTO receipts (receipt_id, supplier_code, sku, tax_rate, amount_cents, received_date, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (receipt_id, supplier, sku, tax_rate, amount_cents, received_date, now_iso()),
        )
        db.execute("COMMIT")
    except Exception:
        _rollback(db)
        raise
    finally:
        db.close()
    return {"receipt_id": receipt_id, "supplier_code": supplier, "sku": sku, "tax_rate": tax_rate, "amount": format_cents(amount_cents), "received_date": received_date, "replayed": False}


def receipts_for(sku: str = "", tax_rate: str = "") -> list[dict]:
    db = connect()
    try:
        rows = _rows(db, "SELECT * FROM receipts ORDER BY seq")
    finally:
        db.close()
    result = []
    for row in rows:
        if sku and row["sku"] != sku:
            continue
        if tax_rate and row["tax_rate"] != tax_rate:
            continue
        result.append({
            "receipt_id": row["receipt_id"],
            "supplier_code": row["supplier_code"],
            "sku": row["sku"],
            "tax_rate": row["tax_rate"],
            "amount": format_cents(int(row["amount_cents"])),
            "received_date": row["received_date"],
        })
    return result


# ---------------------------------------------------------------- assignment
def _require_expected_balance(db, invoice_no, creditor_code, expected_balance_cents, request_id, kind, from_code, to_code, amount_cents):
    """Inside the write transaction, confirm the balance the caller saw.

    The conditional claim update already blocks a changed claim; this check
    also catches a concurrent change that landed on another claim of the
    same creditor, which is what "only the side with an unchanged balance
    wins" requires.
    """
    if expected_balance_cents is None:
        return
    actual = _balance_cents(db, invoice_no, creditor_code)
    if actual != expected_balance_cents:
        raise DomainError(
            "CONCURRENT_MODIFICATION",
            "余额已变化，本次请求未生效，请按最新余额重新提交",
            status=409,
            details={
                "expected_balance": format_cents(expected_balance_cents),
                "actual_balance": format_cents(actual),
                "request": _echo(kind, invoice_no, from_code, to_code, amount_cents, request_id),
            },
        )


def _plan_from(claims, amount_cents: int) -> list[dict]:
    plan, remaining = [], amount_cents
    for row in claims:
        take = min(_unpaid_cents(row), remaining)
        if take > 0:
            plan.append({"claim": row, "amount_cents": take, "version": int(row["version"])})
            remaining -= take
        if remaining == 0:
            break
    return plan


def _with_request_id(kind: str, payload: dict) -> dict:
    """Give every posting an explicit idempotency key.

    Callers that supply ``request_id`` (or an ``Idempotency-Key`` header)
    control replay semantics. Without one, each submission is a distinct
    intent and gets a fresh key - two identical ad-hoc payments must both be
    allowed, only an explicit retry may replay.
    """
    if str(payload.get("request_id") or "").strip():
        return payload
    return {**payload, "request_id": f"{kind}-{uuid4().hex}"}


def assign(payload: dict) -> dict:
    payload = _with_request_id("assignment", payload)
    try:
        return _assign(payload)
    except DomainError as exc:
        _record_failed_attempt("assignment", payload, exc)
        raise


def _assign(payload: dict) -> dict:
    invoice_no = clean_code(payload.get("invoice_no") or payload.get("invoice"), "发票号")
    transferor = clean_code(
        payload.get("transferor_code") or payload.get("transferor") or payload.get("from_creditor") or payload.get("from"),
        "转出方编码",
    )
    transferee = clean_code(
        payload.get("transferee_code") or payload.get("transferee") or payload.get("to_creditor") or payload.get("to"),
        "受让方编码",
    )
    if transferor == transferee:
        raise DomainError("SAME_CREDITOR", "转出方与受让方不能相同")
    amount_cents = to_cents(payload.get("amount") if payload.get("amount") is not None else payload.get("amount_cny"))
    request_id = str(payload.get("request_id") or f"assignment-{_fingerprint('assignment', invoice_no, transferor, transferee, amount_cents)[:24]}")
    fingerprint = _fingerprint("assignment", invoice_no, transferor, transferee, amount_cents)
    expected = payload.get("expected_balance")
    expected_balance_cents = expected_cents(expected)

    snapshot = connect()
    try:
        replayed = _replay(_journal(snapshot, request_id), fingerprint, "assignment")
        if replayed:
            return replayed
        invoice = _invoice(snapshot, invoice_no)
        transferor_claims = _claims_of(snapshot, invoice_no, transferor)
        available = sum(_unpaid_cents(row) for row in transferor_claims)
        if expected_balance_cents is not None and available != expected_balance_cents:
            raise DomainError(
                "CONCURRENT_MODIFICATION",
                "余额已变化，转让未生效，请按最新余额重新提交",
                status=409,
                details={"expected_balance": format_cents(expected_balance_cents), "actual_balance": format_cents(available), "request": _echo("assignment", invoice_no, transferor, transferee, amount_cents, request_id)},
            )
        if available <= 0:
            raise DomainError("NO_OPEN_BALANCE", "转出方在该发票上没有未付余额", details={"invoice_no": invoice_no, "transferor_code": transferor})
        if amount_cents > available:
            raise DomainError(
                "INSUFFICIENT_BALANCE",
                "转让金额超过转出方未付余额",
                details={"invoice_no": invoice_no, "transferor_code": transferor, "amount": format_cents(amount_cents), "available": format_cents(available)},
            )
        plan = _plan_from(transferor_claims, amount_cents)
        if sum(item["amount_cents"] for item in plan) != amount_cents:
            raise DomainError("INSUFFICIENT_BALANCE", "转让金额超过转出方未付余额")
        original_supplier = invoice["supplier_code"]
    finally:
        snapshot.close()

    assignment_id = _new_id("ASG")
    result = _apply_assignment(invoice_no, transferor, transferee, amount_cents, fingerprint, request_id, plan, assignment_id, original_supplier, expected_balance_cents)
    _record_applied("assignment", request_id, fingerprint, payload)
    result["replayed"] = False
    return result


def _apply_assignment(invoice_no, transferor, transferee, amount_cents, fingerprint, request_id, plan, assignment_id, original_supplier, expected_balance_cents=None):
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        replayed = _replay(_journal(db, request_id), fingerprint, "assignment")
        if replayed:
            _rollback(db)
            return replayed
        invoice = _invoice(db, invoice_no)
        if invoice["supplier_code"] != original_supplier:
            raise DomainError("CONCURRENT_MODIFICATION", "原始发票供应商已变化，转让未生效", status=409)
        _require_expected_balance(db, invoice_no, transferor, expected_balance_cents, request_id, "assignment", transferor, transferee, amount_cents)
        _ensure_creditor(db, transferor)
        _ensure_creditor(db, transferee)
        source_claims, child_ids, origins = [], [], set()
        for item in plan:
            source = item["claim"]
            take = item["amount_cents"]
            updated = db.execute(
                "UPDATE claims SET amount_cents = amount_cents - ?, version = version + 1 "
                "WHERE claim_id = ? AND version = ? AND amount_cents - paid_cents >= ?",
                (take, source["claim_id"], item["version"], take),
            ).rowcount
            if updated != 1:
                raise DomainError(
                    "CONCURRENT_MODIFICATION",
                    "余额已被另一笔付款或转让改变，本次转让未生效",
                    status=409,
                    details={"invoice_no": invoice_no, "request_id": request_id, "request": _echo("assignment", invoice_no, transferor, transferee, amount_cents, request_id)},
                )
            origins.add(source["origin_assignment_id"])
            child_id = _new_id("CLM")
            child_ids.append(child_id)
            db.execute(
                "INSERT INTO claims (claim_id, invoice_no, creditor_code, amount_cents, paid_cents, origin_assignment_id, version, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?, 1, ?)",
                (child_id, invoice_no, transferee, take, assignment_id, now_iso()),
            )
            source_claims.append({"claim_id": source["claim_id"], "amount": format_cents(take)})
        parent = origins.pop() if len(origins) == 1 else None
        db.execute(
            "INSERT INTO assignments (assignment_id, invoice_no, transferor_code, transferee_code, amount_cents, parent_assignment_id, source_claims, request_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (assignment_id, invoice_no, transferor, transferee, amount_cents, parent, json.dumps(source_claims), request_id, now_iso()),
        )
        result = {
            "assignment_id": assignment_id,
            "invoice_no": invoice_no,
            "transferor_code": transferor,
            "transferee_code": transferee,
            "amount": format_cents(amount_cents),
            "parent_assignment_id": parent,
            "source_claims": source_claims,
            "child_claims": [_claim_dict(row) for row in _rows(db, f"SELECT * FROM claims WHERE claim_id IN ({','.join('?' for _ in child_ids)}) ORDER BY seq", tuple(child_ids))],
            "balances": {
                "transferor": format_cents(_balance_cents(db, invoice_no, transferor)),
                "transferee": format_cents(_balance_cents(db, invoice_no, transferee)),
            },
            "original_supplier_code": invoice["supplier_code"],
            "request_id": request_id,
        }
        db.execute(
            "INSERT INTO requests (request_id, kind, fingerprint, result_json, created_at) VALUES (?, 'assignment', ?, ?, ?)",
            (request_id, fingerprint, json.dumps(result, ensure_ascii=False), now_iso()),
        )
        db.execute("COMMIT")
        return result
    except sqlite3.IntegrityError:
        _rollback(db)
        raced = _replay_from_store(request_id, fingerprint, "assignment")
        if raced is not None:
            return raced
        raise
    except Exception:
        _rollback(db)
        raise
    finally:
        db.close()


# ------------------------------------------------------------------ payment
def pay(payload: dict) -> dict:
    payload = _with_request_id("payment", payload)
    try:
        return _pay(payload)
    except DomainError as exc:
        _record_failed_attempt("payment", payload, exc)
        raise


def _pay(payload: dict) -> dict:
    invoice_no = clean_code(payload.get("invoice_no") or payload.get("invoice"), "发票号")
    payee = clean_code(
        payload.get("payee_code") or payload.get("payee") or payload.get("creditor_code") or payload.get("creditor"),
        "收款人编码",
    )
    amount_cents = to_cents(payload.get("amount") if payload.get("amount") is not None else payload.get("amount_cny"))
    request_id = str(payload.get("request_id") or f"payment-{_fingerprint('payment', invoice_no, payee, payee, amount_cents)[:24]}")
    fingerprint = _fingerprint("payment", invoice_no, payee, payee, amount_cents)
    expected = payload.get("expected_balance")
    expected_balance_cents = expected_cents(expected)

    snapshot = connect()
    try:
        replayed = _replay(_journal(snapshot, request_id), fingerprint, "payment")
        if replayed:
            return replayed
        invoice = _invoice(snapshot, invoice_no)
        payee_claims = _claims_of(snapshot, invoice_no, payee)
        available = sum(_unpaid_cents(row) for row in payee_claims)
        if expected_balance_cents is not None and available != expected_balance_cents:
            raise DomainError(
                "CONCURRENT_MODIFICATION",
                "余额已变化，付款未生效，请按最新余额重新提交",
                status=409,
                details={"expected_balance": format_cents(expected_balance_cents), "actual_balance": format_cents(available), "request": _echo("payment", invoice_no, payee, payee, amount_cents, request_id)},
            )
        if available <= 0:
            raise DomainError("NO_OPEN_BALANCE", "该收款人在此发票上没有未付余额", details={"invoice_no": invoice_no, "payee_code": payee, "original_supplier_code": invoice["supplier_code"]})
        if amount_cents > available:
            raise DomainError(
                "INSUFFICIENT_BALANCE",
                "付款金额超过该收款人未付余额",
                details={"invoice_no": invoice_no, "payee_code": payee, "amount": format_cents(amount_cents), "available": format_cents(available)},
            )
        plan = _plan_from(payee_claims, amount_cents)
        if sum(item["amount_cents"] for item in plan) != amount_cents:
            raise DomainError("INSUFFICIENT_BALANCE", "付款金额超过该收款人未付余额")
        original_supplier = invoice["supplier_code"]
    finally:
        snapshot.close()

    receipt_id = _new_id("RCT")
    result = _apply_payment(invoice_no, payee, original_supplier, amount_cents, fingerprint, request_id, plan, receipt_id, expected_balance_cents)
    _record_applied("payment", request_id, fingerprint, payload)
    result["replayed"] = False
    return result


def _apply_payment(invoice_no, payee, original_supplier, amount_cents, fingerprint, request_id, plan, receipt_id, expected_balance_cents=None):
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        replayed = _replay(_journal(db, request_id), fingerprint, "payment")
        if replayed:
            _rollback(db)
            return replayed
        invoice = _invoice(db, invoice_no)
        if invoice["supplier_code"] != original_supplier:
            raise DomainError("CONCURRENT_MODIFICATION", "原始发票供应商已变化，付款未生效", status=409)
        _require_expected_balance(db, invoice_no, payee, expected_balance_cents, request_id, "payment", payee, payee, amount_cents)
        _ensure_creditor(db, payee)
        allocations = []
        for item in plan:
            source = item["claim"]
            take = item["amount_cents"]
            updated = db.execute(
                "UPDATE claims SET paid_cents = paid_cents + ?, version = version + 1 "
                "WHERE claim_id = ? AND version = ? AND amount_cents - paid_cents >= ?",
                (take, source["claim_id"], item["version"], take),
            ).rowcount
            if updated != 1:
                raise DomainError(
                    "CONCURRENT_MODIFICATION",
                    "余额已被另一笔付款或转让改变，本次付款未生效",
                    status=409,
                    details={"invoice_no": invoice_no, "request_id": request_id, "request": _echo("payment", invoice_no, payee, payee, amount_cents, request_id)},
                )
            allocations.append({"claim_id": source["claim_id"], "amount": format_cents(take)})
        db.execute(
            "INSERT INTO payments (receipt_id, invoice_no, payee_code, original_supplier_code, amount_cents, allocations, request_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (receipt_id, invoice_no, payee, original_supplier, amount_cents, json.dumps(allocations), request_id, now_iso()),
        )
        result = {
            "receipt_id": receipt_id,
            "invoice_no": invoice_no,
            "payee_code": payee,
            "original_supplier_code": original_supplier,
            "amount": format_cents(amount_cents),
            "allocations": allocations,
            "payee_locked": True,
            "balance": format_cents(_balance_cents(db, invoice_no, payee)),
            "request_id": request_id,
        }
        db.execute(
            "INSERT INTO requests (request_id, kind, fingerprint, result_json, created_at) VALUES (?, 'payment', ?, ?, ?)",
            (request_id, fingerprint, json.dumps(result, ensure_ascii=False), now_iso()),
        )
        db.execute("COMMIT")
        return result
    except sqlite3.IntegrityError:
        _rollback(db)
        raced = _replay_from_store(request_id, fingerprint, "payment")
        if raced is not None:
            return raced
        raise
    except Exception:
        _rollback(db)
        raise
    finally:
        db.close()


# --------------------------------------------------------------------- demo
SAMPLE_INVOICE = "IV-DEMO-1"
SAMPLE_SUPPLIER = "000031"
SAMPLE_FACTOR = "FACTOR-A"


def sample_ledger() -> dict:
    """Seed a fixed demo ledger. Every call reuses the same request ids, so
    pressing the sample button twice never adds a claim or a receipt."""
    register_invoice({
        "invoice_no": SAMPLE_INVOICE,
        "supplier_code": SAMPLE_SUPPLIER,
        "sku": "SKU-A",
        "tax_rate": "0.13",
        "invoice_date": "2026-11-01",
        "amount": "1130.00",
        "request_id": "sample-invoice",
    })
    register_creditor({"creditor_code": SAMPLE_FACTOR, "name": "样例保理商"})
    register_receipt({"receipt_id": "GR-DEMO-1", "supplier_code": SAMPLE_SUPPLIER, "sku": "SKU-A", "tax_rate": "0.13", "amount": "600.00", "received_date": "2026-11-02"})
    register_receipt({"receipt_id": "GR-DEMO-2", "supplier_code": SAMPLE_SUPPLIER, "sku": "SKU-A", "tax_rate": "0.13", "amount": "530.00", "received_date": "2026-11-03"})
    _apply_sample_transfer()
    _apply_sample_payment()
    return ledger_overview()


def _apply_sample_transfer() -> None:
    payload = {
        "invoice_no": SAMPLE_INVOICE,
        "transferor_code": SAMPLE_SUPPLIER,
        "transferee_code": SAMPLE_FACTOR,
        "amount": "400.00",
        "request_id": "sample-assignment",
    }
    try:
        assign(payload)
    except DomainError:
        # Already transferred or the sample balance moved on; never fatal.
        pass


def _apply_sample_payment() -> None:
    payload = {
        "invoice_no": SAMPLE_INVOICE,
        "payee_code": SAMPLE_SUPPLIER,
        "amount": "100.00",
        "request_id": "sample-payment",
    }
    try:
        pay(payload)
    except DomainError:
        pass


# -------------------------------------------------------------------- reads
def invoice_review(invoice_no: str) -> dict:
    invoice_no = clean_code(invoice_no, "发票号")
    db = connect()
    try:
        invoice = _invoice(db, invoice_no)
        claims = _claims_of(db, invoice_no)
        assignment_rows = _assignment_rows(db, invoice_no)
        payment_rows = _payment_rows(db, invoice_no)
        assignments = [_assignment_dict(row) for row in assignment_rows]
        payments = [_payment_dict(row) for row in payment_rows]
        chains = _chains(db, invoice_no)
        review = {
            "invoice_no": invoice_no,
            "original_supplier_code": invoice["supplier_code"],
            "sku": invoice["sku"],
            "tax_rate": invoice["tax_rate"],
            "invoice_date": invoice["invoice_date"],
            "amount": format_cents(int(invoice["amount_cents"])),
            "claims": [_claim_dict(row) for row in claims],
            "creditor_balances": _balances(db, invoice_no),
            "assignments": assignments,
            "chains": chains,
            "payments": payments,
            "checks": {
                "debt_preserved": sum(int(row["amount_cents"]) for row in claims) == int(invoice["amount_cents"]),
                "payments_match_claims": sum(int(row["paid_cents"]) for row in claims) == sum(int(row["amount_cents"]) for row in payment_rows),
                "original_supplier_unchanged": all(row["original_supplier_code"] == invoice["supplier_code"] for row in payment_rows),
                "receipts_preserved": _receipts_preserved(db, payment_rows),
                "balances_non_negative": all(_unpaid_cents(row) >= 0 for row in claims),
                "assignment_chain_valid": _chain_is_valid(db, invoice_no),
            },
        }
        review["balance_equation"] = _balance_equation(review)
        return review
    finally:
        db.close()


def _receipts_preserved(db, payments) -> bool:
    for payment in payments:
        allocations = json.loads(payment["allocations"])
        allocated = 0
        for item in allocations:
            claim = _row(db, "SELECT * FROM claims WHERE claim_id = ?", (item["claim_id"],))
            if claim is None or claim["creditor_code"] != payment["payee_code"]:
                return False
            allocated += text_to_cents(item["amount"])
        if allocated != int(payment["amount_cents"]):
            return False
    return True


def _all_invoice_nos(db) -> list[str]:
    return [row["invoice_no"] for row in _rows(db, "SELECT invoice_no FROM invoices ORDER BY invoice_no")]


def ledger_overview(invoice_no: str | None = None) -> dict:
    db = connect()
    try:
        invoice_nos = [clean_code(invoice_no, "发票号")] if invoice_no else _all_invoice_nos(db)
    finally:
        db.close()
    items = [invoice_review(no) for no in invoice_nos]

    creditor_cents: dict[str, dict] = {}
    for item in items:
        for balance in item["creditor_balances"]:
            entry = creditor_cents.setdefault(balance["creditor_code"], {"creditor_code": balance["creditor_code"], "name": balance["name"], "claim": 0, "paid": 0, "unpaid": 0, "invoices": []})
            entry["claim"] += text_to_cents(balance["claim_amount"])
            entry["paid"] += text_to_cents(balance["paid"])
            entry["unpaid"] += text_to_cents(balance["unpaid"])
            entry["invoices"].append({"invoice_no": item["invoice_no"], "unpaid": balance["unpaid"], "paid": balance["paid"]})
    creditors = [
        {
            "creditor_code": entry["creditor_code"],
            "name": entry["name"],
            "claim_amount": format_cents(entry["claim"]),
            "paid": format_cents(entry["paid"]),
            "unpaid": format_cents(entry["unpaid"]),
            "invoices": entry["invoices"],
        }
        for entry in sorted(creditor_cents.values(), key=lambda row: row["creditor_code"])
    ]
    chains = [{"invoice_no": item["invoice_no"], **chain} for item in items for chain in item["chains"]]
    payments = [{"invoice_no": item["invoice_no"], **payment} for item in items for payment in item["payments"]]
    invoice_amount = sum(text_to_cents(item["amount"]) for item in items)
    paid = sum(text_to_cents(payment["amount"]) for payment in payments)
    unpaid = sum(text_to_cents(balance["unpaid"]) for item in items for balance in item["creditor_balances"])
    return {
        "invoices": items,
        "creditors": creditors,
        "chains": chains,
        "payments": payments,
        "totals": {
            "invoice_amount": format_cents(invoice_amount),
            "paid": format_cents(paid),
            "unpaid": format_cents(unpaid),
        },
        "checks": {
            "debt_preserved": all(item["checks"]["debt_preserved"] for item in items),
            "payments_match_claims": all(item["checks"]["payments_match_claims"] for item in items),
            "original_supplier_unchanged": all(item["checks"]["original_supplier_unchanged"] for item in items),
            "receipts_preserved": all(item["checks"]["receipts_preserved"] for item in items),
            "assignment_chain_valid": all(item["checks"]["assignment_chain_valid"] for item in items),
        },
    }


def get_request(request_id: str) -> dict:
    db = connect()
    try:
        row = _journal(db, request_id)
        attempt = _attempt_status(db, request_id)
        if row is None:
            if attempt is None:
                raise DomainError("UNKNOWN_REQUEST", f"请求不存在：{request_id}", status=404)
            return {
                "request_id": request_id,
                "kind": attempt["kind"],
                "fingerprint": attempt["fingerprint"],
                "result": None,
                "status": attempt["status"],
                "attempt": _attempt_dict(attempt),
                "created_at": attempt["created_at"],
            }
        return {
            "request_id": row["request_id"],
            "kind": row["kind"],
            "fingerprint": row["fingerprint"],
            "result": json.loads(row["result_json"]),
            "status": "applied",
            "attempt": _attempt_dict(attempt),
            "created_at": row["created_at"],
        }
    finally:
        db.close()


def restore_request(request_id: str) -> dict:
    """Hand back the original body of a failed posting so it can be retried.

    The request body is returned untouched; nothing is written to the ledger
    and no claim or receipt is created here.
    """
    db = connect()
    try:
        attempt = _attempt_status(db, request_id)
        if attempt is None:
            raise DomainError("UNKNOWN_REQUEST", f"请求不存在：{request_id}", status=404)
        applied = _journal(db, request_id)
        return {
            "request_id": request_id,
            "kind": attempt["kind"],
            "status": "applied" if applied is not None else attempt["status"],
            "request": json.loads(attempt["payload_json"]),
            "error": json.loads(attempt["error_json"]) if attempt["error_json"] else None,
            "restorable": applied is None and attempt["status"] == "failed",
        }
    finally:
        db.close()


# ------------------------------------------------------------------- checks
def _balance_equation(review: dict) -> dict:
    """Every creditor's claim splits into paid + unpaid, and the invoice
    amount equals the sum of everything still owed plus everything paid."""
    rows, valid = [], True
    for balance in review["creditor_balances"]:
        claim = text_to_cents(balance["claim_amount"])
        paid = text_to_cents(balance["paid"])
        unpaid = text_to_cents(balance["unpaid"])
        row_valid = claim == paid + unpaid and unpaid >= 0
        valid = valid and row_valid
        rows.append({
            "creditor_code": balance["creditor_code"],
            "claim_amount": balance["claim_amount"],
            "paid": balance["paid"],
            "unpaid": balance["unpaid"],
            "valid": row_valid,
        })
    total_unpaid = sum(text_to_cents(row["unpaid"]) for row in rows)
    total_paid = sum(text_to_cents(row["paid"]) for row in rows)
    return {
        "creditors": rows,
        "invoice_amount": review["amount"],
        "total_paid": format_cents(total_paid),
        "total_unpaid": format_cents(total_unpaid),
        "balanced": valid and total_paid + total_unpaid == text_to_cents(review["amount"]),
        "unpaid_equals_claims_minus_paid": total_unpaid == sum(text_to_cents(row["claim_amount"]) - text_to_cents(row["paid"]) for row in rows),
    }
