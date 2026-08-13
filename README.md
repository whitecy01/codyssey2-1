# budget_app — 파일 기반 콘솔 가계부

수입/지출 내역을 파일에 영구 저장하고, 검색 · 월별 요약 · 예산 경고 · CSV 가져오기/내보내기까지
지원하는 콘솔 애플리케이션입니다. **표준 라이브러리만** 사용합니다 (Python 3.10+).

- 저장 포맷: **JSONL** (한 줄에 JSON 객체 하나)
- 조회: **제너레이터 기반 스트리밍** (파일 전체를 메모리에 올리지 않음)
- 공통 관심사(로그/실행 시간/예외 처리): **데코레이터**로 분리
- 계층 분리: **CLI / 서비스 / 저장소 / 모델**

---

## 1. 실행 방법

```bash
python3 --version            # 3.10 이상
cd <이 저장소 루트>
python3 -m budget_app --help # 전체 명령 목록
```

별도 설치(pip)가 필요 없습니다. 모든 명령은 `--help` 를 지원합니다.

```bash
python3 -m budget_app add --help
python3 -m budget_app budget set --help
```

첫 실행 시 저장 폴더와 파일이 자동 생성되고, 카테고리 파일이 비어 있으면
**기본 카테고리가 자동 생성**됩니다(안 A). 그 뒤 바로 `add` 를 사용할 수 있습니다.

```
[안내] 저장 폴더를 초기화했습니다: ./data
  - 생성됨: transactions.jsonl
  - 생성됨: categories.jsonl
  - 생성됨: budgets.jsonl
[안내] 기본 카테고리를 생성했습니다: food, transport, rent, salary, etc
```

### 전역 옵션

| 옵션 | 설명 | 기본값 |
| --- | --- | --- |
| `--data-dir PATH` | 저장 폴더 경로 | `./data` |
| `--verbose` | 실행 로그/실행 시간(DEBUG) 출력 | 꺼짐 |

전역 옵션은 명령 앞뒤 어디에나 붙일 수 있습니다.

```bash
python3 -m budget_app --data-dir ./mydata list
python3 -m budget_app list --data-dir ./mydata --verbose
```

---

## 2. 저장 파일 위치 / 형식

기본 저장 폴더는 `./data` 이며 `--data-dir` 로 변경할 수 있습니다.
데이터는 **3개 파일로 분리**되어 영구 저장됩니다.

| 파일 | 내용 | 레코드 예시 |
| --- | --- | --- |
| `data/transactions.jsonl` | 거래 내역 | `{"id":"TX-000001","date":"2024-01-15","type":"expense","category":"food","amount":15000,"memo":"점심","tags":["meal"]}` |
| `data/categories.jsonl` | 카테고리 | `{"name":"food","created_at":"2026-08-04T11:15:28"}` |
| `data/budgets.jsonl` | 월 예산(월당 1건) | `{"month":"2024-01","amount":500000,"updated_at":"2026-08-04T11:15:28"}` |

- 인코딩은 모두 UTF-8, 한글은 그대로 저장합니다(`ensure_ascii=False`).
- `update` / `delete` / `category remove` 는 **임시 파일에 전량 재작성 → `os.replace` 로 원자적 교체**합니다
  (요구사항 §4-6의 "전체 재작성/임시 파일/원자적 교체(권장)").
  중간에 프로세스가 죽어도 원본 파일이 깨지지 않습니다.
- 손상된 줄(JSON 파싱 실패, 필드 누락)은 경고만 남기고 건너뜁니다. 한 줄의 손상이 전체 조회를 막지 않습니다.

### 2.1 입력 제한

아래 제한은 **대화형 입력 · 옵션 인자 · CSV import 모두에 동일하게** 적용됩니다.
검증 함수를 `validators.py` 한 곳에 모아 두고 세 경로가 공유하기 때문입니다.

| 항목 | 제한 | 초과 시 |
| --- | --- | --- |
| 금액(`amount`) | 1 ~ **10,000,000,000,000,000**(1경) 사이의 정수 | `ValidationError` (exit 2) |
| 메모(`memo`) | **200자** 이하 (선택 항목) | `ValidationError` (exit 2) |
| 태그 1개 | **20자** 이하 | `ValidationError` (exit 2) |
| 태그 개수 | 거래당 **10개** 이하 | `ValidationError` (exit 2) |
| 카테고리명 | **30자** 이하, 공백·쉼표 불가 | `ValidationError` (exit 2) |
| 거래 건수 | **999,999건** (id `TX-000001` ~ `TX-999999`) | `StorageError` (exit 5) |

