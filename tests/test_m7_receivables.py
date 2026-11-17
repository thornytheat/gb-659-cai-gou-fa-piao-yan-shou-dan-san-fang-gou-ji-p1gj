import threading

import pytest

from app.errors import DomainError
from app.modules.m7_receivables import ledger


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    import app.store as store
    import app.modules.m7_receivables.ledger as ledger_module
    path = tmp_path / "ledger.sqlite3"
    monkeypatch.setattr(store, "DATABASE_PATH", path)
    monkeypatch.setattr(ledger_module, "connect", store.connect)
    store._READY_PATHS.clear()
    store.init_db()
    yield
    store._READY_PATHS.clear()


def invoice(no="IV-1", supplier="000031", amount="1000.00", sku="SKU-A", rate="0.13"):
    return ledger.register_invoice({"invoice_no": no, "supplier_code": supplier, "amount": amount, "sku": sku, "tax_rate": rate})


def test_registering_invoice_keeps_original_supplier_and_creates_claim():
    result = invoice()
    assert result["supplier_code"] == "000031" and result["unpaid"] == "1000.00" and result["original_supplier_locked"]
    review = ledger.invoice_review("IV-1")
    assert review["claims"][0]["creditor_code"] == "000031"
    assert review["checks"]["debt_preserved"]


def test_invoice_supplier_cannot_be_changed():
    invoice()
    with pytest.raises(DomainError) as exc:
        invoice(supplier="000999")
    assert exc.value.code == "INVOICE_CONFLICT"
    assert ledger.invoice_review("IV-1")["original_supplier_code"] == "000031"


def test_partial_assignment_moves_only_unpaid_balance():
    invoice()
    result = ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "FACTOR-1", "amount": "400.00"})
    assert result["amount"] == "400.00"
    assert result["balances"] == {"transferor": "600.00", "transferee": "400.00"}
    assert result["original_supplier_code"] == "000031"
    review = ledger.invoice_review("IV-1")
    assert review["amount"] == "1000.00"
    assert {b["creditor_code"]: b["unpaid"] for b in review["creditor_balances"]} == {"000031": "600.00", "FACTOR-1": "400.00"}


def test_assignment_cannot_exceed_balance():
    invoice()
    with pytest.raises(DomainError) as exc:
        ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "1000.01"})
    assert exc.value.code == "INSUFFICIENT_BALANCE"


def test_assignment_requires_positive_amount():
    invoice()
    with pytest.raises(DomainError) as exc:
        ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "0"})
    assert exc.value.code == "INVALID_AMOUNT"


def test_same_creditor_rejected():
    invoice()
    with pytest.raises(DomainError) as exc:
        ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "000031", "amount": "1.00"})
    assert exc.value.code == "SAME_CREDITOR"


def test_payment_to_original_supplier_keeps_receipt_payee_after_assignment():
    invoice()
    payment = ledger.pay({"invoice_no": "IV-1", "payee_code": "000031", "amount": "100.00"})
    assert payment["payee_code"] == "000031" and payment["original_supplier_code"] == "000031"
    ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "500.00"})
    review = ledger.invoice_review("IV-1")
    assert review["payments"][0]["payee_code"] == "000031"
    assert review["payments"][0]["receipt_id"] == payment["receipt_id"]
    assert review["checks"]["original_supplier_unchanged"]
    assert review["checks"]["receipts_preserved"]


def test_payment_uses_payee_claim_not_supplier():
    invoice()
    ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "400.00"})
    with pytest.raises(DomainError) as exc:
        ledger.pay({"invoice_no": "IV-1", "payee_code": "F1", "amount": "400.01"})
    assert exc.value.code == "INSUFFICIENT_BALANCE"
    paid = ledger.pay({"invoice_no": "IV-1", "payee_code": "F1", "amount": "400.00"})
    assert paid["balance"] == "0.00"
    review = ledger.invoice_review("IV-1")
    assert {b["creditor_code"]: b["unpaid"] for b in review["creditor_balances"]} == {"000031": "600.00", "F1": "0.00"}


def test_second_assignment_walks_the_chain():
    invoice()
    first = ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "500.00"})
    second = ledger.assign({"invoice_no": "IV-1", "transferor_code": "F1", "transferee_code": "F2", "amount": "200.00"})
    assert second["parent_assignment_id"] == first["assignment_id"]
    review = ledger.invoice_review("IV-1")
    chain = [item for item in review["chains"] if item["creditor_code"] == "F2"][0]
    assert [step["assignment_id"] for step in chain["path"]] == [first["assignment_id"], second["assignment_id"]]
    assert review["checks"]["assignment_chain_valid"]
    assert review["balance_equation"]["balanced"]


