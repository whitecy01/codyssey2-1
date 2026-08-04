"""budget_app 통합/단위 테스트 (표준 라이브러리 unittest 만 사용).

실행: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from budget_app.cli import main  # noqa: E402
from budget_app.errors import ConflictError, NotFoundError, ValidationError  # noqa: E402
from budget_app.formatting import format_transaction  # noqa: E402
from budget_app.models import Transaction  # noqa: E402
from budget_app.services import (  # noqa: E402
    BudgetService,
    CategoryService,
    ReportService,
    SearchFilter,
    TransactionService,
    TransferService,
)
from budget_app.storage import Storage  # noqa: E402
from budget_app.validators import parse_amount, parse_date, parse_month, parse_tags  # noqa: E402


class CliTestCase(unittest.TestCase):
    """임시 데이터 폴더에서 CLI 를 실행하는 헬퍼."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self._tmp.name) / "data"
        self.addCleanup(self._tmp.cleanup)

    def run_cli(self, *argv: str, stdin: str = "") -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        original_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = main([*argv, "--data-dir", str(self.data_dir)])
        finally:
            sys.stdin = original_stdin
        return code, out.getvalue(), err.getvalue()

    def add_transaction(
        self, date: str, type_: str, category: str, amount: str, memo: str = "", tags: str = ""
    ) -> str:
        stdin = f"{date}\n{type_}\n{category}\n{amount}\n{memo}\n{tags}\n"
        code, out, err = self.run_cli("add", stdin=stdin)
        self.assertEqual(code, 0, err)
        return out.strip().rsplit("id=", 1)[1]


class TestBootstrap(CliTestCase):
    def test_creates_three_or_more_data_files_with_defaults(self) -> None:
        code, out, _ = self.run_cli("category", "list")
        self.assertEqual(code, 0)
        for name in ("transactions.jsonl", "categories.jsonl", "budgets.jsonl"):
            self.assertTrue((self.data_dir / name).exists(), name)
        self.assertIn("- food", out)

    def test_no_command_prints_help(self) -> None:
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("usage:", out)


