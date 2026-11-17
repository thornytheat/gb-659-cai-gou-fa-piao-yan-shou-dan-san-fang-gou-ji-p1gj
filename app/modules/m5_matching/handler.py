from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.errors import DomainError
from app.modules.m7_receivables import ledger as receivables
from app.web import page

from .receivables_view import creditor_totals, match_invoice, matching_view
from .service import demo

router = APIRouter()


@router.get("/matching", response_class=HTMLResponse)
def view(request: Request, invoice_no: str | None = None):
    data = matching_view(invoice_no)
    rows = [
        {"key": item["invoice_no"], "status": f'{len(item["matched_receipt_ids"])} 对 {len(item["candidate_ids"])}', "value": item["amount"]}
        for item in data["invoices"]
    ]
    return page(
        request,
        "m5.html",
        "多对多匹配",
        "/api/matches/solve",
        rows,
        overview=data,
        creditors=creditor_totals(data),
        invoice_no=invoice_no,
    )


@router.post("/api/matches/solve")
async def execute(request: Request):
    body = await _json(request)
    invoice_no = (body.get("invoice_no") or body.get("invoice") or "").strip()
    if not invoice_no:
        return demo()
    return match_invoice(receivables.invoice_review(invoice_no))


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
    return payload
