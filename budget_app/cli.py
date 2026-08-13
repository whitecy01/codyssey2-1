"""CLI 계층(사용자 입출력 전담).

argparse 로 명령/옵션을 해석하고, 서비스 계층을 호출한 뒤 결과를 출력한다.
비즈니스 규칙은 여기서 다루지 않는다.

입력 방식 정책
- add / category add / category remove : 대화형(input) 기본, 필요 시 옵션으로 생략 가능
- list / search / summary / budget / delete / import / export : 옵션 인자 방식
- **update 는 옵션 기반(안 A)으로 고정**한다.
  예) update --id TX-000012 --amount 20000 --memo "저녁"
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Sequence
from typing import Any, NoReturn

from .decorators import handle_errors, logger
from .errors import AppError, ValidationError
from .formatting import format_summary, format_transactions, money
from .services import (
    CSV_COLUMNS,
    BudgetService,
    CategoryService,
    ReportService,
    SearchFilter,
    TransactionService,
    TransferService,
)
from .storage import DEFAULT_CATEGORIES, Storage
from .validators import (
    month_range,
    parse_amount,
    parse_category_name,
    parse_date,
    parse_memo,
    parse_month,
    parse_tags,
    parse_transaction_id,
    parse_type,
    validate_period,
)

PROGRAM = "python -m budget_app"
DEFAULT_DATA_DIR = "./data"
DEFAULT_LIST_LIMIT = 20
DEFAULT_SEARCH_LIMIT = 50
DEFAULT_TOP_N = 5


# --------------------------------------------------------------------------- #
# 대화형 입력 도우미
# --------------------------------------------------------------------------- #
def ask(
    label: str,
    convert: Callable[[str], Any] | None = None,
    *,
    allow_empty: bool = False,
    default: Any = None,
) -> Any:
    """검증에 통과할 때까지 다시 물어보는 입력 루프."""
    while True:
        try:
            raw = input(label)
        except EOFError:
            raise ValidationError(
                "입력이 더 이상 없습니다.", "대화형 입력이 필요합니다. 값을 모두 입력하세요."
            ) from None
        raw = raw.strip()
        if not raw:
            if allow_empty:
                return default
            print("[오류] 값을 입력해야 합니다.", file=sys.stderr)
            continue
        if convert is None:
            return raw
        try:
            return convert(raw)
        except AppError as exc:
            print(f"[오류] {exc.message}", file=sys.stderr)
            if exc.hint:
                print(f"[힌트] {exc.hint}", file=sys.stderr)


def _print_or_notice(text: str, empty_message: str) -> None:
    print(text if text else empty_message)


# --------------------------------------------------------------------------- #
# 명령 핸들러
# --------------------------------------------------------------------------- #
@handle_errors
def cmd_add(args: argparse.Namespace, storage: Storage) -> int:
    """대화형으로 거래를 추가한다."""
    service = TransactionService(storage)
    categories = CategoryService(storage)

    available = categories.list()
    if not available:
        raise AppError(
            "등록된 카테고리가 없습니다.", "먼저 category add 로 카테고리를 등록하세요."
        )
    print(f"등록된 카테고리: {', '.join(available)}")

    transaction = service.add(
        raw_date=ask("날짜(YYYY-MM-DD): ", parse_date),
        raw_type=ask("타입(income/expense): ", parse_type),
        raw_category=ask("카테고리: ", categories.ensure_exists),
        raw_amount=ask("금액(양수): ", parse_amount),
        memo=ask("메모(선택): ", parse_memo, allow_empty=True, default=""),
        raw_tags=ask("태그(쉼표로 구분, 없으면 엔터): ", parse_tags, allow_empty=True, default=[]),
    )
    print(f"[저장 완료] id={transaction.id}")
    return 0


@handle_errors
def cmd_list(args: argparse.Namespace, storage: Storage) -> int:
    """최신순 거래 목록(스트리밍)."""
    limit = args.limit if args.limit is not None else DEFAULT_LIST_LIMIT
    transactions = TransactionService(storage).list_recent(limit)
    _print_or_notice(
        format_transactions(transactions),
        "[안내] 저장된 거래가 없습니다. add 명령으로 첫 내역을 추가하세요.",
    )
    if transactions:
        print(f"\n총 {len(transactions)}건 표시 (최신순)")
    return 0


@handle_errors
def cmd_search(args: argparse.Namespace, storage: Storage) -> int:
    """조건 검색(스트리밍)."""
    criteria = _build_filter(args, storage)
    limit = args.limit if args.limit is not None else DEFAULT_SEARCH_LIMIT
    transactions = TransactionService(storage).search(criteria, limit)
    _print_or_notice(format_transactions(transactions), "[안내] 조건에 맞는 거래가 없습니다.")
    if transactions:
        print(f"\n총 {len(transactions)}건 표시 (최신순)")
    return 0


@handle_errors
def cmd_summary(args: argparse.Namespace, storage: Storage) -> int:
    """월별 요약 + 예산 사용률."""
    summary = ReportService(storage).monthly_summary(args.month)
    if summary.count == 0:
        print(f"[안내] {summary.month} 에 해당하는 데이터가 없습니다.")
        budget = BudgetService(storage).get(summary.month)
        if budget:
            print(f"예산: {money(budget.amount)} (사용률 0.0%)")
        return 0
    print(format_summary(summary, args.top if args.top is not None else DEFAULT_TOP_N))
    return 0


@handle_errors
def cmd_update(args: argparse.Namespace, storage: Storage) -> int:
    """옵션 기반 거래 수정(안 A)."""
    changes: dict[str, Any] = {}
    for field_name in ("date", "type", "category", "amount", "memo", "tags"):
        value = getattr(args, field_name, None)
        if value is not None:
            changes[field_name] = value

    transaction = TransactionService(storage).update(parse_transaction_id(args.id), changes)
    print(f"[수정 완료] id={transaction.id}")
    print(format_transactions([transaction]))
    return 0


@handle_errors
def cmd_delete(args: argparse.Namespace, storage: Storage) -> int:
    """id 기반 거래 삭제."""
    transaction = TransactionService(storage).delete(parse_transaction_id(args.id))
    print(
        f"[삭제 완료] id={transaction.id} ({transaction.date} / {transaction.category} / "
        f"{money(transaction.amount)})"
    )
    return 0


@handle_errors
def cmd_budget(args: argparse.Namespace, storage: Storage) -> int:
    """예산 설정/조회/삭제."""
    service = BudgetService(storage)
    match args.budget_command:
        case "set":
            budget = service.set(args.month, args.amount)
            print(f"[저장 완료] {budget.month} 예산 {money(budget.amount)}")
        case "list":
            budgets = service.list()
            if not budgets:
                print("[안내] 설정된 예산이 없습니다. budget set --month YYYY-MM --amount <금액>")
                return 0
            for budget in budgets:
                print(f"- {budget.month}: {money(budget.amount)}")
        case "remove":
            service.remove(args.month)
            print(f"[삭제 완료] {parse_month(args.month)} 예산")
    return 0


@handle_errors
def cmd_category(args: argparse.Namespace, storage: Storage) -> int:
    """카테고리 추가/조회/삭제."""
    service = CategoryService(storage)
    match args.category_command:
        case "add":
            name = args.name or ask("카테고리명: ", parse_category_name)
            category = service.add(name)
            print(f"[저장 완료] category={category.name}")
        case "list":
            names = service.list()
            if not names:
                print("[안내] 등록된 카테고리가 없습니다. category add 로 추가하세요.")
                return 0
            for name in names:
                print(f"- {name}")
        case "remove":
            name = args.name or ask("삭제할 카테고리명: ", parse_category_name)
            moved, target = service.remove(name, args.replace_with)
            if moved:
                print(f"[삭제 완료] category={name} (거래 {moved}건을 '{target}' 로 이동)")
            else:
                print(f"[삭제 완료] category={name}")
    return 0


@handle_errors
def cmd_import(args: argparse.Namespace, storage: Storage) -> int:
    """CSV 일괄 가져오기."""
    result = TransferService(storage).import_csv(
        args.source, create_categories=args.create_categories
    )
    print(f"[완료] imported={result.imported}, skipped={result.skipped}")
    if result.created_categories:
        print(f"[안내] 새로 등록된 카테고리: {', '.join(result.created_categories)}")
    sys.stdout.flush()
    for message in result.errors[:10]:
        print(f"  - 건너뜀: {message}", file=sys.stderr)
    if len(result.errors) > 10:
        print(f"  - 그 외 {len(result.errors) - 10}건 생략", file=sys.stderr)
    return 0


@handle_errors
def cmd_export(args: argparse.Namespace, storage: Storage) -> int:
    """조건에 맞는 거래를 CSV로 내보내기."""
    criteria = _build_filter(args, storage, require_period=True)
    path, count = TransferService(storage).export_csv(args.out, criteria)
    print(f"[완료] {path} ({count} records)")
    return 0


# --------------------------------------------------------------------------- #
# 공통 헬퍼
# --------------------------------------------------------------------------- #
def _build_filter(
    args: argparse.Namespace, storage: Storage, *, require_period: bool = False
) -> SearchFilter:
    """--month / --from / --to / --category / --type / --q / --tag 를 검색 조건으로 변환."""
    month = getattr(args, "month", None)
    date_from = getattr(args, "date_from", None)
    date_to = getattr(args, "date_to", None)

    if month and (date_from or date_to):
        raise ValidationError(
            "--month 와 --from/--to 는 함께 사용할 수 없습니다.", "둘 중 하나만 지정하세요."
        )
    if month:
        date_from, date_to = month_range(month)
    else:
        date_from, date_to = validate_period(date_from, date_to)

    if require_period and not (date_from or date_to):
        raise ValidationError(
            "기간 조건이 필요합니다.",
            "--month YYYY-MM 또는 --from YYYY-MM-DD --to YYYY-MM-DD 중 하나 이상을 지정하세요.",
        )

    category = getattr(args, "category", None)
    if category:
        category = CategoryService(storage).ensure_exists(category)

    transaction_type = getattr(args, "type", None)
    if transaction_type:
        transaction_type = parse_type(transaction_type)

    return SearchFilter(
        date_from=date_from,
        date_to=date_to,
        category=category,
        type=transaction_type,
        query=getattr(args, "query", None),
        tag=getattr(args, "tag", None),
    )


# --------------------------------------------------------------------------- #
# 파서 구성
# --------------------------------------------------------------------------- #
def _common_parser() -> argparse.ArgumentParser:
    """모든 명령이 공유하는 전역 옵션(값이 없으면 상위 파서 값을 유지)."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--data-dir",
        dest="data_dir",
        metavar="PATH",
        default=argparse.SUPPRESS,
        help=f"저장 폴더 경로 (기본: {DEFAULT_DATA_DIR})",
    )
    common.add_argument(
        "--verbose",
        dest="verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="실행 로그/실행 시간 등 디버그 정보를 출력",
    )
    return common


