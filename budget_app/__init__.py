"""budget_app — 파일 기반 콘솔 가계부.

계층 구조
    cli.py         사용자 입출력(argparse / 대화형 입력 / 출력)
    services.py    비즈니스 규칙(추가/검색/요약/가져오기·내보내기)
    storage.py     파일 I/O(JSONL 스트리밍 읽기, 원자적 쓰기)
    models.py      데이터 구조(dataclass)
    validators.py  입력 검증
    decorators.py  공통 관심사(로그/시간 측정/예외 처리)
    formatting.py  출력 포맷터
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