def test_assignment_of_paid_portion_rejected():
    invoice()
    ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "500.00"})
    ledger.pay({"invoice_no": "IV-1", "payee_code": "F1", "amount": "300.00"})
    with pytest.raises(DomainError) as exc:
        ledger.assign({"invoice_no": "IV-1", "transferor_code": "F1", "transferee_code": "F2", "amount": "300.00"})
    assert exc.value.code == "INSUFFICIENT_BALANCE"
    review = ledger.invoice_review("IV-1")
    assert {b["creditor_code"]: b["unpaid"] for b in review["creditor_balances"]} == {"000031": "500.00", "F1": "200.00"}


def test_retry_with_same_request_id_does_not_duplicate():
    invoice()
    payload = {"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "100.00", "request_id": "req-1"}
    first = ledger.assign(payload)
    second = ledger.assign(payload)
    assert second["replayed"] is True and second["assignment_id"] == first["assignment_id"]
    review = ledger.invoice_review("IV-1")
    assert len(review["assignments"]) == 1
    assert len([c for c in review["claims"] if c["creditor_code"] == "F1"]) == 1


def test_retry_payment_does_not_duplicate_receipt():
    invoice()
    payload = {"invoice_no": "IV-1", "payee_code": "000031", "amount": "100.00", "request_id": "pay-1"}
    first = ledger.pay(payload)
    second = ledger.pay(payload)
    assert second["replayed"] is True and second["receipt_id"] == first["receipt_id"]
    assert len(ledger.invoice_review("IV-1")["payments"]) == 1
    assert ledger.invoice_review("IV-1")["claims"][0]["paid"] == "100.00"


def test_reusing_request_id_for_different_body_is_rejected():
    invoice()
    ledger.pay({"invoice_no": "IV-1", "payee_code": "000031", "amount": "100.00", "request_id": "dup"})
    with pytest.raises(DomainError) as exc:
        ledger.pay({"invoice_no": "IV-1", "payee_code": "000031", "amount": "200.00", "request_id": "dup"})
    assert exc.value.code == "REQUEST_CONFLICT"


def test_failed_request_can_be_restored_and_retried_without_side_effects():
    invoice()
    bad = {"invoice_no": "IV-1", "payee_code": "000031", "amount": "5000.00", "request_id": "pay-bad"}
    with pytest.raises(DomainError):
        ledger.pay(bad)
    restored = ledger.restore_request("pay-bad")
    assert restored["restorable"] and restored["request"] == bad
    assert ledger.invoice_review("IV-1")["payments"] == []
    retry = ledger.pay({**restored["request"], "amount": "1000.00"})
    assert retry["receipt_id"]
    assert len(ledger.invoice_review("IV-1")["payments"]) == 1
    assert ledger.invoice_review("IV-1")["claims"][0]["paid"] == "1000.00"


def test_expected_balance_guard_rejects_stale_request():
    invoice()
    ledger.pay({"invoice_no": "IV-1", "payee_code": "000031", "amount": "100.00"})
    with pytest.raises(DomainError) as exc:
        ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "100.00", "expected_balance": "1000.00"})
    assert exc.value.code == "CONCURRENT_MODIFICATION"
    assert exc.value.details["actual_balance"] == "900.00"


