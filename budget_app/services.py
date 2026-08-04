"""서비스 계층(비즈니스 규칙).

CLI(입출력)와 저장소(파일 I/O) 사이에서 "무엇이 올바른 동작인가"만 책임진다.
여기서는 print()를 하지 않고, 계산 결과나 도메인 객체만 돌려준다.

스트리밍 원칙: 목록/검색/요약은 저장소의 제너레이터를 소비하며,
정렬이 필요한 경우에도 `heapq.nlargest` 로 상위 N건만 유지해
메모리 사용량을 결과 개수에 비례하도록 묶어 둔다.
"""

from __future__ import annotations

import csv
import heapq
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .decorators import log_call, timed
from .errors import ConflictError, NotFoundError, StorageError, ValidationError
from .models import Budget, Category, MonthlySummary, Transaction
from .storage import Storage
from .validators import (
    parse_amount,
    parse_category_name,
    parse_date,
    parse_month,
    parse_tags,
    parse_type,
)

CSV_COLUMNS: tuple[str, ...] = ("date", "type", "category", "amount", "memo", "tags")


@dataclass(slots=True)
class SearchFilter:
    """검색 조건. 모든 필드는 선택이며, 지정된 조건은 AND 로 결합된다."""

    date_from: str | None = None
    date_to: str | None = None
    category: str | None = None
    type: str | None = None
    query: str | None = None
    tag: str | None = None

    def matches(self, transaction: Transaction) -> bool:
        if self.date_from and transaction.date < self.date_from:
            return False
        if self.date_to and transaction.date > self.date_to:
            return False
        if self.category and transaction.category != self.category:
            return False
        if self.type and transaction.type != self.type:
            return False
        if self.query and self.query.lower() not in transaction.memo.lower():
            return False
        if self.tag and self.tag not in transaction.tags:
            return False
        return True

    @property
    def is_empty(self) -> bool:
        return not any(
            (self.date_from, self.date_to, self.category, self.type, self.query, self.tag)
        )


