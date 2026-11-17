from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from app.errors import DomainError

CENT = Decimal("0.01")
NULLS = {"", "-", "N/A", "NULL"}


def to_cents(value, allow_zero: bool = False) -> int:
    """Parse a user supplied amount to integer cents without float error."""
    token = str(value).strip().replace(",", "").replace("，", "").replace("￥", "")
    if token in NULLS:
        raise DomainError("INVALID_AMOUNT", "金额不能为空")
    try:
        amount = Decimal(token)
    except (InvalidOperation, ValueError) as exc:
        raise DomainError("INVALID_AMOUNT", f"金额格式不正确：{value}") from exc
    if not amount.is_finite():
        raise DomainError("INVALID_AMOUNT", f"金额格式不正确：{value}")
    if amount < 0 or (amount == 0 and not allow_zero):
        raise DomainError("INVALID_AMOUNT", "金额必须大于零")
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def format_cents(cents: int) -> str:
    return str((Decimal(str(cents)) / 100).quantize(CENT, rounding=ROUND_HALF_UP))


def clean_code(value, field: str) -> str:
    """Codes stay strings so leading zeroes survive (TRAP-08)."""
    if value is None:
        raise DomainError("EMPTY_VALUE", f"{field} 不能为空")
    token = str(value).strip()
    if not token:
        raise DomainError("EMPTY_VALUE", f"{field} 不能为空")
    if len(token) > 64:
        raise DomainError("BAD_FORMAT", f"{field} 过长")
    return token


def text_to_cents(value: str) -> int:
    """Read a formatted amount (including 0.00) back to cents."""
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def expected_cents(value):
    return None if value in (None, "") else to_cents(value, allow_zero=True)
