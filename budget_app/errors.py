"""애플리케이션 전용 예외 정의.

모든 예외는 사용자에게 보여줄 `message`와 해결 방법을 알려주는 `hint`,
그리고 프로세스 종료 코드(`exit_code`)를 함께 가진다.
CLI 계층은 이 정보를 이용해 스택트레이스 대신 사람이 읽을 수 있는
`[오류] ... / [힌트] ...` 형태로 출력한다.
"""

from __future__ import annotations


class AppError(Exception):
    """가계부 애플리케이션의 모든 예외의 최상위 타입."""

    exit_code: int = 1

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message: str = message
        self.hint: str = hint


class ValidationError(AppError):
    """사용자 입력이 형식/규칙을 만족하지 못했을 때."""

    exit_code = 2


class NotFoundError(AppError):
    """존재하지 않는 거래 id, 카테고리 등을 참조했을 때."""

    exit_code = 3


class ConflictError(AppError):
    """이미 존재하는 카테고리 추가, 사용 중인 카테고리 삭제 등."""

    exit_code = 4


class StorageError(AppError):
    """파일 입출력(권한, 손상된 레코드 등) 문제."""

    exit_code = 5
