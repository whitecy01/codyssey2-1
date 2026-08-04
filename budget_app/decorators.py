"""공통 관심사(로그 / 실행 시간 / 예외 처리)를 분리하는 데코레이터 모음.

비즈니스 로직 함수는 "무엇을 하는지"만 담고,
"어떻게 로그를 남기고 오류를 사용자에게 보여줄지"는 이 모듈이 책임진다.

- `log_call`  : 함수 호출/반환을 DEBUG 로그로 남긴다.
- `timed`     : 실행 시간을 측정해 DEBUG 로그로 남긴다.
- `handle_errors`: CLI 명령 핸들러를 감싸 예외를 종료 코드로 변환한다.
"""

from __future__ import annotations

import functools
import logging
import sys
import time
from collections.abc import Callable
from typing import ParamSpec, TypeVar

from .errors import AppError, StorageError

P = ParamSpec("P")
R = TypeVar("R")

logger = logging.getLogger("budget_app")


def log_call(func: Callable[P, R]) -> Callable[P, R]:
    """함수 호출 전/후를 DEBUG 로그로 남긴다 (--verbose 시 화면 출력)."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        logger.debug("→ %s(args=%s, kwargs=%s)", func.__qualname__, _brief(args), _brief(kwargs))
        result = func(*args, **kwargs)
        logger.debug("← %s = %s", func.__qualname__, _brief(result))
        return result

    return wrapper


def timed(func: Callable[P, R]) -> Callable[P, R]:
    """실행 시간을 측정해 DEBUG 로그로 남긴다."""

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        started = time.perf_counter()
        try:
            return func(*args, **kwargs)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.debug("⏱ %s took %.2f ms", func.__qualname__, elapsed_ms)

    return wrapper


def handle_errors(func: Callable[P, int]) -> Callable[P, int]:
    """CLI 명령 핸들러 전용. 예외를 잡아 사용자 메시지 + 종료 코드로 변환한다.

    스택트레이스는 절대 출력하지 않는다(--verbose 일 때만 DEBUG 로그로 남김).
    """

    @functools.wraps(func)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> int:
        try:
            return func(*args, **kwargs)
        except AppError as exc:
            logger.debug("AppError in %s", func.__qualname__, exc_info=True)
            _print_error(exc.message, exc.hint)
            return exc.exit_code
        except KeyboardInterrupt:
            print("\n[중단] 사용자가 실행을 취소했습니다.", file=sys.stderr)
            return 130
        except (OSError, UnicodeDecodeError) as exc:
            logger.debug("OSError in %s", func.__qualname__, exc_info=True)
            wrapped = StorageError(
                f"파일을 읽거나 쓰는 중 문제가 발생했습니다: {exc}",
                "--data-dir 경로와 파일 접근 권한을 확인하세요.",
            )
            _print_error(wrapped.message, wrapped.hint)
            return wrapped.exit_code

    return wrapper


def _print_error(message: str, hint: str) -> None:
    print(f"[오류] {message}", file=sys.stderr)
    if hint:
        print(f"[힌트] {hint}", file=sys.stderr)


def _brief(value: object, limit: int = 120) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "…"