@dataclass(slots=True)
class ImportResult:
    """CSV 가져오기 결과."""

    imported: int = 0
    skipped: int = 0
    created_categories: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class CategoryService:
    """카테고리 관리 규칙."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    def list(self) -> list[str]:
        return sorted(self._storage.categories.names())

    @log_call
    def add(self, raw_name: str) -> Category:
        name = parse_category_name(raw_name)
        if self._storage.categories.exists(name):
            raise ConflictError(
                f"이미 존재하는 카테고리입니다: {name}",
                "category list 로 현재 목록을 확인하세요.",
            )
        return self._storage.categories.add(name)

    @log_call
    def remove(self, raw_name: str, replace_with: str | None = None) -> tuple[int, str | None]:
        """카테고리를 삭제한다.

        사용 중인 카테고리는 기본적으로 삭제를 막고,
        `replace_with` 가 주어지면 해당 거래들을 대체 카테고리로 옮긴 뒤 삭제한다.
        반환값: (이동된 거래 수, 대체 카테고리명)
        """
        name = parse_category_name(raw_name)
        if not self._storage.categories.exists(name):
            raise NotFoundError(
                f"존재하지 않는 카테고리입니다: {name}",
                "category list 로 현재 목록을 확인하세요.",
            )

        in_use = self._storage.transactions.count_by_category(name)
        moved = 0
        target: str | None = None
        if in_use:
            if not replace_with:
                raise ConflictError(
                    f"'{name}' 카테고리를 사용하는 거래가 {in_use}건 있어 삭제할 수 없습니다.",
                    f"--replace-with <다른 카테고리> 로 대체 카테고리를 지정하세요. "
                    f"예: category remove --name {name} --replace-with etc",
                )
            target = parse_category_name(replace_with)
            if target == name:
                raise ValidationError(
                    "대체 카테고리가 삭제할 카테고리와 같습니다.", "다른 카테고리명을 지정하세요."
                )
            if not self._storage.categories.exists(target):
                raise NotFoundError(
                    f"대체 카테고리가 존재하지 않습니다: {target}",
                    f"먼저 category add 로 '{target}' 을 등록하세요.",
                )
            moved = self._storage.transactions.recategorize(name, target)

        self._storage.categories.remove(name)
        return moved, target

    def ensure_exists(self, raw_name: str) -> str:
        """등록된 카테고리인지 확인하고 정규화된 이름을 반환한다."""
        name = parse_category_name(raw_name)
        if not self._storage.categories.exists(name):
            available = ", ".join(self.list()) or "(없음)"
            raise NotFoundError(
                f"등록되지 않은 카테고리입니다: {name}",
                f"등록된 카테고리: {available} / 새로 만들려면 category add 를 사용하세요.",
            )
        return name


class TransactionService:
    """거래 추가/조회/검색/수정/삭제 규칙."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._categories = CategoryService(storage)

    @log_call
    def add(
        self,
        *,
        raw_date: str,
        raw_type: str,
        raw_category: str,
        raw_amount: str | int,
        memo: str = "",
        raw_tags: str | list[str] | None = None,
    ) -> Transaction:
        transaction = Transaction(
            id=self._storage.transactions.next_id(),
            date=parse_date(raw_date),
            type=parse_type(raw_type),
            category=self._categories.ensure_exists(raw_category),
            amount=parse_amount(raw_amount),
            memo=(memo or "").strip(),
            tags=parse_tags(raw_tags),
        )
        return self._storage.transactions.add(transaction)

    @timed
    def list_recent(self, limit: int = 20) -> list[Transaction]:
        """최신순 목록. 스트리밍하며 상위 N건만 유지한다."""
        return self._top(self._storage.transactions.stream(), limit)

    @timed
    def search(self, criteria: SearchFilter, limit: int = 50) -> list[Transaction]:
        """조건에 맞는 거래를 최신순으로 반환한다(스트리밍 + 상위 N건)."""
        return self._top(self.iter_matching(criteria), limit)

    def iter_matching(self, criteria: SearchFilter) -> Iterator[Transaction]:
        """조건에 맞는 거래를 저장 순서대로 흘려보내는 제너레이터."""
        for transaction in self._storage.transactions.stream():
            if criteria.matches(transaction):
                yield transaction

    @staticmethod
    def _top(source: Iterable[Transaction], limit: int) -> list[Transaction]:
        """limit<=0 이면 전체 정렬, 아니면 힙으로 상위 N건만 메모리에 유지."""
        if limit and limit > 0:
            return heapq.nlargest(limit, source, key=lambda item: item.sort_key)
        return sorted(source, key=lambda item: item.sort_key, reverse=True)

    def get(self, transaction_id: str) -> Transaction:
        transaction = self._storage.transactions.get(transaction_id)
        if transaction is None:
            raise NotFoundError(
                f"해당 id의 거래가 없습니다: {transaction_id}",
                "list 명령으로 존재하는 id를 확인하세요.",
            )
        return transaction

    @log_call
    def update(self, transaction_id: str, changes: dict[str, Any]) -> Transaction:
        """옵션으로 전달된 필드만 수정한다(빈 dict 는 오류)."""
        current = self.get(transaction_id)
        if not changes:
            raise ValidationError(
                "수정할 항목이 없습니다.",
                "--date/--type/--category/--amount/--memo/--tags 중 하나 이상을 지정하세요.",
            )

        updated = Transaction(**{**current.to_dict(), "tags": list(current.tags)})
        for field_name, raw_value in changes.items():
            match field_name:
                case "date":
                    updated.date = parse_date(str(raw_value))
                case "type":
                    updated.type = parse_type(str(raw_value))
                case "category":
                    updated.category = self._categories.ensure_exists(str(raw_value))
                case "amount":
                    updated.amount = parse_amount(raw_value)
                case "memo":
                    updated.memo = str(raw_value).strip()
                case "tags":
                    updated.tags = parse_tags(raw_value)
                case _:
                    raise ValidationError(
                        f"수정할 수 없는 항목입니다: {field_name}",
                        "date/type/category/amount/memo/tags 만 수정할 수 있습니다.",
                    )

        if not self._storage.transactions.replace(transaction_id, updated):
            raise NotFoundError(
                f"해당 id의 거래가 없습니다: {transaction_id}",
                "list 명령으로 존재하는 id를 확인하세요.",
            )
        return updated

    @log_call
    def delete(self, transaction_id: str) -> Transaction:
        target = self.get(transaction_id)
        if not self._storage.transactions.delete(transaction_id):
            raise NotFoundError(
                f"해당 id의 거래가 없습니다: {transaction_id}",
                "list 명령으로 존재하는 id를 확인하세요.",
            )
        return target


