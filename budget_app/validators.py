"""입력 검증 유틸.

각 함수는 "문자열 → 검증된 값"으로 변환하며, 실패하면 원인과 힌트를 담은
`ValidationError`를 던진다. CLI(대화형/옵션)와 CSV import가 같은 규칙을
공유하도록 한 곳에 모아 두었다.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from .errors import ValidationError

TRANSACTION_TYPES: tuple[str, ...] = ("income", "expense")

# 입력 크기 상한. 무제한이면 출력이 무너지고, 큰 정수는 예산 사용률 계산(float)에서
# OverflowError 를 일으키므로 저장 전에 막는다.
MAX_AMOUNT: int = 10_000_000_000_000_000  # 1경
MAX_MEMO_LENGTH: int = 200
MAX_TAG_LENGTH: int = 20
MAX_TAG_COUNT: int = 10
MAX_CATEGORY_LENGTH: int = 30

_MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")
_ID_PATTERN = re.compile(r"^TX-\d{6}$")  # 6자리 고정(storage.MAX_TRANSACTION_NUMBER 와 짝)


def parse_date(raw: str) -> str:
    """'YYYY-MM-DD' 형식의 날짜 문자열을 검증해 그대로 반환한다."""
    text = (raw or "").strip()
    if not text:
        raise ValidationError("날짜가 비어 있습니다.", "예: 2024-01-15")
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise ValidationError(
            "날짜 형식이 올바르지 않습니다 (YYYY-MM-DD).", "예: 2024-01-15"
        ) from None
    return parsed.isoformat()


def parse_month(raw: str) -> str:
    """'YYYY-MM' 형식의 월 문자열을 검증해 그대로 반환한다."""
    text = (raw or "").strip()
    if not _MONTH_PATTERN.match(text):
        raise ValidationError(
            "월 형식이 올바르지 않습니다 (YYYY-MM).", "예: --month 2024-01"
        )
    month_number = int(text[5:7])
    if not 1 <= month_number <= 12:
        raise ValidationError(
            f"존재하지 않는 월입니다: {text}", "01~12 사이의 값을 사용하세요."
        )
    return text


def parse_amount(raw: str | int) -> int:
    """금액을 양의 정수로 변환한다. 쉼표(1,000)와 공백을 허용한다."""
    if isinstance(raw, int):
        text = str(raw)
    else:
        text = (raw or "").strip().replace(",", "").replace("원", "")
    if not text:
        raise ValidationError("금액이 비어 있습니다.", "예: 15000")
    try:
        amount = int(text)
    except ValueError:
        raise ValidationError(
            f"금액은 정수여야 합니다: {raw!r}", "예: 15000 또는 15,000"
        ) from None
    if amount <= 0:
        raise ValidationError("금액은 0보다 큰 값이어야 합니다.", "예: 15000")
    if amount > MAX_AMOUNT:
        raise ValidationError(
            f"금액이 너무 큽니다 ({len(str(amount))}자리).",
            f"최대 {MAX_AMOUNT:,}원까지 입력할 수 있습니다.",
        )
    return amount


def parse_type(raw: str) -> str:
    """거래 타입(income/expense)을 검증한다."""
    text = (raw or "").strip().lower()
    if text not in TRANSACTION_TYPES:
        raise ValidationError(
            f"허용되지 않는 타입입니다: {raw!r}", "income 또는 expense 중 하나를 입력하세요."
        )
    return text


def parse_tags(raw: str | list[str] | None) -> list[str]:
    """쉼표로 구분된 태그 문자열을 리스트로 변환한다(공백/중복 제거)."""
    if raw is None:
        return []
    items = raw if isinstance(raw, list) else str(raw).split(",")
    tags: list[str] = []
    for item in items:
        tag = str(item).strip()
        if not tag or tag in tags:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise ValidationError(
                f"태그는 {MAX_TAG_LENGTH}자 이하여야 합니다: {tag[:MAX_TAG_LENGTH]}…",
                f"입력값 길이: {len(tag)}",
            )
        tags.append(tag)
    if len(tags) > MAX_TAG_COUNT:
        raise ValidationError(
            f"태그는 최대 {MAX_TAG_COUNT}개까지 지정할 수 있습니다.",
            f"입력한 태그 수: {len(tags)}",
        )
    return tags


def parse_memo(raw: str | None) -> str:
    """메모를 검증한다(선택 항목이므로 빈 값은 허용)."""
    text = (raw or "").strip()
    if len(text) > MAX_MEMO_LENGTH:
        raise ValidationError(
            f"메모는 {MAX_MEMO_LENGTH}자 이하여야 합니다.", f"입력값 길이: {len(text)}"
        )
    return text


def parse_category_name(raw: str) -> str:
    """카테고리명을 검증한다(영문/숫자/한글/_- 허용)."""
    text = (raw or "").strip()
    if not text:
        raise ValidationError("카테고리명이 비어 있습니다.", "예: food")
    if len(text) > MAX_CATEGORY_LENGTH:
        raise ValidationError(
            f"카테고리명은 {MAX_CATEGORY_LENGTH}자 이하여야 합니다.", f"입력값 길이: {len(text)}"
        )
    if "," in text or any(ch.isspace() for ch in text):
        raise ValidationError(
            f"카테고리명에 공백이나 쉼표를 사용할 수 없습니다: {text!r}",
            "예: food, transport, side_income",
        )
    return text


def parse_transaction_id(raw: str) -> str:
    """거래 id 형식(TX-000001)을 검증한다."""
    text = (raw or "").strip().upper()
    if not _ID_PATTERN.match(text):
        raise ValidationError(
            f"거래 id 형식이 올바르지 않습니다: {raw!r}", "예: --id TX-000012"
        )
    return text


def validate_period(date_from: str | None, date_to: str | None) -> tuple[str | None, str | None]:
    """--from / --to 조합을 검증한다."""
    start = parse_date(date_from) if date_from else None
    end = parse_date(date_to) if date_to else None
    if start and end and start > end:
        raise ValidationError(
            f"기간이 뒤집혀 있습니다: {start} ~ {end}", "--from 이 --to 보다 앞선 날짜여야 합니다."
        )
    return start, end


def month_range(month: str) -> tuple[str, str]:
    """'YYYY-MM' → (해당 월 1일, 해당 월 마지막 날) 로 변환한다."""
    parse_month(month)
    year, mon = int(month[:4]), int(month[5:7])
    first = date(year, mon, 1)
    last_day = date(year + (mon == 12), (mon % 12) + 1, 1).toordinal() - 1
    return first.isoformat(), date.fromordinal(last_day).isoformat()