대화형 입력에서는 오류 메시지를 보여준 뒤 **같은 항목을 다시 물어봅니다.**
옵션 인자 방식에서는 즉시 오류 종료합니다.

```
금액(양수): 111111111111111111111111111111111111111111
[오류] 금액이 너무 큽니다 (42자리).
[힌트] 최대 10,000,000,000,000,000원까지 입력할 수 있습니다.
금액(양수):
```

제한을 둔 이유는 두 가지입니다.

- **금액**: 파이썬 정수는 자릿수 제한이 없어, 상한이 없으면 예산 사용률 계산(`총지출 / 예산`)이
  float 변환에서 `OverflowError` 로 죽습니다. 그보다 작은 값에서도 정밀도가 깨져 엉뚱한 사용률이 나옵니다.
- **거래 id**: id 는 `TX-` + **6자리 0채움**으로 고정합니다. 자릿수가 섞이면
  `Transaction.sort_key` 의 문자열 비교가 깨지기 때문입니다(`"TX-999999" > "TX-1000000"`).
  한도에 닿으면 조용히 7자리로 넘어가지 않고 새 id 발급을 거부합니다.

> 저장 파일을 직접 편집해 제한을 넘는 **금액**을 넣으면, 읽는 시점에 손상 레코드로 간주해
> 그 한 건만 건너뜁니다(경고 로그). 반면 **메모 길이**는 읽을 때 검사하지 않습니다 —
> 길어도 동작에 지장이 없는데 거래 전체를 버리는 편이 손해이기 때문입니다.

---

## 3. 주요 명령 예시

### 3.1 add — 거래 추가 (대화형)

```
$ python3 -m budget_app add
등록된 카테고리: etc, food, rent, salary, transport
날짜(YYYY-MM-DD): 2024-01-15
타입(income/expense): expense
카테고리: food
금액(양수): 15000
메모(선택): 점심
태그(쉼표로 구분, 없으면 엔터): meal
[저장 완료] id=TX-000001
```

잘못된 값을 넣으면 원인 + 힌트를 보여주고 **그 항목만 다시** 묻습니다.

```
날짜(YYYY-MM-DD): 2024-13-40
[오류] 날짜 형식이 올바르지 않습니다 (YYYY-MM-DD).
[힌트] 예: 2024-01-15
날짜(YYYY-MM-DD):
```

### 3.2 list — 목록 조회 (최신순, 스트리밍)

```
$ python3 -m budget_app list --limit 3
TX-000001 | 2024-01-15 | expense | food | 15000 | 점심 | meal
TX-000002 | 2024-01-14 | income | salary | 3000000
TX-000003 | 2024-01-12 | expense | transport | 20000

총 3건 표시 (최신순)
```

출력 형식은 `id | date | type | category | amount | memo | tags` 이며,
`--limit` 기본값은 20, `--limit 0` 은 전체 출력입니다.

### 3.3 search — 조건 검색 (최신순, 스트리밍)

```bash
python3 -m budget_app search --from 2024-01-01 --to 2024-01-31
python3 -m budget_app search --month 2024-01 --type expense --category food
python3 -m budget_app search --q 점심            # 메모 키워드
python3 -m budget_app search --tag meal --limit 5 # 태그
```

| 옵션 | 설명 |
| --- | --- |
| `--from` / `--to` | 기간(포함). `--month` 와 동시 사용 불가 |
| `--month` | 해당 월 전체 |
| `--category` | 등록된 카테고리 |
| `--type` | `income` / `expense` |
| `--q` | 메모 부분 일치(대소문자 무시) |
| `--tag` | 태그 정확 일치 |
| `--limit` | 출력 건수(기본 50, `0`=전체) |

### 3.4 summary — 월별 요약 (+ 예산)

```
$ python3 -m budget_app summary --month 2024-01 --top 3
[2024-01 요약] 거래 4건
총 수입: 3000000원
총 지출: 215000원
잔액: 2785000원
예산: 500000원 (사용률 43.0%)
남은 예산: 285000원

지출 TOP 3
1) rent 150000원
2) food 45000원
3) transport 20000원
```