def build_parser() -> argparse.ArgumentParser:
    """전체 명령 트리를 만든다. 모든 명령은 --help 를 지원한다."""
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        parents=[common],
        description="파일 기반 콘솔 가계부 (JSONL 저장 / 스트리밍 조회)",
        epilog="예: %(prog)s summary --month 2024-01 --top 3",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # add ------------------------------------------------------------------ #
    add = subparsers.add_parser(
        "add", parents=[common], help="거래 추가(대화형)", description="대화형으로 거래를 추가합니다."
    )
    add.set_defaults(handler=cmd_add)

    # list ----------------------------------------------------------------- #
    listing = subparsers.add_parser(
        "list",
        parents=[common],
        help="거래 목록(최신순)",
        description="저장된 거래를 최신순으로 출력합니다(스트리밍 처리).",
    )
    listing.add_argument(
        "--limit", type=int, metavar="N", help=f"출력 건수 (기본: {DEFAULT_LIST_LIMIT}, 0=전체)"
    )
    listing.set_defaults(handler=cmd_list)

    # search --------------------------------------------------------------- #
    search = subparsers.add_parser(
        "search",
        parents=[common],
        help="조건 검색(최신순)",
        description="기간/카테고리/타입/메모/태그 조건으로 거래를 검색합니다.",
    )
    _add_period_options(search)
    search.add_argument("--category", metavar="NAME", help="카테고리")
    search.add_argument("--type", choices=("income", "expense"), help="거래 타입")
    search.add_argument("--q", dest="query", metavar="KEYWORD", help="메모 키워드")
    search.add_argument("--tag", metavar="TAG", help="태그")
    search.add_argument(
        "--limit", type=int, metavar="N", help=f"출력 건수 (기본: {DEFAULT_SEARCH_LIMIT}, 0=전체)"
    )
    search.set_defaults(handler=cmd_search)

    # summary -------------------------------------------------------------- #
    summary = subparsers.add_parser(
        "summary",
        parents=[common],
        help="월별 요약",
        description="월별 수입/지출/잔액과 카테고리별 지출 TOP N, 예산 사용률을 출력합니다.",
    )
    summary.add_argument("--month", required=True, metavar="YYYY-MM", help="요약할 월")
    summary.add_argument("--top", type=int, metavar="N", help=f"지출 상위 N개 (기본: {DEFAULT_TOP_N})")
    summary.set_defaults(handler=cmd_summary)

    # update --------------------------------------------------------------- #
    update = subparsers.add_parser(
        "update",
        parents=[common],
        help="거래 수정(옵션 기반)",
        description="지정한 필드만 수정합니다. 예: update --id TX-000012 --amount 20000",
    )
    update.add_argument("--id", required=True, metavar="TX-000001", help="수정할 거래 id")
    update.add_argument("--date", metavar="YYYY-MM-DD", help="날짜")
    update.add_argument("--type", choices=("income", "expense"), help="거래 타입")
    update.add_argument("--category", metavar="NAME", help="카테고리")
    update.add_argument("--amount", metavar="N", help="금액(양수)")
    update.add_argument("--memo", metavar="TEXT", help="메모")
    update.add_argument("--tags", metavar="A,B", help="태그(쉼표 구분)")
    update.set_defaults(handler=cmd_update)

    # delete --------------------------------------------------------------- #
    delete = subparsers.add_parser(
        "delete", parents=[common], help="거래 삭제", description="id 로 거래를 삭제합니다."
    )
    delete.add_argument("--id", required=True, metavar="TX-000001", help="삭제할 거래 id")
    delete.set_defaults(handler=cmd_delete)

    # budget --------------------------------------------------------------- #
    budget = subparsers.add_parser(
        "budget", parents=[common], help="월 예산 설정/조회", description="월 예산을 설정하고 조회합니다."
    )
    budget_sub = budget.add_subparsers(dest="budget_command", metavar="<set|list|remove>")
    budget_set = budget_sub.add_parser("set", parents=[common], help="월 예산 저장")
    budget_set.add_argument("--month", required=True, metavar="YYYY-MM", help="대상 월")
    budget_set.add_argument("--amount", required=True, metavar="N", help="예산 금액(양수)")
    budget_sub.add_parser("list", parents=[common], help="설정된 예산 목록")
    budget_remove = budget_sub.add_parser("remove", parents=[common], help="월 예산 삭제")
    budget_remove.add_argument("--month", required=True, metavar="YYYY-MM", help="대상 월")
    budget.set_defaults(handler=cmd_budget, budget_command=None)

    # category ------------------------------------------------------------- #
    category = subparsers.add_parser(
        "category", parents=[common], help="카테고리 관리", description="카테고리를 추가/조회/삭제합니다."
    )
    category_sub = category.add_subparsers(dest="category_command", metavar="<add|list|remove>")
    category_add = category_sub.add_parser("add", parents=[common], help="카테고리 추가(대화형)")
    category_add.add_argument("--name", metavar="NAME", help="카테고리명(생략 시 대화형 입력)")
    category_sub.add_parser("list", parents=[common], help="카테고리 목록")
    category_remove = category_sub.add_parser("remove", parents=[common], help="카테고리 삭제")
    category_remove.add_argument("--name", metavar="NAME", help="삭제할 카테고리명")
    category_remove.add_argument(
        "--replace-with",
        dest="replace_with",
        metavar="NAME",
        help="사용 중인 카테고리를 삭제할 때 옮겨 담을 대체 카테고리",
    )
    category.set_defaults(handler=cmd_category, category_command=None)

    # import / export ------------------------------------------------------ #
    importer = subparsers.add_parser(
        "import",
        parents=[common],
        help="CSV 가져오기",
        description=f"CSV 스키마: {','.join(CSV_COLUMNS)} (UTF-8, 헤더 포함)",
    )
    importer.add_argument("--from", dest="source", required=True, metavar="FILE", help="가져올 CSV 경로")
    importer.add_argument(
        "--create-categories",
        dest="create_categories",
        action="store_true",
        help="CSV에만 있는 카테고리를 자동 등록",
    )
    importer.set_defaults(handler=cmd_import)

    exporter = subparsers.add_parser(
        "export",
        parents=[common],
        help="CSV 내보내기",
        description=f"CSV 스키마: {','.join(CSV_COLUMNS)} (UTF-8, 헤더 포함). "
        "--month 또는 --from/--to 중 하나 이상 필수.",
    )
    exporter.add_argument("--out", required=True, metavar="FILE", help="저장할 CSV 경로")
    exporter.add_argument("--month", metavar="YYYY-MM", help="대상 월")
    _add_period_options(exporter, with_month=False)
    exporter.add_argument("--category", metavar="NAME", help="카테고리")
    exporter.add_argument("--type", choices=("income", "expense"), help="거래 타입")
    exporter.set_defaults(handler=cmd_export)

    return parser


