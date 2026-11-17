import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.m7_receivables import ledger


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    import app.store as store
    import app.modules.m7_receivables.ledger as ledger_module
    path = tmp_path / "api.sqlite3"
    monkeypatch.setattr(store, "DATABASE_PATH", path)
    monkeypatch.setattr(ledger_module, "connect", store.connect)
    store._READY_PATHS.clear()
    store.init_db()
    yield
    store._READY_PATHS.clear()


@pytest.fixture
def client():
    return TestClient(app)


def seed():
    ledger.register_invoice({"invoice_no": "IV-100", "supplier_code": "000031", "amount": "1000.00", "sku": "SKU-A", "tax_rate": "0.13"})
    ledger.register_receipt({"receipt_id": "GR-1", "supplier_code": "000031", "sku": "SKU-A", "tax_rate": "0.13", "amount": "600.00"})
    ledger.register_receipt({"receipt_id": "GR-2", "supplier_code": "000031", "sku": "SKU-A", "tax_rate": "0.13", "amount": "400.00"})


def test_pages_render_with_real_ledger(client):
    seed()
    ledger.assign({"invoice_no": "IV-100", "transferor_code": "000031", "transferee_code": "F1", "amount": "400.00"})
    ledger.pay({"invoice_no": "IV-100", "payee_code": "F1", "amount": "100.00"})
    for path in ("/receivables", "/matching"):
        response = client.get(path)
        assert response.status_code == 200
    assert "F1" in client.get("/receivables").text
    assert "IV-100" in client.get("/matching").text
    assert "GR-1" in client.get("/matching").text


def test_http_assignment_and_payment_flow(client):
    seed()
    assigned = client.post("/api/receivables/assignments", json={"invoice_no": "IV-100", "transferor_code": "000031", "transferee_code": "F1", "amount": "400.00"})
    assert assigned.status_code == 200
    assert assigned.json()["balances"] == {"transferor": "600.00", "transferee": "400.00"}
    paid = client.post("/api/receivables/payments", json={"invoice_no": "IV-100", "payee_code": "F1", "amount": "400.00"})
    assert paid.status_code == 200 and paid.json()["payee_locked"]
    review = client.get("/api/receivables/invoices/IV-100").json()
    assert {row["creditor_code"]: row["unpaid"] for row in review["creditor_balances"]} == {"000031": "600.00", "F1": "0.00"}
    assert review["checks"]["debt_preserved"] and review["checks"]["receipts_preserved"]
    assert review["balance_equation"]["balanced"]


def test_idempotency_key_header_replays(client):
    seed()
    headers = {"Idempotency-Key": "abc-123"}
    body = {"invoice_no": "IV-100", "payee_code": "000031", "amount": "100.00"}
    first = client.post("/api/receivables/payments", json=body, headers=headers)
    second = client.post("/api/receivables/payments", json=body, headers=headers)
    assert first.status_code == second.status_code == 200
    assert second.json()["replayed"] is True
    assert second.json()["receipt_id"] == first.json()["receipt_id"]
    assert len(client.get("/api/receivables/invoices/IV-100").json()["payments"]) == 1


def test_failure_returns_restore_pointer_and_retry_does_not_duplicate(client):
    seed()
    body = {"invoice_no": "IV-100", "payee_code": "000031", "amount": "9999.00", "request_id": "req-bad"}
    failed = client.post("/api/receivables/payments", json=body)
    assert failed.status_code == 422
    detail = failed.json()["detail"]
    assert detail["code"] == "INSUFFICIENT_BALANCE"
    assert detail["request_id"] == "req-bad"
    restore_url = detail["restore_url"]
    restored = client.get(restore_url).json()
    assert restored["request"] == body and restored["restorable"]
    assert client.get("/api/receivables/requests/req-bad").json()["result"] is None
    assert client.get("/api/receivables/requests/req-bad").json()["attempt"]["status"] == "failed"
    retried = client.post("/api/receivables/payments", json={**restored["request"], "amount": "100.00"})
    assert retried.status_code == 200
    again = client.post("/api/receivables/payments", json={**restored["request"], "amount": "100.00"})
    assert again.json()["replayed"] is True
    review = client.get("/api/receivables/invoices/IV-100").json()
    assert len(review["payments"]) == 1 and review["claims"][0]["paid"] == "100.00"


def test_stale_expected_balance_returns_409(client):
    seed()
    client.post("/api/receivables/payments", json={"invoice_no": "IV-100", "payee_code": "000031", "amount": "100.00"})
    response = client.post("/api/receivables/assignments", json={"invoice_no": "IV-100", "transferor_code": "000031", "transferee_code": "F1", "amount": "100.00", "expected_balance": "1000.00"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "CONCURRENT_MODIFICATION"
    assert response.json()["detail"]["actual_balance"] == "900.00"


def test_invoice_supplier_change_rejected_over_http(client):
    seed()
    response = client.post("/api/receivables/invoices", json={"invoice_no": "IV-100", "supplier_code": "999999", "amount": "1000.00", "sku": "SKU-A", "tax_rate": "0.13"})
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "INVOICE_CONFLICT"


def test_matching_endpoint_uses_real_invoice(client):
    seed()
    ledger.assign({"invoice_no": "IV-100", "transferor_code": "000031", "transferee_code": "F1", "amount": "400.00"})
    ledger.pay({"invoice_no": "IV-100", "payee_code": "F1", "amount": "400.00"})
    result = client.post("/api/matches/solve", json={"invoice_no": "IV-100"}).json()
    assert result["invoice_no"] == "IV-100"
    assert result["matched"] == ["GR-1", "GR-2"]
    assert result["amount"] == "1000.00"
    assert {row["creditor_code"]: row["unpaid"] for row in result["creditor_balances"]} == {"000031": "600.00", "F1": "0.00"}
    assert result["chains"][0]["path"][0]["transferee_code"] == "F1"
    assert result["checks"]["debt_preserved"]


def test_unknown_invoice_404(client):
    response = client.get("/api/receivables/invoices/NOPE")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "UNKNOWN_INVOICE"


def test_empty_overview_is_valid(client):
    data = client.get("/api/receivables").json()
    assert data["invoices"] == [] and data["totals"]["unpaid"] == "0.00"


def test_two_identical_posts_without_key_are_distinct_intents(client):
    seed()
    first = client.post("/api/receivables/payments", json={"invoice_no": "IV-100", "payee_code": "000031", "amount": "10.00"})
    second = client.post("/api/receivables/payments", json={"invoice_no": "IV-100", "payee_code": "000031", "amount": "10.00"})
    assert first.json()["receipt_id"] != second.json()["receipt_id"]
    assert first.json()["replayed"] is False and second.json()["replayed"] is False
    review = client.get("/api/receivables/invoices/IV-100").json()
    assert len(review["payments"]) == 2 and review["claims"][0]["paid"] == "20.00"