class TestAddAndList(CliTestCase):
    def test_add_returns_sequential_ids_and_persists(self) -> None:
        first = self.add_transaction("2024-01-15", "expense", "food", "15000", "점심", "meal")
        second = self.add_transaction("2024-01-16", "income", "salary", "3000000")
        self.assertEqual((first, second), ("TX-000001", "TX-000002"))

        lines = (self.data_dir / "transactions.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        record = json.loads(lines[0])
        self.assertEqual(record["tags"], ["meal"])
        self.assertEqual(record["amount"], 15000)

    def test_add_reprompts_until_valid(self) -> None:
        stdin = "2024-13-40\n2024-01-15\nspend\nexpense\nnope\nfood\n-5\n15000\n\n\n"
        code, out, err = self.run_cli("add", stdin=stdin)
        self.assertEqual(code, 0)
        self.assertIn("날짜 형식이 올바르지 않습니다", err)
        self.assertIn("허용되지 않는 타입", err)
        self.assertIn("등록되지 않은 카테고리", err)
        self.assertIn("[저장 완료] id=TX-000001", out)

    def test_list_is_newest_first_and_respects_limit(self) -> None:
        self.add_transaction("2024-01-10", "expense", "food", "1000")
        self.add_transaction("2024-01-20", "expense", "food", "2000")
        self.add_transaction("2024-01-15", "expense", "food", "3000")

        code, out, _ = self.run_cli("list", "--limit", "2")
        self.assertEqual(code, 0)
        body = [line for line in out.splitlines() if line.startswith("TX-")]
        self.assertEqual([line.split(" | ")[1] for line in body], ["2024-01-20", "2024-01-15"])

    def test_list_empty_shows_notice(self) -> None:
        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("저장된 거래가 없습니다", out)


class TestSearch(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_transaction("2024-01-05", "expense", "rent", "150000", "월세", "fixed")
        self.add_transaction("2024-01-12", "expense", "transport", "20000", "지하철")
        self.add_transaction("2024-01-15", "expense", "food", "15000", "점심 김밥", "meal")
        self.add_transaction("2024-02-01", "income", "salary", "3000000", "2월 급여")

    def _ids(self, *argv: str) -> list[str]:
        code, out, err = self.run_cli(*argv)
        self.assertEqual(code, 0, err)
        return [line.split(" | ")[0] for line in out.splitlines() if line.startswith("TX-")]

    def test_period_filter(self) -> None:
        self.assertEqual(
            self._ids("search", "--from", "2024-01-10", "--to", "2024-01-20"),
            ["TX-000003", "TX-000002"],
        )

    def test_month_filter(self) -> None:
        self.assertEqual(self._ids("search", "--month", "2024-02"), ["TX-000004"])

    def test_type_category_query_tag_filters(self) -> None:
        self.assertEqual(self._ids("search", "--type", "income"), ["TX-000004"])
        self.assertEqual(self._ids("search", "--category", "rent"), ["TX-000001"])
        self.assertEqual(self._ids("search", "--q", "김밥"), ["TX-000003"])
        self.assertEqual(self._ids("search", "--tag", "fixed"), ["TX-000001"])

    def test_no_match_shows_notice(self) -> None:
        code, out, _ = self.run_cli("search", "--q", "존재하지않는메모")
        self.assertEqual(code, 0)
        self.assertIn("조건에 맞는 거래가 없습니다", out)

    def test_month_and_period_conflict(self) -> None:
        code, _, err = self.run_cli("search", "--month", "2024-01", "--from", "2024-01-01")
        self.assertNotEqual(code, 0)
        self.assertIn("함께 사용할 수 없습니다", err)

    def test_reversed_period_is_rejected(self) -> None:
        code, _, err = self.run_cli("search", "--from", "2024-03-01", "--to", "2024-01-01")
        self.assertNotEqual(code, 0)
        self.assertIn("기간이 뒤집혀", err)


class TestSummaryAndBudget(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.add_transaction("2024-01-05", "expense", "rent", "150000")
        self.add_transaction("2024-01-12", "expense", "transport", "20000")
        self.add_transaction("2024-01-15", "expense", "food", "45000")
        self.add_transaction("2024-01-25", "income", "salary", "3000000")

    def test_summary_totals_and_top_n(self) -> None:
        code, out, err = self.run_cli("summary", "--month", "2024-01", "--top", "2")
        self.assertEqual(code, 0, err)
        self.assertIn("총 수입: 3000000원", out)
        self.assertIn("총 지출: 215000원", out)
        self.assertIn("잔액: 2785000원", out)
        self.assertIn("지출 TOP 2", out)
        self.assertIn("rent", out)
        self.assertNotIn("transport", out)  # TOP 2 밖

    def test_summary_without_data(self) -> None:
        code, out, _ = self.run_cli("summary", "--month", "2023-07")
        self.assertEqual(code, 0)
        self.assertIn("데이터가 없습니다", out)

    def test_budget_usage_rate_and_over_warning(self) -> None:
        self.run_cli("budget", "set", "--month", "2024-01", "--amount", "500000")
        code, out, _ = self.run_cli("summary", "--month", "2024-01")
        self.assertEqual(code, 0)
        self.assertIn("사용률 43.0%", out)
        self.assertNotIn("[경고]", out)

        self.run_cli("budget", "set", "--month", "2024-01", "--amount", "100000")
        _, out, _ = self.run_cli("summary", "--month", "2024-01")
        self.assertIn("[경고] 예산을 115000원 초과했습니다!", out)

    def test_budget_is_persisted_and_unique_per_month(self) -> None:
        self.run_cli("budget", "set", "--month", "2024-01", "--amount", "500000")
        self.run_cli("budget", "set", "--month", "2024-01", "--amount", "700000")
        lines = (self.data_dir / "budgets.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["amount"], 700000)

    def test_budget_bad_month_exits_nonzero(self) -> None:
        code, _, err = self.run_cli("budget", "set", "--month", "2024-1", "--amount", "1000")
        self.assertEqual(code, 2)
        self.assertIn("월 형식이 올바르지 않습니다", err)


class TestUpdateDelete(CliTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.first = self.add_transaction("2024-01-10", "expense", "food", "1000", "메모")
        self.second = self.add_transaction("2024-01-11", "expense", "food", "2000")

    def test_update_changes_only_given_fields(self) -> None:
        code, out, err = self.run_cli("update", "--id", self.first, "--amount", "9999")
        self.assertEqual(code, 0, err)
        self.assertIn("[수정 완료]", out)

        records = [
            json.loads(line)
            for line in (self.data_dir / "transactions.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["amount"], 9999)
        self.assertEqual(records[0]["memo"], "메모")
        self.assertEqual(records[1]["amount"], 2000)

    def test_update_unknown_id(self) -> None:
        code, _, err = self.run_cli("update", "--id", "TX-999999", "--amount", "10")
        self.assertNotEqual(code, 0)
        self.assertIn("해당 id의 거래가 없습니다", err)

    def test_update_rejects_unknown_category(self) -> None:
        code, _, err = self.run_cli("update", "--id", self.first, "--category", "unknown")
        self.assertNotEqual(code, 0)
        self.assertIn("등록되지 않은 카테고리", err)

    def test_delete_removes_only_target(self) -> None:
        code, out, _ = self.run_cli("delete", "--id", self.first)
        self.assertEqual(code, 0)
        self.assertIn("[삭제 완료]", out)
        remaining = (self.data_dir / "transactions.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(json.loads(remaining[0])["id"], self.second)

    def test_delete_unknown_id_exits_nonzero(self) -> None:
        code, _, err = self.run_cli("delete", "--id", "TX-000404")
        self.assertNotEqual(code, 0)
        self.assertIn("해당 id의 거래가 없습니다", err)

    def test_delete_bad_id_format(self) -> None:
        code, _, err = self.run_cli("delete", "--id", "12")
        self.assertEqual(code, 2)
        self.assertIn("거래 id 형식", err)


class TestCategory(CliTestCase):
    def test_add_and_duplicate(self) -> None:
        code, out, _ = self.run_cli("category", "add", "--name", "coffee")
        self.assertEqual(code, 0)
        self.assertIn("[저장 완료] category=coffee", out)

        code, _, err = self.run_cli("category", "add", "--name", "coffee")
        self.assertNotEqual(code, 0)
        self.assertIn("이미 존재하는 카테고리", err)

    def test_add_interactive(self) -> None:
        code, out, _ = self.run_cli("category", "add", stdin="cafe\n")
        self.assertEqual(code, 0)
        self.assertIn("category=cafe", out)

    def test_remove_blocked_when_in_use(self) -> None:
        self.add_transaction("2024-01-01", "expense", "food", "1000")
        code, _, err = self.run_cli("category", "remove", "--name", "food")
        self.assertNotEqual(code, 0)
        self.assertIn("삭제할 수 없습니다", err)
        self.assertIn("--replace-with", err)

    def test_remove_with_replacement_moves_transactions(self) -> None:
        self.add_transaction("2024-01-01", "expense", "food", "1000")
        code, out, err = self.run_cli(
            "category", "remove", "--name", "food", "--replace-with", "etc"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("'etc' 로 이동", out)
        self.assertNotIn("food", self.run_cli("category", "list")[1])

        record = json.loads(
            (self.data_dir / "transactions.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        self.assertEqual(record["category"], "etc")

    def test_remove_unknown_category(self) -> None:
        code, _, err = self.run_cli("category", "remove", "--name", "ghost")
        self.assertNotEqual(code, 0)
        self.assertIn("존재하지 않는 카테고리", err)

    def test_group_command_without_subcommand(self) -> None:
        code, _, err = self.run_cli("category")
        self.assertEqual(code, 2)
        self.assertIn("하위 명령이 필요합니다", err)


class TestImportExport(CliTestCase):
    def _write_csv(self, body: str) -> Path:
        path = self.data_dir.parent / "in.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def test_import_skips_invalid_rows(self) -> None:
        csv_path = self._write_csv(
            "date,type,category,amount,memo,tags\n"
            "2024-02-01,expense,food,8000,아침,meal\n"
            "2024-02-02,expense,coffee,4500,카페,\n"  # 미등록 카테고리
            "2024-13-40,expense,food,1000,,\n"  # 잘못된 날짜
            "2024-02-03,expense,food,-1,,\n"  # 음수 금액
        )
        code, out, err = self.run_cli("import", "--from", str(csv_path))
        self.assertEqual(code, 0, err)
        self.assertIn("imported=1, skipped=3", out)

    def test_import_can_create_categories(self) -> None:
        csv_path = self._write_csv(
            "date,type,category,amount,memo,tags\n2024-02-02,expense,coffee,4500,카페,tag1\n"
        )
        code, out, _ = self.run_cli("import", "--from", str(csv_path), "--create-categories")
        self.assertEqual(code, 0)
        self.assertIn("imported=1, skipped=0", out)
        self.assertIn("- coffee", self.run_cli("category", "list")[1])

    def test_import_missing_file(self) -> None:
        code, _, err = self.run_cli("import", "--from", str(self.data_dir / "nope.csv"))
        self.assertNotEqual(code, 0)
        self.assertIn("가져올 CSV 파일이 없습니다", err)

    def test_import_missing_columns(self) -> None:
        csv_path = self._write_csv("date,category\n2024-01-01,food\n")
        code, _, err = self.run_cli("import", "--from", str(csv_path))
        self.assertNotEqual(code, 0)
        self.assertIn("필수 컬럼이 없습니다", err)

    def test_export_requires_period(self) -> None:
        code, _, err = self.run_cli("export", "--out", str(self.data_dir / "out.csv"))
        self.assertNotEqual(code, 0)
        self.assertIn("기간 조건이 필요합니다", err)

    def test_export_round_trip(self) -> None:
        self.add_transaction("2024-01-15", "expense", "food", "15000", "점심", "meal,lunch")
        self.add_transaction("2024-02-15", "expense", "food", "16000")
        out_path = self.data_dir.parent / "out.csv"

        code, out, err = self.run_cli(
            "export", "--out", str(out_path), "--month", "2024-01"
        )
        self.assertEqual(code, 0, err)
        self.assertIn("(1 records)", out)

        rows = out_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(rows[0], "date,type,category,amount,memo,tags")
        self.assertEqual(rows[1], '2024-01-15,expense,food,15000,점심,"meal,lunch"')

        code, out, _ = self.run_cli("import", "--from", str(out_path))
        self.assertEqual(code, 0)
        self.assertIn("imported=1", out)


class TestStreamingAndStorage(CliTestCase):
    def test_stream_is_lazy_generator(self) -> None:
        storage = Storage(self.data_dir)
        storage.initialize()
        storage.categories.seed_defaults()
        service = TransactionService(storage)
        for day in range(1, 6):
            service.add(
                raw_date=f"2024-03-{day:02d}",
                raw_type="expense",
                raw_category="food",
                raw_amount="1000",
            )

        stream = storage.transactions.stream()
        self.assertFalse(isinstance(stream, list))
        first = next(stream)
        self.assertEqual(first.id, "TX-000001")  # 전체를 읽지 않고 첫 건만 소비
        stream.close()

    def test_corrupted_line_is_skipped(self) -> None:
        storage = Storage(self.data_dir)
        storage.initialize()
        storage.categories.seed_defaults()
        TransactionService(storage).add(
            raw_date="2024-03-01", raw_type="expense", raw_category="food", raw_amount="1000"
        )
        with (self.data_dir / "transactions.jsonl").open("a", encoding="utf-8") as handle:
            handle.write("{ 깨진 줄\n")
            handle.write(json.dumps({"id": "TX-000009", "date": "bad"}) + "\n")

        code, out, _ = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertEqual(len([line for line in out.splitlines() if line.startswith("TX-")]), 1)

    def test_atomic_rewrite_leaves_no_temp_files(self) -> None:
        storage = Storage(self.data_dir)
        storage.initialize()
        storage.categories.seed_defaults()
        service = TransactionService(storage)
        created = service.add(
            raw_date="2024-03-01", raw_type="expense", raw_category="food", raw_amount="1000"
        )
        service.update(created.id, {"amount": "2000"})
        leftovers = [p.name for p in self.data_dir.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])


class TestServiceLayer(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.storage = Storage(Path(self._tmp.name) / "data")
        self.storage.initialize()
        self.storage.categories.seed_defaults()
        self.transactions = TransactionService(self.storage)

    def _seed(self) -> None:
        rows = [
            ("2024-01-05", "expense", "rent", 150000, "월세", "fixed"),
            ("2024-01-12", "expense", "transport", 20000, "지하철", ""),
            ("2024-01-15", "expense", "food", 45000, "점심", "meal"),
            ("2024-01-25", "income", "salary", 3000000, "급여", ""),
            ("2024-02-01", "expense", "food", 7000, "커피", ""),
        ]
        for date, type_, category, amount, memo, tags in rows:
            self.transactions.add(
                raw_date=date,
                raw_type=type_,
                raw_category=category,
                raw_amount=amount,
                memo=memo,
                raw_tags=tags,
            )

    def test_summary_calculation(self) -> None:
        self._seed()
        summary = ReportService(self.storage).monthly_summary("2024-01")
        self.assertEqual(summary.total_income, 3000000)
        self.assertEqual(summary.total_expense, 215000)
        self.assertEqual(summary.balance, 2785000)
        self.assertEqual(summary.count, 4)
        self.assertEqual(summary.top_expenses(2), [("rent", 150000), ("food", 45000)])
        self.assertIsNone(summary.usage_rate)

    def test_summary_with_budget(self) -> None:
        self._seed()
        BudgetService(self.storage).set("2024-01", 200000)
        summary = ReportService(self.storage).monthly_summary("2024-01")
        self.assertTrue(summary.is_over_budget)
        self.assertAlmostEqual(summary.usage_rate or 0, 107.5)

    def test_search_filter_matches(self) -> None:
        self._seed()
        found = self.transactions.search(SearchFilter(type="expense", category="food"), limit=10)
        self.assertEqual([t.date for t in found], ["2024-02-01", "2024-01-15"])

    def test_search_limit_zero_returns_all(self) -> None:
        self._seed()
        self.assertEqual(len(self.transactions.search(SearchFilter(), limit=0)), 5)

    def test_category_service_errors(self) -> None:
        categories = CategoryService(self.storage)
        with self.assertRaises(ConflictError):
            categories.add("food")
        with self.assertRaises(NotFoundError):
            categories.ensure_exists("ghost")

    def test_transfer_service_export_requires_period(self) -> None:
        with self.assertRaises(ValidationError):
            TransferService(self.storage).export_csv("out.csv", SearchFilter())


class TestValidators(unittest.TestCase):
    def test_parse_date(self) -> None:
        self.assertEqual(parse_date(" 2024-01-15 "), "2024-01-15")
        for bad in ("2024-13-01", "2024-02-30", "24-01-01", "", "abc"):
            with self.assertRaises(ValidationError, msg=bad):
                parse_date(bad)

    def test_parse_month(self) -> None:
        self.assertEqual(parse_month("2024-01"), "2024-01")
        for bad in ("2024-1", "2024-13", "2024", ""):
            with self.assertRaises(ValidationError, msg=bad):
                parse_month(bad)

    def test_parse_amount(self) -> None:
        self.assertEqual(parse_amount("15,000"), 15000)
        self.assertEqual(parse_amount(15000), 15000)
        for bad in ("0", "-1", "1.5", "abc", ""):
            with self.assertRaises(ValidationError, msg=bad):
                parse_amount(bad)

    def test_parse_tags_dedupes_and_trims(self) -> None:
        self.assertEqual(parse_tags(" a , b ,a,, "), ["a", "b"])
        self.assertEqual(parse_tags(None), [])


class TestFormatting(unittest.TestCase):
    def test_format_transaction_line(self) -> None:
        transaction = Transaction(
            id="TX-000001",
            date="2024-01-15",
            type="expense",
            category="food",
            amount=15000,
            memo="점심",
            tags=["meal"],
        )
        self.assertEqual(
            format_transaction(transaction),
            "TX-000001 | 2024-01-15 | expense | food | 15000 | 점심 | meal",
        )

    def test_format_transaction_trims_empty_trailing_fields(self) -> None:
        transaction = Transaction(
            id="TX-000002", date="2024-01-14", type="income", category="salary", amount=3000000
        )
        self.assertEqual(
            format_transaction(transaction), "TX-000002 | 2024-01-14 | income | salary | 3000000"
        )


if __name__ == "__main__":
    unittest.main()