def test_concurrent_payment_and_assignment_only_untouched_balance_wins():
    """Both requests are built against the same 1000.00 balance; the first
    to commit wins and the second must see a changed balance and roll back."""
    invoice()
    barrier = threading.Barrier(2)
    results = {}

    def payment():
        barrier.wait()
        try:
            results["payment"] = ledger.pay({"invoice_no": "IV-1", "payee_code": "000031", "amount": "1000.00", "expected_balance": "1000.00", "request_id": "race-pay"})
        except DomainError as exc:
            results["payment"] = exc

    def assignment():
        barrier.wait()
        try:
            results["assignment"] = ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "1000.00", "expected_balance": "1000.00", "request_id": "race-asg"})
        except DomainError as exc:
            results["assignment"] = exc

    threads = [threading.Thread(target=payment), threading.Thread(target=assignment)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winner = [name for name, value in results.items() if not isinstance(value, DomainError)]
    loser = [name for name, value in results.items() if isinstance(value, DomainError)]
    assert len(winner) == 1 and len(loser) == 1
    assert results[loser[0]].code == "CONCURRENT_MODIFICATION"

    review = ledger.invoice_review("IV-1")
    if winner[0] == "payment":
        assert review["payments"] and len(review["payments"]) == 1
        assert [a for a in review["assignments"]] == []
    else:
        assert [a for a in review["assignments"]]
        assert review["payments"] == []
    assert review["checks"]["debt_preserved"] and review["balance_equation"]["balanced"]


def test_concurrent_payments_only_one_succeeds():
    invoice()
    barrier = threading.Barrier(2)
    outcomes = []

    def pay(marker):
        barrier.wait()
        try:
            outcomes.append(ledger.pay({"invoice_no": "IV-1", "payee_code": "000031", "amount": "1000.00", "expected_balance": "1000.00", "request_id": f"p-{marker}"}))
        except DomainError as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=pay, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len([row for row in outcomes if not isinstance(row, DomainError)]) == 1
    assert len(ledger.invoice_review("IV-1")["payments"]) == 1


def test_overview_reports_every_creditor_balance_and_chain():
    invoice()
    ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "500.00"})
    ledger.assign({"invoice_no": "IV-1", "transferor_code": "F1", "transferee_code": "F2", "amount": "200.00"})
    ledger.pay({"invoice_no": "IV-1", "payee_code": "000031", "amount": "100.00"})
    data = ledger.ledger_overview()
    assert {row["creditor_code"]: row["unpaid"] for row in data["creditors"]} == {"000031": "400.00", "F1": "300.00", "F2": "200.00"}
    assert data["totals"]["paid"] == "100.00" and data["totals"]["unpaid"] == "900.00"
    assert max(chain["depth"] for chain in data["chains"]) == 2
    assert data["checks"]["debt_preserved"] and data["checks"]["original_supplier_unchanged"]


def test_unknown_invoice_rejected():
    with pytest.raises(DomainError) as exc:
        ledger.invoice_review("NOPE")
    assert exc.value.code == "UNKNOWN_INVOICE"


def test_multi_source_assignment_chain_uses_longest_branch():
    invoice()
    ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "400.00"})
    ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F2", "amount": "200.00"})
    ledger.assign({"invoice_no": "IV-1", "transferor_code": "F1", "transferee_code": "F3", "amount": "100.00"})
    mixed = ledger.assign({"invoice_no": "IV-1", "transferor_code": "F1", "transferee_code": "F2", "amount": "300.00"})
    review = ledger.invoice_review("IV-1")
    assert review["checks"]["assignment_chain_valid"]
    chain = [item for item in review["chains"] if item["claim_id"] == mixed["child_claims"][0]["claim_id"]][0]
    assert chain["depth"] >= 2
    assert chain["path"][-1]["assignment_id"] == mixed["assignment_id"]
    assert review["balance_equation"]["balanced"]


def test_assignment_beyond_paid_portion_cannot_use_paid_amount():
    invoice()
    ledger.pay({"invoice_no": "IV-1", "payee_code": "000031", "amount": "600.00"})
    with pytest.raises(DomainError) as exc:
        ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "500.00"})
    assert exc.value.code == "INSUFFICIENT_BALANCE"
    ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "400.00"})
    review = ledger.invoice_review("IV-1")
    assert {b["creditor_code"]: b["unpaid"] for b in review["creditor_balances"]} == {"000031": "0.00", "F1": "400.00"}
    assert review["checks"]["receipts_preserved"]


def test_assignment_preserves_invoice_amount_and_supplier():
    invoice()
    ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "999.99"})
    review = ledger.invoice_review("IV-1")
    assert review["amount"] == "1000.00"
    assert review["original_supplier_code"] == "000031"
    assert review["checks"]["debt_preserved"]


def test_expected_balance_checked_for_all_claims_of_creditor():
    """A creditor can hold several claims; a concurrent change on any of them
    must invalidate a request built against the old total balance."""
    invoice()
    ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "300.00"})
    # F1 now holds one claim of 300; give it a second claim via a second transfer.
    ledger.assign({"invoice_no": "IV-1", "transferor_code": "000031", "transferee_code": "F1", "amount": "200.00"})
    # Transfer part of the first claim so only claim balances shift, total unchanged.
    ledger.assign({"invoice_no": "IV-1", "transferor_code": "F1", "transferee_code": "F2", "amount": "50.00"})
    with pytest.raises(DomainError) as exc:
        ledger.pay({"invoice_no": "IV-1", "payee_code": "F1", "amount": "100.00", "expected_balance": "500.00", "request_id": "stale-1"})
    assert exc.value.code == "CONCURRENT_MODIFICATION"
    assert exc.value.details["actual_balance"] == "450.00"
    assert not ledger.invoice_review("IV-1")["payments"]