- 예산을 넘기면 `[경고] 예산을 N원 초과했습니다!`, 80% 이상이면 `[주의]` 를 출력합니다.
- 데이터가 없는 달은 `[안내] 2023-07 에 해당하는 데이터가 없습니다.` 를 출력합니다.

### 3.5 budget — 예산 설정/조회

```bash
python3 -m budget_app budget set --month 2024-01 --amount 500000
python3 -m budget_app budget list
python3 -m budget_app budget remove --month 2024-01
```

같은 월을 다시 `set` 하면 덮어씁니다(월당 1건 유지).

### 3.6 category — 카테고리 관리

```bash
python3 -m budget_app category add                 # 대화형: 카테고리명 입력
python3 -m budget_app category add --name coffee   # 옵션으로 생략 가능
python3 -m budget_app category list
python3 -m budget_app category remove --name coffee
python3 -m budget_app category remove --name food --replace-with etc
```

사용 중인 카테고리는 **삭제를 막습니다.** 대체 카테고리를 지정하면 해당 거래를 모두 옮긴 뒤 삭제합니다.

```
$ python3 -m budget_app category remove --name food
[오류] 'food' 카테고리를 사용하는 거래가 3건 있어 삭제할 수 없습니다.
[힌트] --replace-with <다른 카테고리> 로 대체 카테고리를 지정하세요. 예: category remove --name food --replace-with etc
```

### 3.7 update — 거래 수정 (**옵션 기반, 안 A로 고정**)

> 이 프로젝트의 `update` 는 **옵션 방식**입니다. 대화형 입력은 사용하지 않습니다.

```bash
python3 -m budget_app update --id TX-000001 --amount 20000 --memo "저녁"
python3 -m budget_app update --id TX-000001 --category transport --tags "commute,bus"
```

- 지정한 필드만 변경되고 나머지는 그대로 유지됩니다.
- 수정 가능한 필드: `--date` `--type` `--category` `--amount` `--memo` `--tags`
- 아무 필드도 주지 않으면 `[오류] 수정할 항목이 없습니다.` 로 종료합니다(exit 2).
- 없는 id 는 `[오류] 해당 id의 거래가 없습니다: TX-999999` (exit 3).

### 3.8 delete — 거래 삭제

```
$ python3 -m budget_app delete --id TX-000003
[삭제 완료] id=TX-000003 (2024-01-12 / transport / 20000원)
```

### 3.9 import / export — CSV 가져오기/내보내기

```bash
python3 -m budget_app export --out export.csv --month 2024-01
python3 -m budget_app export --out q1.csv --from 2024-01-01 --to 2024-03-31 --type expense
python3 -m budget_app import --from import.csv
python3 -m budget_app import --from import.csv --create-categories
```

```
$ python3 -m budget_app export --out export.csv --month 2024-01
[완료] export.csv (12 records)

$ python3 -m budget_app import --from import.csv
  - 건너뜀: 3행: 등록되지 않은 카테고리입니다: coffee
[완료] imported=5, skipped=1
```

- `export` 는 `--month` **또는** `--from`/`--to` 중 **하나 이상이 필수**입니다(없으면 exit 2).
- `import` 는 잘못된 행을 건너뛰고 사유를 출력하며, 유효한 행만 저장합니다.
  기본적으로 **등록되지 않은 카테고리 행은 건너뜁니다.** `--create-categories` 를 주면 자동 등록합니다.

---

## 4. import / export CSV 스키마

공통: **UTF-8, 헤더 포함**, 컬럼 순서는 아래와 같습니다.

