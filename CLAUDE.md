# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this project is

`partial-server` is an internal **control panel for the ingestion stage of a RAG
pipeline**. It is not the RAG system itself — it does not answer questions and it
does not serve retrieval traffic. It is the human-in-the-loop tool that decides
*what* gets ingested, *how* it gets parsed, and *whether the result is good enough
to keep*.

The corpus is roughly **9,000 PDFs**. They are not arbitrary: documents cluster
into a small number of **types and categories**, and documents inside a cluster
are structurally similar (same layout, same header/footer, same table shapes).
That similarity is the core design assumption of this project.

The workflow it exists to support:

1. Register PDFs and assign each one to a type/category.
2. Hand-tune the parsing strategy for a few **representative** documents per
   category.
3. Inspect the extracted output in the browser, accept or reject it.
4. Promote the accepted strategy to the rest of the category as a batch run.

So the tool is optimised for **inspecting one document carefully**, not for
bulk-processing throughput. Batch execution is the payoff, not the focus.

## Stack

| Concern | Choice |
| --- | --- |
| Runtime / packaging | Python + `uv` (`pyproject.toml`, `uv.lock`) |
| Web framework | FastAPI |
| Templating | Jinja2, server-rendered |
| Interactivity | HTMX (no build step, no SPA framework) |
| Metadata store | PostgreSQL |
| Blob store | RustFS (S3-compatible) |

Two hard constraints:

- **No frontend build step.** No npm, no bundler, no React. If a page needs
  interactivity, reach for HTMX first and a small inline `<script>` second.
- **Postgres holds metadata and state; RustFS holds bytes.** Never store PDF or
  image blobs in Postgres, and never treat filenames in RustFS as the source of
  truth for anything.

## Commands

```bash
uv sync                          # install / align the environment
uv run fastapi dev app/main.py   # dev server with autoreload
uv add <package>                 # add a dependency (never edit pyproject by hand)
uv run pytest                    # tests
uv run ruff check --fix .        # lint
uv run ruff format .             # format
```

Always run Python through `uv run`. Do not activate the venv manually and do not
call bare `python` or `pip`.

<!-- TODO: add migration + worker commands once those exist, e.g.
uv run alembic upgrade head
uv run python -m app.worker
-->

## Layout

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
  ingestion/         # parsers, extraction strategies, batch runners
  templates/         # Jinja2 — base.html + one dir per feature
  static/            # css, small js
tests/
```

Routers stay thin: parse input, call a service, render a template or return JSON.
No SQL and no storage calls inside a router.

## Domain model

The names below are the vocabulary of the project. Use them consistently in code,
templates, table names, and commit messages.

- **Document** — one source PDF. Immutable once registered.
- **Category** — the cluster a document belongs to. Drives which strategy applies.
- **Strategy** — a named, versioned parsing configuration (extractor choice, page
  ranges, table handling, OCR on/off, cleanup rules).
- **Run** — one execution of a strategy over one document. Produces artifacts and
  a status. Re-running creates a new Run; it never overwrites an old one.
- **Artifact** — an output of a Run (extracted text, page image, chunk set,
  markdown). Bytes in RustFS, row in Postgres.
- **Review** — a human verdict on a Run: accepted, rejected, needs-work, plus a
  note.

Document lifecycle:

```
registered → categorised → strategy_assigned → parsed → reviewed → promoted
```

`promoted` means the reviewed strategy has been applied to the rest of the
category. A document can move backwards; treat status as mutable state, not as an
append-only log.

## Database conventions

- Postgres is the source of truth for status, lineage, and review verdicts.
- Every table: `id` (UUID), `created_at`, `updated_at`, all timestamps `timestamptz`.
- Content-addressed identity: store `sha256` of each PDF and enforce a unique
  index on it. The same file registered twice is one Document.
- Store the RustFS object key on the row. Never reconstruct a key from a title or
  a user-supplied filename.
- Free-form parser output goes in a `jsonb` column; anything queried or filtered
  on gets a real column.
- Schema changes go through a migration. Never `create_all()` against a database
  that has data in it.

## RustFS conventions

- Access it through `app/storage/`, never with a raw client from a router or a
  service. That wrapper is also what makes tests fixable.
- Key layout:
  ```
  documents/{sha256}.pdf
  runs/{run_id}/text.txt
  runs/{run_id}/pages/{n}.png
  runs/{run_id}/chunks.jsonl
  ```
- Objects are write-once. A correction is a new Run with new keys, not an
  overwrite of an existing key.
- Config comes from env: endpoint, access key, secret key, bucket. Nothing
  hardcoded, no credentials in the repo.

## UI conventions

- Server-rendered Jinja2. All templates extend `templates/base.html`.
- HTMX handles partial updates. A route that serves an HTMX request returns an
  HTML **fragment**, not a full page — keep fragments in
  `templates/<feature>/_partials/`.
- This is an internal tool for a small team. Prefer a dense, readable table over
  a polished dashboard. Legibility of the extracted output beats visual design.
- The single most important screen is the **document review view**: rendered page
  image on one side, extracted output on the other, accept/reject controls
  underneath. Optimise that screen before anything else.

## Working rules for Claude Code

**Scale.** 9,000 documents is enough that a careless loop is a real problem.

- Never write code that loads all documents into memory or iterates the whole
  corpus in a request handler.
- Every list view is paginated. Default page size 50.
- Batch operations are explicit, resumable, and report progress. They never run
  inside an HTTP request.
- When testing something new, run it against a handful of documents first. There
  should always be an easy way to say "just these 5".

**Idempotency.** Ingestion will be re-run constantly during tuning. Re-running the
same strategy on the same document must not duplicate rows or corrupt state.

**Reversibility.** Nothing in this tool should permanently destroy a source PDF or
a previous Run's artifacts. Deletion means marking deleted.

**When making changes:**

- Confirm the domain vocabulary above before introducing a new noun. If a concept
  genuinely doesn't fit, say so rather than overloading an existing term.
- Prefer boring, explicit code. This project will be read more than written.
- Type-hint everything. Validate at the boundary with pydantic.
- If a task touches parsing behaviour, show the effect on a sample document
  rather than only describing the change.
- Ask before adding a dependency, changing the schema, or introducing background
  workers, queues, or async processing.

**Avoid:**

- Adding a frontend framework or a build step.
- Storing blobs in Postgres.
- Silent fallbacks in parsing code — a failed extraction must be recorded as
  failed, with the error, not quietly returned as an empty string.
- Speculative abstraction for parsers that don't exist yet.

## Open decisions

<!-- Keep this list current. Claude should ask rather than assume on these. -->

- ORM / driver: SQLAlchemy vs. raw asyncpg — **not decided**.
- Migration tool: Alembic assumed but **not set up**.
- PDF extraction library per document type — **undecided**; expect several.
- OCR path for scanned documents — **not yet designed**.
- Chunking strategy and embedding model live downstream of this tool and are
  **out of scope** here.
- Auth: currently none. Internal use only, not exposed publicly.