from . import ledger


def create_creditor(payload: dict) -> dict:
    return ledger.register_creditor(payload or {})


def create_invoice(payload: dict) -> dict:
    return ledger.register_invoice(payload or {})


def create_receipt(payload: dict) -> dict:
    return ledger.register_receipt(payload or {})


def assign(payload: dict) -> dict:
    return ledger.assign(payload or {})


def pay(payload: dict) -> dict:
    return ledger.pay(payload or {})


def review(invoice_no: str) -> dict:
    return ledger.invoice_review(invoice_no)


def overview(invoice_no: str | None = None) -> dict:
    return ledger.ledger_overview(invoice_no)


def request_status(request_id: str) -> dict:
    return ledger.get_request(request_id)


def restore(request_id: str) -> dict:
    return ledger.restore_request(request_id)


def demo() -> dict:
    return ledger.sample_ledger()
