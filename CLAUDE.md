# CLAUDE.md

## Project
Support-ticket draft service. Helpdesk webhook → background LLM draft from a knowledge base → operator approves/rejects in a web UI → approved reply is sent via the helpdesk API.

**The single source of truth for requirements and design decisions is `SPEC.md`. Read it before any task. If a task conflicts with SPEC.md, stop and ask instead of improvising.**

## Stack
- Python 3.12, uv, ruff
- FastAPI + Jinja2 (API and operator UI)
- PostgreSQL 16, SQLAlchemy 2.0, Alembic — async engine (asyncpg) in FastAPI, sync engine (psycopg) in Celery tasks; shared models
- Celery with RabbitMQ broker; Redis only for the global LLM rate limiter
- docker compose for everything

## Project layout
```
app/
  main.py            # FastAPI app, routers
  config.py          # settings from env
  db.py              # engine, session
  models.py          # SQLAlchemy models
  schemas.py         # Pydantic schemas
  webhooks.py        # POST /webhooks/helpdesk
  operator.py        # operator UI routes
  kb.py              # knowledge base search (Postgres FTS)
  llm.py             # LLM client incl. no-key mode
  rate_limit.py      # Redis global limiter
  helpdesk_client.py # outgoing replies
  tasks.py           # Celery tasks: generate_draft, send_reply, reaper
  celery_app.py
  templates/
helpdesk_mock/       # separate mock service
seed/kb/             # knowledge base articles (markdown)
scripts/             # send_webhooks.py, create seed, etc.
alembic/
tests/
```

## Non-negotiable rules
1. **Webhook never calls the LLM or waits on workers.** Insert into DB, enqueue after commit, return 202.
2. **Deduplication lives in the database:** unique index on `event_id` + `INSERT ... ON CONFLICT DO NOTHING`. No "check then insert" in Python.
3. **Every status transition is a conditional atomic UPDATE** (`WHERE status = <expected> RETURNING`). If 0 rows are updated, the task exits quietly.
4. **Tasks must be idempotent** — safe to run twice.
5. Celery: `acks_late=True`, `task_reject_on_worker_lost=True`, `worker_prefetch_multiplier=1`, durable queues.
6. Outgoing replies always send `Idempotency-Key: <ticket.id>`.
7. The global LLM limit (60/min) is enforced via Redis, not Celery `rate_limit`.
8. No-key mode must still make a real HTTP request to the provider, then return a static response after a random 5–30 s delay.
9. Never commit secrets. Only `.env.example` goes into git.
10. In `generate_draft`, before a Celery `retry`, revert the status with a conditional UPDATE (`generating → received`) so the retried task passes step 1 like the first attempt. `send_reply` stays in `sending` between retries (step 1 accepts `approved|sending`); duplicates are prevented by the `Idempotency-Key`.

## Workflow
- Work strictly stage by stage as listed in SPEC.md §11. One stage per task.
- Before writing code for a stage: briefly state the plan and the files you will touch, then wait for confirmation.
- Keep changes small and focused; do not refactor unrelated code.
- After each stage: run `ruff check`, run tests, then stop so the changes can be reviewed and committed.
- After each stage, append to docs/decisions.md any deviations from SPEC.md and any corrections (mine or yours), with reasons.
- Do not squash or rewrite git history.

## Code style
- Type hints everywhere. Async DB access in FastAPI routes; sync sessions inside Celery tasks.
- Settings only through `config.py` (pydantic-settings).
- Log every status transition with ticket id.
- Tests with pytest; DB-dependent tests run against the compose Postgres, in a separate `support_test` database that `tests/conftest.py` creates and migrates (never the main one).

## Commands
- Start: `docker compose up --build`
- Migrations: `docker compose run --rm api alembic upgrade head`
- Tests: `uv run pytest` (locally, compose Postgres on 127.0.0.1:5432 must be up; dev deps are not in the image)
- Lint: `uv run ruff check .`
- Load test: `uv run python scripts/send_webhooks.py --count 500 --duplicates`
