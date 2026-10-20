# partial-server

RAG 파이프라인의 **수집(ingestion) 단계를 관리하는 내부 컨트롤 패널**입니다.
RAG 시스템 자체가 아니며, 질문에 답하거나 검색 트래픽을 처리하지 않습니다.
*무엇을* 수집할지, *어떻게* 파싱할지, 그 결과가 *보존할 만큼 좋은지*를 사람이
직접 판단할 수 있게 해주는 human-in-the-loop 도구입니다.

대상 코퍼스는 약 **9,000개의 PDF**입니다. 문서들은 소수의 유형/카테고리로
묶이며, 같은 카테고리 안의 문서들은 구조적으로 유사합니다(같은 레이아웃,
같은 머리글/바닥글, 같은 표 형태). 이 유사성이 프로젝트의 핵심 설계 전제입니다.

## 워크플로우

1. PDF를 등록하고 각 문서에 유형/카테고리를 지정
2. 카테고리별 **대표 문서** 몇 개에 대해 파싱 전략을 수동 튜닝
3. 추출 결과를 브라우저에서 검수하고 승인/반려
4. 승인된 전략을 카테고리의 나머지 문서에 배치로 승격(promote)

즉, 이 도구는 **문서 하나를 꼼꼼히 들여다보는 것**에 최적화되어 있습니다.
대량 처리 속도는 목표가 아니라 결과물입니다.

## 파이프라인 단계

업로드부터 검색까지, 화면 단위로 단계가 나뉩니다.

| 단계 | 설명 |
| --- | --- |
| 문서 등록 (Documents) | PDF 업로드, sha256 기반 중복 제거, RustFS 저장 |
| 파싱 (Parsing) | MinerU로 전체 문서 파싱 — 마크다운, JSON, 페이지 이미지 생성 |
| 목차/헤더 (Headers) | 목차 페이지에서 헤더 구조 추출 (doc-extract + LLM) |
| 섹션 (Sections) | 헤더 기준으로 본문 분할, 표 정규화, VLM 기반 그림 설명 |
| 그룹핑 (Grouping) | 섹션을 의미 단위로 묶기 |
| 청킹 (Chunking) | 토크나이저 기반으로 검색 단위 청크 생성 |
| 요약 (Summarizing) | 청크/섹션 요약 생성 (개조식) |
| 임베딩 (Embedding) | Upstage 임베딩 API로 벡터화, Qdrant에 저장 |
| 검색 (Retrieval) | 검수용 검색 화면 — 질의 임베딩 후 Qdrant 조회 |
| 클라이언트 API (Client) | SSE 기반 Q&A API — 출처가 포함된 답변 제공 |

## 기술 스택

| 영역 | 선택 |
| --- | --- |
| 런타임 / 패키징 | Python 3.13+ + `uv` |
| 웹 프레임워크 | FastAPI |
| 템플릿 | Jinja2 (서버 렌더링) |
| 인터랙션 | HTMX (빌드 스텝 없음, SPA 없음) |
| 메타데이터 저장소 | PostgreSQL (psycopg 3, SQLAlchemy, Alembic) |
| 블롭 저장소 | RustFS (S3 호환) |
| 벡터 저장소 | Qdrant |
| 임베딩 | Upstage solar-embedding-2 (passage/query 쌍) |
| 파싱 서비스 | MinerU, doc-extract API |
| LLM / VLM | 자체 호스팅 OpenAI 호환 엔드포인트 |

두 가지 고정 원칙:

- **프론트엔드 빌드 스텝 없음.** npm, 번들러, React 금지. 인터랙션이 필요하면
  HTMX를 먼저, 작은 인라인 `<script>`를 그다음에 사용합니다.
- **Postgres에는 메타데이터와 상태, RustFS에는 바이트.** PDF나 이미지 블롭을
  Postgres에 저장하지 않으며, RustFS의 파일명을 어떤 것의 원본 소스로도
  취급하지 않습니다.

## 시작하기

### 요구 사항