def _add_period_options(parser: argparse.ArgumentParser, *, with_month: bool = True) -> None:
    if with_month:
        parser.add_argument("--month", metavar="YYYY-MM", help="대상 월(--from/--to 와 함께 쓸 수 없음)")
    parser.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD", help="시작일(포함)")
    parser.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD", help="종료일(포함)")


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="[%(levelname)s] %(message)s",
        stream=sys.stderr,
    )
    logger.setLevel(logging.DEBUG if verbose else logging.WARNING)


def _bootstrap(storage: Storage) -> None:
    """저장 파일 자동 생성 + 기본 카테고리 시드(안 A)."""
    created = storage.initialize()
    if created:
        print(f"[안내] 저장 폴더를 초기화했습니다: {storage.data_dir}")
        for path in created:
            print(f"  - 생성됨: {path.name}")
    seeded = storage.categories.seed_defaults()
    if seeded:
        print(f"[안내] 기본 카테고리를 생성했습니다: {', '.join(DEFAULT_CATEGORIES)}")


def main(argv: Sequence[str] | None = None) -> int:
    """엔트리 포인트. 정상 종료 0, 오류 종료는 0이 아닌 값을 반환한다."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "command", None) is None:
        parser.print_help()
        return 0

    _configure_logging(getattr(args, "verbose", False))

    # 하위 명령이 없는 그룹 명령(budget/category) 처리
    for group, dest in (("budget", "budget_command"), ("category", "category_command")):
        if args.command == group and getattr(args, dest, None) is None:
            print(f"[오류] {group} 하위 명령이 필요합니다.", file=sys.stderr)
            print(f"[힌트] {PROGRAM} {group} --help 를 확인하세요.", file=sys.stderr)
            return 2

    storage = Storage(getattr(args, "data_dir", None) or DEFAULT_DATA_DIR)
    try:
        _bootstrap(storage)
    except AppError as exc:
        print(f"[오류] {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"[힌트] {exc.hint}", file=sys.stderr)
        return exc.exit_code

    handler: Callable[[argparse.Namespace, Storage], int] = args.handler
    return handler(args, storage)


def run() -> NoReturn:
    """`python -m budget_app` 의 실제 진입점."""
    sys.exit(main())
