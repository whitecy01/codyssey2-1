"""출력 포맷터.

서비스 계층은 계산만 하고, "어떻게 보이는가"는 이 모듈이 책임진다.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import MonthlySummary, Transaction


def money(amount: int) -> str:
    """금액 표기."""
    return f"{amount}원"


def format_transaction(transaction: Transaction) -> str:
    """거래 한 건을 한 줄로 표현한다."""
    fields = [
        transaction.id,
        transaction.date,
        transaction.type,
        transaction.category,
        str(transaction.amount),
        transaction.memo,
        ",".join(transaction.tags),
    ]
    return " | ".join(fields).rstrip(" |")


def format_transactions(transactions: Sequence[Transaction]) -> str:
    """거래 목록."""
    return "\n".join(format_transaction(transaction) for transaction in transactions)


def format_summary(summary: MonthlySummary, top_n: int) -> str:
    """월별 요약 출력(예산 사용률/초과 경고 포함)."""
    lines = [
        f"[{summary.month} 요약] 거래 {summary.count}건",
        f"총 수입: {money(summary.total_income)}",
        f"총 지출: {money(summary.total_expense)}",
        f"잔액: {money(summary.balance)}",
    ]

    if summary.budget is None:
        lines.append(f"예산: 미설정 (budget set --month {summary.month} --amount <금액>)")
    else:
        rate = summary.usage_rate or 0.0
        lines.append(f"예산: {money(summary.budget)} (사용률 {rate:.1f}%)")
        if summary.is_over_budget:
            over = summary.total_expense - summary.budget
            lines.append(f"[경고] 예산을 {money(over)} 초과했습니다!")
        elif rate >= 80:
            lines.append(f"[주의] 예산의 {rate:.1f}%를 사용했습니다.")
        else:
            lines.append(f"남은 예산: {money(summary.budget - summary.total_expense)}")

    top = summary.top_expenses(top_n)
    lines.append("")
    if top:
        lines.append(f"지출 TOP {len(top)}")
        lines.extend(
            f"{index}) {name} {money(amount)}"
            for index, (name, amount) in enumerate(top, start=1)
        )
    else:
        lines.append("지출 내역이 없습니다.")
    return "\n".join(lines)
