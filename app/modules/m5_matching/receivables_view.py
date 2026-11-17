"""Glue between the matching page and the real receivable ledger.

The matching algorithm itself (``candidates`` / ``solver``) is untouched.
This module only feeds it real invoices and goods receipts and joins the
result with the assignment chains and payment receipts stored by M7, so an
accountant can check every creditor's balance together with the chain that
produced it.
"""
from decimal import Decimal

from app.modules.m7_receivables import ledger as receivables
from app.modules.m7_receivables.money import text_to_cents

from .candidates import grouped_candidates
from .solver import matched_total, subset_match


def _receipt_rows(invoice: dict) -> list[dict]:
    rows = receivables.receipts_for(invoice["sku"], invoice["tax_rate"])
    return [
        {
            "id": row["receipt_id"],
            "receipt_id": row["receipt_id"],
            "sku": row["sku"],
            "tax_rate": row["tax_rate"],
            "amount": row["amount"],
            "supplier_code": row["supplier_code"],
        }
        for row in rows
    ]


def match_invoice(review: dict) -> dict:
    """Run the untouched M5 solver over real receipts for one invoice."""
    receipts = _receipt_rows(review)
    candidates = grouped_candidates(receipts, {"sku": review["sku"], "tax_rate": review["tax_rate"]})
    selected = subset_match(candidates, Decimal(review["amount"]))
    return {
        "invoice_no": review["invoice_no"],
        "invoice": review["invoice_no"],
        "original_supplier_code": review["original_supplier_code"],
        "sku": review["sku"],
        "tax_rate": review["tax_rate"],
        "matched": [row["receipt_id"] for row in selected],
        "candidate_ids": [row["receipt_id"] for row in candidates],
        "amount": str(matched_total(selected)),
        "invoice_amount": review["amount"],
        "creditor_balances": review["creditor_balances"],
        "chains": review["chains"],
        "payments": review["payments"],
        "checks": {**review["checks"], "balance_equation": review["balance_equation"]},
    }


def matching_view(invoice_no: str | None = None) -> dict:
    data = receivables.ledger_overview(invoice_no)
    invoices = []
    for review in data["invoices"]:
        match = match_invoice(review)
        invoices.append({
            "invoice_no": review["invoice_no"],
            "supplier_code": review["original_supplier_code"],
            "sku": review["sku"],
            "tax_rate": review["tax_rate"],
            "amount": review["amount"],
            "receipts": _receipt_rows(review),
            "candidate_ids": match["candidate_ids"],
            "matched_receipt_ids": match["matched"],
            "matched_amount": match["amount"],
            "creditor_balances": review["creditor_balances"],
            "chains": review["chains"],
            "payments": review["payments"],
            "checks": match["checks"],
        })
    return {
        "invoices": invoices,
        "creditors": data["creditors"],
        "chains": data["chains"],
        "payments": data["payments"],
        "totals": data["totals"],
        "checks": data["checks"],
    }


def creditor_totals(view: dict) -> list[dict]:
    """Sum each creditor's outstanding balance across all invoices."""
    grouped: dict[str, dict] = {}
    for invoice in view["invoices"]:
        for balance in invoice["creditor_balances"]:
            entry = grouped.setdefault(balance["creditor_code"], {"creditor_code": balance["creditor_code"], "name": balance["name"], "unpaid_cents": 0, "paid_cents": 0, "invoices": []})
            entry["unpaid_cents"] += text_to_cents(balance["unpaid"])
            entry["paid_cents"] += text_to_cents(balance["paid"])
            entry["invoices"].append(invoice["invoice_no"])
    return [
        {
            "creditor_code": entry["creditor_code"],
            "name": entry["name"],
            "unpaid": receivables.format_cents(entry["unpaid_cents"]),
            "paid": receivables.format_cents(entry["paid_cents"]),
            "invoices": entry["invoices"],
        }
        for entry in sorted(grouped.values(), key=lambda item: item["creditor_code"])
    ]
