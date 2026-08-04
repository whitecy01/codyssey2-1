"""도메인 모델(데이터 구조) 계층.

저장소/서비스/CLI 어느 계층에서도 같은 타입을 주고받도록 dataclass로 정의한다.
`from_dict` / `to_dict` 는 JSONL 한 줄과 객체 사이의 변환 계약을 담당하며,
읽을 때도 검증을 수행해 손상된 레코드가 그대로 흘러 들어오지 않게 한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import ValidationError
from .validators import (
    parse_amount,
    parse_category_name,
    parse_date,
    parse_month,
    parse_tags,
    parse_type,
)


@dataclass(slots=True)
class Transaction:
    """거래 내역 한 건."""

    id: str
    date: str  # YYYY-MM-DD
    type: str  # income | expense
    category: str
    amount: int  # 양수
    memo: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def month(self) -> str:
        """'YYYY-MM' 형태의 소속 월."""
        return self.date[:7]

    @property
    def sort_key(self) -> tuple[str, str]:
        """최신순 정렬 기준(날짜 → id)."""
        return (self.date, self.id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "date": self.date,
            "type": self.type,
            "category": self.category,
            "amount": self.amount,
            "memo": self.memo,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Transaction:
        """저장된 레코드(dict)를 검증하며 객체로 복원한다."""
        try:
            identifier = str(raw["id"]).strip()
        except KeyError:
            raise ValidationError("거래 레코드에 id가 없습니다.", "손상된 줄일 수 있습니다.") from None
        if not identifier:
            raise ValidationError("거래 id가 비어 있습니다.", "손상된 줄일 수 있습니다.")
        return cls(
            id=identifier,
            date=parse_date(str(raw.get("date", ""))),
            type=parse_type(str(raw.get("type", ""))),
            category=parse_category_name(str(raw.get("category", ""))),
            amount=parse_amount(raw.get("amount", "")),
            memo=str(raw.get("memo", "") or ""),
            tags=parse_tags(raw.get("tags")),
        )


@dataclass(slots=True)
class Category:
    """지출/수입 분류."""

    name: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Category:
        return cls(
            name=parse_category_name(str(raw.get("name", ""))),
            created_at=str(raw.get("created_at", "") or ""),
        )


@dataclass(slots=True)
class Budget:
    """월 예산."""

    month: str  # YYYY-MM
    amount: int
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"month": self.month, "amount": self.amount, "updated_at": self.updated_at}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Budget:
        return cls(
            month=parse_month(str(raw.get("month", ""))),
            amount=parse_amount(raw.get("amount", "")),
            updated_at=str(raw.get("updated_at", "") or ""),
        )


@dataclass(slots=True)
class MonthlySummary:
    """summary 명령의 계산 결과(출력 형식과 분리된 순수 데이터)."""

    month: str
    total_income: int
    total_expense: int
    count: int
    expense_by_category: dict[str, int]
    budget: int | None = None

    @property
    def balance(self) -> int:
        return self.total_income - self.total_expense

    @property
    def usage_rate(self) -> float | None:
        """예산 대비 지출 사용률(%)."""
        if not self.budget:
            return None
        return self.total_expense / self.budget * 100

    @property
    def is_over_budget(self) -> bool:
        return self.budget is not None and self.total_expense > self.budget

    def top_expenses(self, top_n: int) -> list[tuple[str, int]]:
        """지출 합계 상위 N개 카테고리."""
        ordered = sorted(self.expense_by_category.items(), key=lambda kv: (-kv[1], kv[0]))
        return ordered[:top_n] if top_n > 0 else ordered