class BudgetService:
    """월 예산 설정/조회."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage

    @log_call
    def set(self, raw_month: str, raw_amount: str | int) -> Budget:
        return self._storage.budgets.set(parse_month(raw_month), parse_amount(raw_amount))

    def get(self, raw_month: str) -> Budget | None:
        return self._storage.budgets.get(parse_month(raw_month))

    def list(self) -> list[Budget]:
        return sorted(self._storage.budgets.stream(), key=lambda item: item.month, reverse=True)

    @log_call
    def remove(self, raw_month: str) -> None:
        month = parse_month(raw_month)
        if not self._storage.budgets.remove(month):
            raise NotFoundError(
                f"해당 월의 예산이 없습니다: {month}", "budget list 로 설정된 월을 확인하세요."
            )


class ReportService:
    """월별 요약 계산."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._budgets = BudgetService(storage)

    @timed
    def monthly_summary(self, raw_month: str) -> MonthlySummary:
        """해당 월의 수입/지출/잔액과 카테고리별 지출 합계를 스트리밍으로 집계한다."""
        month = parse_month(raw_month)
        total_income = 0
        total_expense = 0
        count = 0
        by_category: dict[str, int] = defaultdict(int)

        for transaction in self._storage.transactions.stream():
            if transaction.month != month:
                continue
            count += 1
            if transaction.type == "income":
                total_income += transaction.amount
            else:
                total_expense += transaction.amount
                by_category[transaction.category] += transaction.amount

        budget = self._budgets.get(month)
        return MonthlySummary(
            month=month,
            total_income=total_income,
            total_expense=total_expense,
            count=count,
            expense_by_category=dict(by_category),
            budget=budget.amount if budget else None,
        )


class TransferService:
    """CSV 가져오기/내보내기."""

    def __init__(self, storage: Storage) -> None:
        self._storage = storage
        self._categories = CategoryService(storage)
        self._transactions = TransactionService(storage)

    @timed
    @log_call
    def import_csv(self, source: str | Path, *, create_categories: bool = False) -> ImportResult:
        path = Path(source).expanduser()
        if not path.exists():
            raise NotFoundError(
                f"가져올 CSV 파일이 없습니다: {path}", "--from 경로를 확인하세요."
            )

        result = ImportResult()
        next_number = int(self._storage.transactions.next_id().split("-")[1])
        known = set(self._storage.categories.names())
        buffered: list[Transaction] = []

        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames is None:
                raise ValidationError(
                    "CSV 헤더를 찾을 수 없습니다.",
                    f"첫 줄에 헤더가 필요합니다: {','.join(CSV_COLUMNS)}",
                )
            missing = [col for col in ("date", "type", "category", "amount") if col not in reader.fieldnames]
            if missing:
                raise ValidationError(
                    f"CSV 필수 컬럼이 없습니다: {', '.join(missing)}",
                    f"헤더 예시: {','.join(CSV_COLUMNS)}",
                )

            for line_no, row in enumerate(reader, start=2):
                try:
                    category = parse_category_name(str(row.get("category", "")))
                    if category not in known:
                        if not create_categories:
                            raise NotFoundError(
                                f"등록되지 않은 카테고리입니다: {category}",
                                "--create-categories 옵션을 쓰거나 category add 로 먼저 등록하세요.",
                            )
                        self._storage.categories.add(category)
                        known.add(category)
                        result.created_categories.append(category)

                    transaction = Transaction(
                        id=f"TX-{next_number:06d}",
                        date=parse_date(str(row.get("date", ""))),
                        type=parse_type(str(row.get("type", ""))),
                        category=category,
                        amount=parse_amount(str(row.get("amount", ""))),
                        memo=str(row.get("memo", "") or "").strip(),
                        tags=parse_tags(row.get("tags")),
                    )
                except (ValidationError, NotFoundError) as exc:
                    result.skipped += 1
                    result.errors.append(f"{line_no}행: {exc.message}")
                    continue

                buffered.append(transaction)
                next_number += 1

        result.imported = self._storage.transactions.add_many(buffered)
        return result

    @timed
    @log_call
    def export_csv(self, out: str | Path, criteria: SearchFilter) -> tuple[Path, int]:
        """조건에 맞는 거래를 CSV로 저장한다. (경로, 건수) 반환."""
        if criteria.is_empty:
            raise ValidationError(
                "export 는 기간 조건이 필요합니다.",
                "--month YYYY-MM 또는 --from YYYY-MM-DD --to YYYY-MM-DD 를 지정하세요.",
            )
        path = Path(out).expanduser()
        if path.parent and not path.parent.exists():
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise StorageError(
                    f"내보낼 폴더를 만들 수 없습니다: {path.parent}",
                    "--out 경로를 확인하세요.",
                ) from exc

        written = 0
        rows = sorted(self._transactions.iter_matching(criteria), key=lambda item: item.sort_key)
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
            writer.writeheader()
            for transaction in rows:
                writer.writerow(
                    {
                        "date": transaction.date,
                        "type": transaction.type,
                        "category": transaction.category,
                        "amount": transaction.amount,
                        "memo": transaction.memo,
                        "tags": ",".join(transaction.tags),
                    }
                )
                written += 1
        return path, written