| column | required | 설명 |
| --- | --- | --- |
| `date` | Y | `YYYY-MM-DD` |
| `type` | Y | `income` / `expense` |
| `category` | Y | 등록된 카테고리 (`--create-categories` 사용 시 자동 등록) |
| `amount` | Y | 양수 정수 (최대 1경, [§2.1](#21-입력-제한)) |
| `memo` | N | 문자열 (200자 이하) |
| `tags` | N | 쉼표(`,`) 구분 문자열 (개당 20자, 최대 10개) |

```csv
date,type,category,amount,memo,tags
2024-01-15,expense,food,15000,점심,"meal,lunch"
2024-01-25,income,salary,3000000,1월 급여,
```

- `memo` / `tags` 컬럼은 없어도 됩니다(필수 컬럼 4개만 있으면 동작).
- 태그처럼 쉼표가 들어가는 값은 큰따옴표로 감쌉니다(표준 CSV 규칙, `csv` 모듈이 처리).
- `import` 시 id 는 저장소가 새로 부여합니다(CSV의 id 컬럼은 사용하지 않습니다).
- 검증에 실패한 행은 `skipped` 로 집계하고 사유를 출력한 뒤 **나머지 행은 계속 처리**합니다.
  단, 거래 id 한도(999,999건)를 넘으면 그 시점에 중단하며 **아무 행도 저장되지 않습니다**
  (파일 쓰기는 모든 행을 검증한 뒤 한 번에 수행하므로, 전부 아니면 전무입니다).

---

## 5. 프로젝트 구조와 설계

```
budget_app/
├── __main__.py     python -m budget_app 진입점
├── cli.py          CLI 계층   : argparse, 대화형 입력, 출력
├── services.py     서비스 계층: 비즈니스 규칙(추가/검색/요약/CSV/예산)
├── storage.py      저장소 계층: JSONL 스트리밍 읽기 + 원자적 쓰기
├── models.py       모델 계층  : dataclass (Transaction / Category / Budget / MonthlySummary)
├── validators.py   입력 검증(날짜·금액·타입·태그·월·id)
├── decorators.py   공통 관심사(로그 / 실행 시간 / 예외 처리)
└── formatting.py   출력 포맷터
tests/
└── test_budget_app.py   unittest 통합/단위 테스트 50개
```

### 5.1 계층별 책임

| 계층 | 파일 | 책임 | 하지 않는 일 |
| --- | --- | --- | --- |
| CLI | `cli.py` | 옵션 파싱, `input()`, `print()`, 종료 코드 | 금액 합산 같은 계산 |
| 서비스 | `services.py` | 규칙 판단(카테고리 존재 확인, 요약 집계, 검색 조건) | 화면 출력, 파일 포맷 |
| 저장소 | `storage.py` | 파일 읽기/쓰기, 스트리밍, 원자적 교체 | 도메인 규칙 |
| 모델 | `models.py` | 데이터 구조와 dict ↔ 객체 변환 계약 | I/O |

덕분에 저장 포맷을 CSV로 바꾸더라도 `storage.py` 만 교체하면 되고,
출력을 JSON으로 바꾸려면 `formatting.py` 만 손대면 됩니다.

### 5.2 제너레이터 스트리밍 (`yield`)

`JsonlFile.stream()` 은 파일을 한 줄씩 읽어 `yield` 합니다.

```python
def stream(self) -> Iterator[dict[str, Any]]:
    with self.path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            ...
            yield record          # 전체를 리스트로 만들지 않는다
```

- **왜?** `json.load(f)` 나 `f.readlines()` 는 파일 크기만큼 메모리를 먹습니다.
  10만 건이 쌓여도 제너레이터는 한 번에 한 줄만 들고 있습니다.
- **최신순 정렬은?** 정렬은 원래 전체를 필요로 하지만,
  `heapq.nlargest(limit, stream, key=...)` 로 **상위 N건만 힙에 유지**해
  메모리를 `O(limit)` 로 묶었습니다 (`services.TransactionService._top`).
- `summary` 도 스트림을 한 번 훑으며 합계만 누적합니다(`O(1)` 메모리).
- `update`/`delete` 역시 `stream()` 을 소비하면서 임시 파일에 바로 써서 교체합니다.

### 5.3 데코레이터로 분리한 공통 관심사

| 데코레이터 | 하는 일 | 적용 위치 |
| --- | --- | --- |
| `@handle_errors` | 예외 → `[오류]/[힌트]` 출력 + 종료 코드 반환. **스택트레이스 출력 금지** | 모든 CLI 명령 핸들러(`cmd_add`, `cmd_list`, …) |
| `@timed` | 실행 시간 측정 → DEBUG 로그 | `list_recent`, `search`, `monthly_summary`, `import_csv`, 저장소 재작성 |
| `@log_call` | 호출 인자/반환값 DEBUG 로그 | 서비스·저장소의 상태 변경 함수 |

```bash
$ python3 -m budget_app list --limit 2 --verbose
[DEBUG] ⏱ TransactionService.list_recent took 1.41 ms
```

`try/except` 를 명령마다 반복해서 쓰지 않아도 되고, 로깅 정책을 바꿀 때
비즈니스 코드를 건드리지 않습니다. `ParamSpec`/`TypeVar` 를 써서 감싼 뒤에도
원본 함수의 타입이 유지됩니다.

### 5.4 타입 힌트로 만든 계약

```python
def search(self, criteria: SearchFilter, limit: int = 50) -> list[Transaction]: ...
def stream(self) -> Iterator[Transaction]: ...          # 리스트가 아니라 스트림임이 드러남
def remove(self, raw_name: str, replace_with: str | None = None) -> tuple[int, str | None]: ...
```

- `Iterator[Transaction]` vs `list[Transaction]` 만 봐도 "전체 로드인지 스트리밍인지" 구분됩니다.
- `raw_*` 인자(검증 전 문자열)와 검증된 값(`int`, `str`)을 이름과 타입으로 구분해
  "어디서 검증이 끝나는지"가 코드에 드러납니다.
- `MonthlySummary` 같은 dataclass 반환 타입 덕분에 CLI 는 딕셔너리 키를 추측할 필요가 없습니다.

---

## 6. 오류 처리 / 종료 코드

스택트레이스는 출력하지 않고, 항상 **원인 + 해결 힌트** 를 표준 오류로 보여줍니다.

```
$ python3 -m budget_app summary --month 2024-1
[오류] 월 형식이 올바르지 않습니다 (YYYY-MM).
[힌트] 예: --month 2024-01
$ echo $?
2
```

| 종료 코드 | 의미 | 예 |
| --- | --- | --- |
| `0` | 정상 종료 | 모든 성공, "데이터 없음" 안내 |
| `1` | 일반 오류(`AppError`) / 예상치 못한 예외 | 카테고리가 하나도 없는데 `add` 실행 |
| `2` | 입력 검증 실패(`ValidationError`) / argparse 사용법 오류 | 잘못된 날짜·금액·월, 수정 필드 미지정 |
| `3` | 대상 없음(`NotFoundError`) | 없는 거래 id, 없는 CSV 파일 |
| `4` | 충돌(`ConflictError`) | 중복 카테고리, 사용 중 카테고리 삭제 |
| `5` | 저장소 오류(`StorageError`) | 권한 없는 폴더, 거래 id 한도 초과 |
| `130` | 사용자 중단(Ctrl+C) | 대화형 입력 중 취소 |

`handle_errors` 데코레이터에는 **최종 방어선**이 있습니다. 위 예외에 해당하지 않는
예상치 못한 예외가 올라와도 스택트레이스 대신 아래처럼 출력하고 `1` 로 종료합니다.

```
[오류] 예상치 못한 오류가 발생했습니다: RuntimeError: ...
[힌트] --verbose 옵션으로 자세한 내용을 확인할 수 있습니다.
```

스택트레이스는 `--verbose` 를 줬을 때만 DEBUG 로그로 남습니다.

---

## 7. 테스트

```bash
python3 -m unittest discover -s tests -v
```

임시 폴더(`tempfile`)를 사용하므로 실제 `./data` 는 건드리지 않습니다.
CLI 실행/스트리밍/원자적 쓰기/검증/포맷터/CSV 왕복까지 50개 테스트가 포함되어 있습니다.

```
Ran 50 tests in 0.47s

OK
```

---

## 8. 요구사항 대응표

| 요구사항 | 구현 위치 |
| --- | --- |
| 10가지 기능(add/list/search/summary/budget/category/update/delete/import/export) | `cli.py` 의 `cmd_*` |
| 3개 이상 저장 파일 | `storage.DataPaths` (transactions / categories / budgets) |
| dataclass 데이터 모델, 2개 이상 클래스 | `models.py`, `storage.py`, `services.py` |
| 제너레이터 스트리밍 | `storage.JsonlFile.stream`, `services.TransactionService.iter_matching` |
| 데코레이터 1개 이상 | `decorators.py` 의 `handle_errors` / `timed` / `log_call` |
| 타입 힌트 | 전 모듈 (`from __future__ import annotations`) |
| 3개 이상 모듈 분리 | `cli` / `services` / `storage` / `models` / `validators` / `decorators` / `formatting` |
| update/delete 안정성(임시 파일 + 원자적 교체) | `storage.JsonlFile.rewrite` |
| 스택트레이스 금지 · 종료 코드 | `decorators.handle_errors`, `errors.py` |
| 입력 검증(날짜/금액/타입/카테고리 + 길이·상한) | `validators.py` ([§2.1](#21-입력-제한)) |

> 보너스 과제(백업 / 반복 내역 / 테이블 정렬)는 구현하지 않았습니다.
