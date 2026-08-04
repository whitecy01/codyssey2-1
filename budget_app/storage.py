"""저장소 계층(파일 I/O 전담).

저장 포맷은 **JSONL**(한 줄에 JSON 객체 하나)이며, 데이터는 3개 이상의 파일로
분리해 영구 저장한다.

    data/transactions.jsonl   거래 내역
    data/categories.jsonl     카테고리
    data/budgets.jsonl        월 예산

핵심 설계 두 가지
1) **스트리밍 읽기**: `stream()` 은 파일을 통째로 메모리에 올리지 않고
   `yield` 로 한 줄씩 흘려보낸다. 수십만 건이 쌓여도 메모리 사용량이 일정하다.
2) **원자적 쓰기**: update/delete 는 임시 파일에 전량 재작성한 뒤
   `os.replace` 로 교체한다. 중간에 프로세스가 죽어도 원본이 망가지지 않는다.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .decorators import log_call, logger, timed
from .errors import StorageError
from .models import Budget, Category, Transaction

DEFAULT_CATEGORIES: tuple[str, ...] = ("food", "transport", "rent", "salary", "etc")


class JsonlFile:
    """JSONL 파일 하나에 대한 저수준 접근(읽기 스트림 / 추가 / 원자적 재작성)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def ensure(self) -> bool:
        """파일이 없으면 빈 파일로 생성한다. 새로 만들었으면 True."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            return False
        self.path.touch()
        logger.debug("created data file: %s", self.path)
        return True

    def stream(self) -> Iterator[dict[str, Any]]:
        """파일을 한 줄씩 읽어 dict 로 흘려보내는 제너레이터.

        전체 로드를 하지 않으므로 대용량 파일에서도 메모리가 일정하다.
        깨진 줄은 건너뛰고 경고 로그만 남겨 한 줄의 손상이 전체 조회를 막지 않게 한다.
        """
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line_no, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("손상된 줄을 건너뜁니다: %s:%d", self.path.name, line_no)
                    continue
                if isinstance(record, dict):
                    yield record
                else:
                    logger.warning("객체가 아닌 줄을 건너뜁니다: %s:%d", self.path.name, line_no)

    def append(self, record: dict[str, Any]) -> None:
        """레코드 한 건을 파일 끝에 추가한다."""
        self.ensure()
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def rewrite(self, records: Iterable[dict[str, Any]]) -> None:
        """임시 파일에 쓰고 os.replace 로 교체하는 원자적 전량 재작성."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for record in records:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
            logger.debug("atomically rewrote %s", self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise


@dataclass(slots=True)
class DataPaths:
    """저장 파일 경로 묶음."""

    root: Path

    @property
    def transactions(self) -> Path:
        return self.root / "transactions.jsonl"

    @property
    def categories(self) -> Path:
        return self.root / "categories.jsonl"

    @property
    def budgets(self) -> Path:
        return self.root / "budgets.jsonl"

    def all(self) -> tuple[Path, ...]:
        return (self.transactions, self.categories, self.budgets)


class TransactionRepository:
    """거래 내역 저장소."""

    def __init__(self, path: Path) -> None:
        self._file = JsonlFile(path)

    @property
    def path(self) -> Path:
        return self._file.path

    def ensure(self) -> bool:
        return self._file.ensure()

    def stream(self) -> Iterator[Transaction]:
        """저장된 거래를 파일 순서대로 하나씩 흘려보낸다(스트리밍)."""
        for record in self._file.stream():
            try:
                yield Transaction.from_dict(record)
            except Exception as exc:  # 손상 레코드는 건너뛴다
                logger.warning("잘못된 거래 레코드를 건너뜁니다 (%s): %s", exc, record)

    @log_call
    def add(self, transaction: Transaction) -> Transaction:
        self._file.append(transaction.to_dict())
        return transaction

    @log_call
    def add_many(self, transactions: Iterable[Transaction]) -> int:
        count = 0
        self._file.ensure()
        with self._file.path.open("a", encoding="utf-8") as handle:
            for transaction in transactions:
                handle.write(json.dumps(transaction.to_dict(), ensure_ascii=False) + "\n")
                count += 1
        return count

    def get(self, transaction_id: str) -> Transaction | None:
        for transaction in self.stream():
            if transaction.id == transaction_id:
                return transaction
        return None

    def next_id(self) -> str:
        """저장된 최대 번호 + 1 로 새 id(TX-000001)를 만든다."""
        largest = 0
        for transaction in self.stream():
            _, _, digits = transaction.id.partition("-")
            if digits.isdigit():
                largest = max(largest, int(digits))
        return f"TX-{largest + 1:06d}"

    @timed
    @log_call
    def replace(self, transaction_id: str, updated: Transaction) -> bool:
        """id 에 해당하는 거래를 교체한다(원자적 재작성). 없으면 False."""
        found = False

        def records() -> Iterator[dict[str, Any]]:
            nonlocal found
            for transaction in self.stream():
                if transaction.id == transaction_id:
                    found = True
                    yield updated.to_dict()
                else:
                    yield transaction.to_dict()

        self._file.rewrite(records())
        return found

    @timed
    @log_call
    def delete(self, transaction_id: str) -> bool:
        """id 에 해당하는 거래를 삭제한다(원자적 재작성). 없으면 False."""
        found = False

        def records() -> Iterator[dict[str, Any]]:
            nonlocal found
            for transaction in self.stream():
                if transaction.id == transaction_id:
                    found = True
                    continue
                yield transaction.to_dict()

        self._file.rewrite(records())
        return found

    @timed
    @log_call
    def recategorize(self, old_category: str, new_category: str) -> int:
        """카테고리 일괄 변경(카테고리 삭제 시 대체용). 변경 건수를 반환."""
        changed = 0

        def records() -> Iterator[dict[str, Any]]:
            nonlocal changed
            for transaction in self.stream():
                if transaction.category == old_category:
                    transaction.category = new_category
                    changed += 1
                yield transaction.to_dict()

        self._file.rewrite(records())
        return changed

    def count_by_category(self, category: str) -> int:
        return sum(1 for transaction in self.stream() if transaction.category == category)


class CategoryStore:
    """카테고리 저장소."""

    def __init__(self, path: Path) -> None:
        self._file = JsonlFile(path)

    @property
    def path(self) -> Path:
        return self._file.path

    def ensure(self) -> bool:
        return self._file.ensure()

    def stream(self) -> Iterator[Category]:
        for record in self._file.stream():
            try:
                yield Category.from_dict(record)
            except Exception as exc:
                logger.warning("잘못된 카테고리 레코드를 건너뜁니다 (%s): %s", exc, record)

    def names(self) -> list[str]:
        return [category.name for category in self.stream()]

    def exists(self, name: str) -> bool:
        return any(category.name == name for category in self.stream())

    @log_call
    def add(self, name: str) -> Category:
        category = Category(name=name, created_at=_now())
        self._file.append(category.to_dict())
        return category

    @log_call
    def remove(self, name: str) -> bool:
        found = False

        def records() -> Iterator[dict[str, Any]]:
            nonlocal found
            for category in self.stream():
                if category.name == name:
                    found = True
                    continue
                yield category.to_dict()

        self._file.rewrite(records())
        return found

    def seed_defaults(self) -> list[str]:
        """파일이 비어 있으면 기본 카테고리를 자동 생성한다(안 A)."""
        if self.names():
            return []
        for name in DEFAULT_CATEGORIES:
            self.add(name)
        return list(DEFAULT_CATEGORIES)


class BudgetStore:
    """월 예산 저장소. 같은 월은 항상 한 건만 유지한다."""

    def __init__(self, path: Path) -> None:
        self._file = JsonlFile(path)

    @property
    def path(self) -> Path:
        return self._file.path

    def ensure(self) -> bool:
        return self._file.ensure()

    def stream(self) -> Iterator[Budget]:
        for record in self._file.stream():
            try:
                yield Budget.from_dict(record)
            except Exception as exc:
                logger.warning("잘못된 예산 레코드를 건너뜁니다 (%s): %s", exc, record)

    def get(self, month: str) -> Budget | None:
        for budget in self.stream():
            if budget.month == month:
                return budget
        return None

    @log_call
    def set(self, month: str, amount: int) -> Budget:
        budget = Budget(month=month, amount=amount, updated_at=_now())
        others = [item.to_dict() for item in self.stream() if item.month != month]
        others.append(budget.to_dict())
        others.sort(key=lambda item: item["month"])
        self._file.rewrite(others)
        return budget

    @log_call
    def remove(self, month: str) -> bool:
        found = False

        def records() -> Iterator[dict[str, Any]]:
            nonlocal found
            for budget in self.stream():
                if budget.month == month:
                    found = True
                    continue
                yield budget.to_dict()

        self._file.rewrite(records())
        return found


class Storage:
    """저장소 묶음. 데이터 디렉터리 준비를 함께 담당한다."""

    def __init__(self, data_dir: str | os.PathLike[str]) -> None:
        self.paths = DataPaths(Path(data_dir).expanduser())
        self.transactions = TransactionRepository(self.paths.transactions)
        self.categories = CategoryStore(self.paths.categories)
        self.budgets = BudgetStore(self.paths.budgets)

    @property
    def data_dir(self) -> Path:
        return self.paths.root

    def initialize(self) -> list[Path]:
        """저장 파일이 없으면 자동 생성한다. 새로 만든 파일 목록을 반환."""
        try:
            self.paths.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StorageError(
                f"데이터 폴더를 만들 수 없습니다: {self.paths.root}",
                "--data-dir 로 쓰기 가능한 경로를 지정하세요.",
            ) from exc
        created: list[Path] = []
        for store in (self.transactions, self.categories, self.budgets):
            if store.ensure():
                created.append(store.path)
        return created


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# 서비스 계층이 저장소 종류에 직접 의존하지 않도록 하는 팩토리 타입 별칭
StorageFactory = Callable[[str], Storage]