- Python 3.13+, [uv](https://docs.astral.sh/uv/)
- PostgreSQL, RustFS(또는 S3 호환 스토리지), Qdrant 접근 가능 환경

### 설정

프로젝트 루트에 `.env` 파일을 만들고 다음 변수를 채웁니다.

| 변수 | 설명 |
| --- | --- |
| `DATABASE_URL` | PostgreSQL 연결 문자열 (`postgresql://…`) |
| `RUSTFS_ENDPOINT_URL` | RustFS S3 API 엔드포인트 (웹 콘솔 주소 아님) |
| `RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY` | RustFS 자격 증명 |
| `BUCKET_DOCUMENTS` / `BUCKET_DERIVED` | 원본 PDF 버킷 / 파생 산출물 버킷 |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | 텍스트 LLM (OpenAI 호환) |
| `VLM_*` | 그림 설명용 비전 모델 (기본값 있음) |
| `UPSTAGE_API_KEY` | Upstage 임베딩 API 키 |
| `QDRANT_URL` / `QDRANT_COLLECTION` | Qdrant 주소와 컬렉션 이름 |
| `DOC_EXTRACT_BASE_URL` | 목차 추출용 doc-extract 서비스 |
| `MINERU_BASE_URL` | MinerU 파싱 서비스 |

전체 목록과 기본값은 `app/config.py`를 참고하세요.

### 실행

```bash
uv sync                                    # 환경 설치/동기화
uv run python -m alembic upgrade head      # DB 마이그레이션
uv run python -m fastapi dev app/main.py   # 개발 서버 (자동 리로드)
```

```bash
uv run python -m pytest                    # 테스트
uv run python -m ruff check --fix .        # 린트
uv run python -m ruff format .             # 포맷
```

> **Windows 참고:** Python은 항상 `uv run python -m <tool>` 형태로 실행하세요.
> Smart App Control이 켜진 Windows 11에서는 uv가 생성하는 `.venv/Scripts/`의
> 서명되지 않은 `.exe` 셤(shim)이 차단됩니다(오류 4551). `-m` 형태는 서명된
> `python.exe`로 실행되므로 문제가 없습니다.

## 프로젝트 구조

```
app/
  main.py            # FastAPI 앱, 라우터 등록, 시작 시 연결
  config.py          # 환경 변수 기반 설정 (pydantic-settings)
  db.py              # 엔진 / 세션 수명 주기
  models/            # ORM 모델 — 애그리거트당 한 모듈
  schemas/           # pydantic 요청/응답 모델
  routers/           # HTTP 라우트 (얇게 유지)
  services/          # 비즈니스 로직 — 실제 작업은 여기서
  storage/           # RustFS 클라이언트 래퍼
  llm/ mineru/ doc_extract/ embedding/ vectors/   # 외부 서비스 클라이언트
  templates/         # Jinja2 — base.html + 기능별 디렉터리
  static/            # CSS, 소량의 JS
tests/
alembic/             # DB 마이그레이션
```

라우터는 얇게 유지합니다: 입력 파싱 → 서비스 호출 → 템플릿 렌더링 또는 JSON
반환. 라우터 안에서 SQL이나 스토리지 호출을 하지 않습니다.

## 설계 원칙

- **멱등성** — 같은 전략을 같은 문서에 다시 실행해도 행이 중복되거나 상태가
  깨지지 않습니다. 재실행은 새 Run을 만들 뿐, 이전 Run을 덮어쓰지 않습니다.
- **가역성** — 원본 PDF나 이전 Run의 산출물을 영구 삭제하지 않습니다. 삭제는
  삭제 표시(soft delete)를 의미합니다.
- **규모 의식** — 모든 목록은 페이지네이션(기본 50건). 배치 작업은 명시적이고
  재개 가능하며, HTTP 요청 안에서 실행되지 않습니다.
- **침묵하는 폴백 금지** — 추출 실패는 오류와 함께 실패로 기록합니다. 빈
  문자열로 조용히 넘어가지 않습니다.

---

# partial-server (English)

An internal **control panel for the ingestion stage of a RAG pipeline**.
It is not the RAG system itself — it does not answer questions and it does not
serve retrieval traffic. It is the human-in-the-loop tool that decides *what*
gets ingested, *how* it gets parsed, and *whether the result is good enough to
keep*.

The corpus is roughly **9,000 PDFs**. They are not arbitrary: documents cluster
into a small number of types and categories, and documents inside a cluster are
structurally similar (same layout, same header/footer, same table shapes). That
similarity is the core design assumption of this project.

## Workflow

1. Register PDFs and assign each one to a type/category.
2. Hand-tune the parsing strategy for a few **representative** documents per
   category.
3. Inspect the extracted output in the browser, accept or reject it.
4. Promote the accepted strategy to the rest of the category as a batch run.

The tool is optimised for **inspecting one document carefully**, not for
bulk-processing throughput. Batch execution is the payoff, not the focus.

## Pipeline stages

From upload to retrieval, each stage has its own screen.

| Stage | Description |
| --- | --- |
| Documents | PDF upload, sha256-based deduplication, storage in RustFS |
| Parsing | Full-document parsing via MinerU — markdown, JSON, page images |
| Headers | Header/outline extraction from contents pages (doc-extract + LLM) |
| Sections | Splitting body text by headers, table normalization, VLM figure descriptions |
| Grouping | Grouping sections into semantic units |
| Chunking | Tokenizer-based chunk generation for retrieval |
| Summarizing | Chunk/section summaries |
| Embedding | Vectorization via the Upstage embedding API, stored in Qdrant |
| Retrieval | Inspection search screen — embed the query, search Qdrant |
| Client API | SSE-based Q&A API — answers with sources |

## Stack

| Concern | Choice |
| --- | --- |
| Runtime / packaging | Python 3.13+ + `uv` |
| Web framework | FastAPI |
| Templating | Jinja2, server-rendered |
| Interactivity | HTMX (no build step, no SPA framework) |
| Metadata store | PostgreSQL (psycopg 3, SQLAlchemy, Alembic) |
| Blob store | RustFS (S3-compatible) |
| Vector store | Qdrant |
| Embeddings | Upstage solar-embedding-2 (passage/query pair) |
| Parsing services | MinerU, doc-extract API |
| LLM / VLM | Self-hosted OpenAI-compatible endpoints |

Two hard constraints:

- **No frontend build step.** No npm, no bundler, no React. If a page needs
  interactivity, reach for HTMX first and a small inline `<script>` second.
- **Postgres holds metadata and state; RustFS holds bytes.** Never store PDF or
  image blobs in Postgres, and never treat filenames in RustFS as the source of
  truth for anything.

## Getting started

### Requirements

- Python 3.13+, [uv](https://docs.astral.sh/uv/)
- Access to PostgreSQL, RustFS (or any S3-compatible storage), and Qdrant

### Configuration

Create a `.env` file in the project root with the following variables:

| Variable | Description |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection string (`postgresql://…`) |
| `RUSTFS_ENDPOINT_URL` | RustFS S3 API endpoint (not the web console) |
| `RUSTFS_ACCESS_KEY` / `RUSTFS_SECRET_KEY` | RustFS credentials |
| `BUCKET_DOCUMENTS` / `BUCKET_DERIVED` | Source PDF bucket / derived artifact bucket |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | Text LLM (OpenAI-compatible) |
| `VLM_*` | Vision model for figure descriptions (has defaults) |
| `UPSTAGE_API_KEY` | Upstage embedding API key |
| `QDRANT_URL` / `QDRANT_COLLECTION` | Qdrant address and collection name |
| `DOC_EXTRACT_BASE_URL` | doc-extract service for outline extraction |
| `MINERU_BASE_URL` | MinerU parsing service |

See `app/config.py` for the full list and defaults.

### Running

```bash
uv sync                                    # install / align the environment
uv run python -m alembic upgrade head      # database migrations
uv run python -m fastapi dev app/main.py   # dev server with autoreload
```

```bash
uv run python -m pytest                    # tests
uv run python -m ruff check --fix .        # lint
uv run python -m ruff format .             # format
```

> **Windows note:** always run Python through `uv run python -m <tool>`. On
> Windows 11 with Smart App Control enabled, the unsigned `.exe` launcher shims
> uv generates in `.venv/Scripts/` are blocked (error 4551). The `-m` form runs
> through the signed `python.exe` and works fine.

## Project layout

```
app/
  main.py            # FastAPI app, router registration, startup wiring
  config.py          # settings from env (pydantic-settings)
  db.py              # engine / session lifecycle
  models/            # ORM models — one module per aggregate
  schemas/           # pydantic request/response models
  routers/           # HTTP routes, thin
  services/          # business logic, the real work lives here
  storage/           # RustFS client wrapper
  llm/ mineru/ doc_extract/ embedding/ vectors/   # external service clients
  templates/         # Jinja2 — base.html + one dir per feature
  static/            # css, small js
tests/
alembic/             # database migrations
```

Routers stay thin: parse input, call a service, render a template or return
JSON. No SQL and no storage calls inside a router.

## Design principles

- **Idempotency** — re-running the same strategy on the same document never
  duplicates rows or corrupts state. A re-run creates a new Run; it never
  overwrites an old one.
- **Reversibility** — nothing permanently destroys a source PDF or a previous
  Run's artifacts. Deletion means marking deleted.
- **Scale awareness** — every list view is paginated (default 50). Batch
  operations are explicit, resumable, and never run inside an HTTP request.
- **No silent fallbacks** — a failed extraction is recorded as failed, with the
  error, never quietly returned as an empty string.
