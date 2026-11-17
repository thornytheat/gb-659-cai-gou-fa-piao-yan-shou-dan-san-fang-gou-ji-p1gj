from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.errors import DomainError
from app.web import page

from .service import assign, create_creditor, create_invoice, create_receipt, demo, overview, pay, request_status, restore, review

router = APIRouter()


@router.get("/receivables", response_class=HTMLResponse)
def view(request: Request, invoice_no: str | None = None):
    data = overview(invoice_no)
    rows = [
        {
            "key": item["invoice_no"],
            "status": item["original_supplier_code"],
            "value": item["amount"],
        }
        for item in data["invoices"]
    ]
    return page(
        request,
        "m7.html",
        "债权转让与付款",
        "/api/receivables/check",
        rows,
        overview=data,
        invoice_no=invoice_no,
    )


@router.get("/api/receivables")
def read_overview(invoice_no: str | None = None):
    return overview(invoice_no)


@router.get("/api/receivables/ledger")
def read_ledger(invoice_no: str | None = None):
    return overview(invoice_no)


@router.post("/api/receivables/check")
def check_ledger():
    return demo()


@router.post("/api/receivables/demo")
def post_demo():
    return demo()


@router.get("/api/receivables/invoices/{invoice_no}")
def read_invoice(invoice_no: str):
    return review(invoice_no)


@router.post("/api/receivables/creditors")
async def post_creditor(request: Request):
    return create_creditor(await _json(request))


@router.post("/api/receivables/invoices")
async def post_invoice(request: Request):
    return create_invoice(await _json(request))


@router.post("/api/receivables/receipts")
async def post_receipt(request: Request):
    return create_receipt(await _json(request))


@router.post("/api/receivables/assignments")
async def post_assignment(request: Request):
    return assign(await _json(request))


@router.post("/api/receivables/payments")
async def post_payment(request: Request):
    return pay(await _json(request))


@router.get("/api/receivables/requests/{request_id}")
def read_request(request_id: str):
    return request_status(request_id)


@router.get("/api/receivables/requests/{request_id}/restore")
def restore_request(request_id: str):
    return restore(request_id)


async def _json(request: Request) -> dict:
    body = await request.body()
    if not body.strip():
        return {}
    try:
        payload = await request.json()
    except ValueError as exc:
        raise DomainError("BAD_JSON", "请求体不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise DomainError("BAD_JSON", "请求体必须是 JSON 对象")
    header = (request.headers.get("idempotency-key") or "").strip()
    if header and not payload.get("request_id"):
        payload = {**payload, "request_id": header}
    return payload
