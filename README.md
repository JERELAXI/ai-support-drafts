# AI Support Drafts

A support-ticket draft service. The helpdesk sends a webhook for every new ticket. A background worker searches a knowledge base, asks an LLM for a reply draft, and an operator approves (optionally editing) or rejects it in a small web UI. Approved replies are sent back through the helpdesk API.

Requirements and design decisions live in [`SPEC.md`](SPEC.md) (in Ukrainian). Every deviation from the spec and every correction made along the way is logged in [`docs/decisions.md`](docs/decisions.md).

## Live demo

| What | Link |
|---|---|
| Webhook | `POST` <https://34-173-82-162.sslip.io/webhooks/helpdesk> |
| Operator UI | <https://34-173-82-162.sslip.io/operator> (HTTP Basic Auth) |
| Health | <https://34-173-82-162.sslip.io/health> |
| Helpdesk mock replies | <https://34-173-82-162.sslip.io/mock/replies> |

Operator credentials are provided separately. Only `GET /mock/replies` of the helpdesk mock is public; every other `/mock/*` path returns `404`.

Send a test ticket to the public webhook:

```bash
curl -i -X POST https://34-173-82-162.sslip.io/webhooks/helpdesk \
  -H 'Content-Type: application/json' \
  -d '{
        "event_id": "evt-demo-'"$(date +%s)"'",
        "ticket_id": "T-demo-1",
        "customer_email": "customer@example.com",
        "message": "Як експортувати звіт у PDF?",
        "reply_url": "http://helpdesk-mock:8001/tickets/T-demo-1/replies"
      }'
# HTTP/1.1 202 Accepted  {"status":"accepted","duplicate":false}
```

Keep `reply_url` as `http://helpdesk-mock:8001/tickets/<id>/replies` (with `<id>` equal to `ticket_id`): the approved reply is sent by the worker from inside the compose network, where the mock is reachable by its service name, not by a public URL. The draft shows up in the operator UI in 5–30 s (no-key mode), and after approval the reply appears in <https://34-173-82-162.sslip.io/mock/replies>.

## Architecture

```mermaid
flowchart LR
    HD[Helpdesk] -- "POST /webhooks/helpdesk" --> API
    OP[Operator browser] -- "/operator (Basic Auth)" --> API
    subgraph compose[docker compose]
        API[api<br/>FastAPI + Jinja2]
        PG[(PostgreSQL 16<br/>tickets, kb_articles + FTS)]
        MQ[[RabbitMQ<br/>durable queues]]
        W[worker<br/>Celery, threads pool]
        B[beat<br/>reaper, every minute]
        R[(Redis<br/>global LLM limiter)]
        MOCK[helpdesk-mock]
        MIG[migrate<br/>Alembic + KB seed]
    end
    API -- "INSERT ... ON CONFLICT DO NOTHING<br/>conditional UPDATEs" --> PG
    API -- "enqueue after commit" --> MQ
    B -- "reap_stuck" --> MQ
    MQ --> W
    W -- "FTS, status transitions" --> PG
    W -- "INCR llm:rl:&lt;minute&gt;" --> R
    W -- "chat completion" --> LLM[LLM provider]
    W -- "POST reply_url<br/>Idempotency-Key: ticket.id" --> MOCK
    MIG --> PG
```

### Ticket statuses

Every arrow is a conditional atomic `UPDATE ... WHERE status = <expected> RETURNING`. If no row is updated, someone else already handled the transition and the task exits quietly.

```mermaid
stateDiagram-v2
    [*] --> received: webhook
    received --> generating: generate_draft
    generating --> received: retry / rate limited / reaper (5 min)
    generating --> drafted
    generating --> generation_failed: permanent error or attempts exhausted
    drafted --> approved: operator approve
    drafted --> rejected: operator reject
    approved --> sending: send_reply
    sending --> sending: retry (5xx, 409, 429, timeout)
    sending --> sent: 201
    sending --> send_failed: other 4xx or attempts exhausted
    sent --> [*]
    rejected --> [*]
    generation_failed --> [*]
    send_failed --> [*]
```

## Running

```bash
cp .env.example .env
# edit .env: set OPERATOR_PASSWORD (and real passwords for Postgres/RabbitMQ)
docker compose up --build
```

- `OPERATOR_USER` and `OPERATOR_PASSWORD` are **required**. Without them none of the app services start, so the operator UI never runs unprotected or with a default password.
- **With an LLM key:** set `LLM_API_KEY` (and optionally `LLM_MODEL`, `LLM_BASE_URL`) in `.env`. Drafts come from the model.
- **Without a key** (leave `LLM_API_KEY` empty): the worker still makes a real HTTP request to the provider (expected `401`), then returns a static draft built from the found articles after a random 5–30 s delay. The rate limiter, retries and statuses behave exactly as with a key.
- The `migrate` service runs `alembic upgrade head` and seeds the knowledge base from `seed/kb/*.md`. The seed is baked into the image, so **after changing `seed/kb/` run `docker compose build migrate`** (then `docker compose up migrate`), otherwise the old articles are loaded.

Services: `api` (port 8000), `worker`, `beat`, `postgres` (127.0.0.1:5432), `rabbitmq` (management UI on 127.0.0.1:15672), `redis`, `helpdesk-mock` (port 8001), `migrate`.

### Sending a test ticket

```bash
curl -i -X POST http://localhost:8000/webhooks/helpdesk \
  -H 'Content-Type: application/json' \
  -d '{
        "event_id": "evt-demo-1",
        "ticket_id": "T-1",
        "customer_email": "customer@example.com",
        "message": "Як експортувати звіт у PDF?",
        "reply_url": "http://helpdesk-mock:8001/tickets/T-1/replies"
      }'
# HTTP/1.1 202 Accepted  {"status":"accepted","duplicate":false}
# Repeat the same request: still 202, now "duplicate": true.
```

`reply_url` uses the compose hostname `helpdesk-mock` because the worker calls it from inside the compose network.

Then open <http://localhost:8000/operator> (log in with `OPERATOR_USER` / `OPERATOR_PASSWORD`). The page refreshes every 10 s unless you are editing. Once the ticket is `drafted`, edit the text if needed and approve it. To check what the helpdesk received:

```bash
curl http://localhost:8001/replies
```

The mock fails 20 % of requests with `500` and answers 10 % slowly (stores the reply first, then waits longer than `HELPDESK_TIMEOUT`), so retries are visible in `docker compose logs worker`. Tune it with `MOCK_ERROR_RATE`, `MOCK_SLOW_RATE`, `MOCK_SLOW_SECONDS`.

## Tests

Tests run locally, not in the container (the image is built without dev dependencies):

```bash
docker compose up -d postgres   # or the whole stack
uv sync
uv run pytest
```

- DB-dependent tests use a separate database, `support_test`, in the compose Postgres. `tests/conftest.py` creates it if missing and runs `alembic upgrade head`, so this works on a clean checkout. The main `support` database is never touched (the live worker and reaper run against it, and the tests truncate tables).
- Override the connection with `TEST_DATABASE_URL` (default `postgresql://support:change-me@127.0.0.1:5432/support_test`, matching `.env.example`).
- If Postgres is not reachable, the DB tests are skipped and the rest still run.

The three DB tests cover the key guarantees: concurrent duplicate webhooks → one row and one enqueued task; concurrent double approve → one transition to `approved` and one `send_reply`; `send_reply` with the helpdesk answering `500` then `201` → `sent`, two requests with the same `Idempotency-Key`. Without a DB: no-key mode (a real request reaches the provider, which answers `401`, and the static draft cites the best article; the 5–30 s delay is patched out), the rate limiter on `fakeredis` (with a limit of 3 the 4th `acquire()` is refused with `retry_after` until the next minute), the helpdesk client error classification, the mock (idempotency, injected 500s, slow answers) and operator auth.

Lint: `uv run ruff check .`

## Load test

```bash
uv run python scripts/send_webhooks.py --count 500 --duplicates
```

Options: `--count N` distinct events, `--duplicates` sends every event 3 times concurrently, `--url` webhook URL (default `http://localhost:8000/webhooks/helpdesk`), `--concurrency` max requests in flight (default 50). At the end it prints status codes, new vs duplicate counts, errors, latency (min/p50/p95/max), the number of responses slower than 2 s, and total time.

The real peak from the task is 500 tickets in 10 minutes, i.e. under 1 request per second. The test sends 500 events × 3 copies = 1500 requests at concurrency 50 in about 8–10 s, which is far above that peak.

Correctness held in every run: 1500 × `202`, 500 new / 1000 duplicates, exactly 500 rows in the database, no enqueue failures. Latency, measured locally (Docker Desktop on macOS, 4 CPUs for the VM, the whole stack running):

| API DB pool | `--concurrency` | req/s | p50 | p95 | max | > 2 s |
|---|---|---|---|---|---|---|
| 5 + 10 (SQLAlchemy default) | 50 | 160 | 197 ms | 943 ms | 2.4 s | 3 |
| 5 + 10 | 15 | — | 29 ms | 87 ms | 215 ms | 0 |
| 20 + 10 (current), 3 runs | 50 | 86–185 | 147–358 ms | 884–1915 ms | 2.5–5.3 s | 4–59 |
| 20 + 10 (current) | 15 | 338 | 32 ms | 106 ms | 363 ms | 0 |

The pool is configurable (`API_DB_POOL_SIZE`, `API_DB_MAX_OVERFLOW`), but it is **not** what limits the tail at concurrency 50: a bigger pool, an in-memory broker instead of RabbitMQ, 4 uvicorn workers, and running the load from inside the compose network all gave the same picture, and even `/docs` (no DB) slows down the same way. Above roughly 15 concurrent requests throughput drops (≈ 450 → ≈ 190 req/s) while the API container uses about a third of its CPUs, so the limit appears to be the local Docker environment rather than the service; this was not profiled further. At the real peak the webhook answers in tens of milliseconds. Re-measure on the deployment target.

Note that the load test creates real tickets in the main database; the worker will generate drafts for all of them (within the 60/min LLM limit).

## Deployment

The demo runs on a clean Ubuntu VM in GCP with only ports 22, 80 and 443 open. HTTPS comes from [Caddy](https://caddyserver.com/) with an automatic Let's Encrypt certificate for an [sslip.io](https://sslip.io/) hostname (`34-173-82-162.sslip.io` resolves to `34.173.82.162`), so no own domain is needed. Port 80 must stay open: Let's Encrypt uses it for the HTTP-01 challenge, and Caddy redirects it to HTTPS.

`docker-compose.prod.yml` is an override on top of `docker-compose.yml`: it adds the `caddy` service (ports 80/443, certificates in the `caddy_data` volume, config in [`deploy/Caddyfile`](deploy/Caddyfile)) and removes the published ports `8000`/`8001` of `api` and `helpdesk-mock`, so they are reachable only through Caddy over HTTPS. Caddy routes `GET /mock/replies` to the mock (with the `/mock` prefix stripped), returns `404` for any other `/mock/*` path, and sends everything else to `api`.

1. Install Docker (Engine + Compose plugin) and allow your user to run it:

   ```bash
   curl -fsSL https://get.docker.com | sudo sh
   sudo usermod -aG docker "$USER" && newgrp docker
   ```

2. Clone the repository:

   ```bash
   git clone https://github.com/JERELAXI/ai-support-drafts.git && cd ai-support-drafts
   ```

3. Create `.env` with random passwords and the domain (`<ip-with-dashes>.sslip.io` for the VM's external IP):

   ```bash
   cp .env.example .env
   PG_PASS=$(openssl rand -hex 16)
   MQ_PASS=$(openssl rand -hex 16)
   OP_PASS=$(openssl rand -hex 16)
   sed -i \
     -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$PG_PASS|" \
     -e "s|^DATABASE_URL=.*|DATABASE_URL=postgresql://support:$PG_PASS@postgres:5432/support|" \
     -e "s|^RABBITMQ_DEFAULT_PASS=.*|RABBITMQ_DEFAULT_PASS=$MQ_PASS|" \
     -e "s|^CELERY_BROKER_URL=.*|CELERY_BROKER_URL=amqp://support:$MQ_PASS@rabbitmq:5672//|" \
     -e "s|^OPERATOR_PASSWORD=.*|OPERATOR_PASSWORD=$OP_PASS|" \
     -e "s|^DOMAIN=.*|DOMAIN=34-173-82-162.sslip.io|" \
     .env
   echo "Operator password: $OP_PASS"
   ```

   Hex passwords are safe inside the `DATABASE_URL` and `CELERY_BROKER_URL` URLs. Postgres and RabbitMQ apply their passwords only when their volumes are first created, so set them before the first start. Optionally set `LLM_API_KEY`.

4. Start the stack:

   ```bash
   docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
   ```

5. Check it (the first request may take a few seconds while Caddy obtains the certificate):

   ```bash
   curl -i https://34-173-82-162.sslip.io/health
   ```

`DOMAIN` is required by the override: without it `docker compose` refuses to start. Logs: `docker compose -f docker-compose.yml -f docker-compose.prod.yml logs -f caddy api worker`.

### Troubleshooting

**RabbitMQ exits with `.erlang.cookie: eacces`.** On a slow VM (seen on GCP e2-micro) the healthcheck (`rabbitmq-diagnostics`, which runs as root) can create `/var/lib/rabbitmq/.erlang.cookie` in the volume before the server does, so the file ends up owned by root and the server, running as `rabbitmq`, cannot read it. Fix the ownership in the volume once, then start the stack again:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm --no-deps --entrypoint sh rabbitmq -c "chown rabbitmq:rabbitmq /var/lib/rabbitmq/.erlang.cookie && chmod 400 /var/lib/rabbitmq/.erlang.cookie"
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

The cookie persists in the `rabbitmqdata` volume, so this is needed only once.

## Key decisions and trade-offs

Details and the reasoning behind each item are in [`docs/decisions.md`](docs/decisions.md).

- **Deduplication in the database.** A unique index on `event_id` plus `INSERT ... ON CONFLICT DO NOTHING RETURNING id`. It holds for concurrent identical events, which a "check, then insert" in Python does not. Duplicates also get `202`, so the helpdesk stops redelivering.
- **Non-blocking webhook.** The webhook only inserts and commits, then publishes the task off the event loop with a 1 s broker connect timeout, one short retry and a hard 1.5 s cap. If publishing fails, the ticket stays `received` and the reaper picks it up. The webhook never calls the LLM or waits on workers.
- **Conditional transitions.** Every status change is `UPDATE ... WHERE status = <expected> RETURNING`; zero rows means "already handled", and the task exits. Together with `acks_late`, `worker_prefetch_multiplier=1` and durable queues this makes every task idempotent and safe to run twice. Attempt counters live in DB columns, not Celery `max_retries`, so rate-limiter refusals do not burn attempts and worker crashes still count. A Celery beat reaper (`SELECT ... FOR UPDATE SKIP LOCKED`, at most 100 tickets per pass) re-enqueues tickets stuck in `received`, `generating`, `approved` or `sending`.
- **`Idempotency-Key` and the honest "exactly once" limit.** Every reply is sent with `Idempotency-Key: <ticket.id>`, and the ticket stays in `sending` between retries. If the helpdesk accepted a reply but the `201` was lost to a timeout, the retry with the same key returns the same `201` instead of a second reply. This only works because the helpdesk deduplicates by key. Against a helpdesk without idempotency support, exactly-once delivery is impossible: you choose between at-most-once (never retry after a timeout) and at-least-once (retry and risk a duplicate). `send_failed` therefore means "no confirmation received", not "the customer got nothing".
- **Global rate limiter with a fixed window.** 60 LLM requests/min across all workers via Redis `INCR llm:rl:<minute>` + `EXPIRE`. Celery's `rate_limit` is per worker, so it does not fit. A refused task goes back to `received` and retries at the start of the next minute without spending an attempt. Trade-off: at a minute boundary up to 2× the limit can pass within a few seconds; the provider then answers `429`, which is a normal retryable error.
- **Postgres full-text search instead of embeddings.** It works without an API key (embeddings would need one) and is enough for a 10-article KB. Query words are lowercased, words shorter than 3 characters are dropped, the rest are cut to 5-character stems and OR-ed with prefix match (`to_tsquery('simple', 'експо:* | pdf:*')`), ranked by `ts_rank`. The `simple` config is used because Postgres has no Ukrainian dictionary. Trade-offs: occasional false matches on stems and noise from common words; the prompt tells the LLM to answer only from relevant articles.
- **Sync sessions in the worker.** FastAPI uses an async engine (asyncpg); Celery tasks use a sync engine (psycopg) with the shared models. The worker runs `--pool=threads --concurrency=20` because tasks are IO-bound (peak ~50 tickets/min × ~17 s per LLM call ≈ 15 concurrent tasks), and the sync pool size matches the thread count.
- **Operator auth: HTTP Basic, deliberately minimal.** All `/operator` routes are protected at the router level with a constant-time credential check; `/health`, the webhook and `/kb/search` stay open. Known limits: Basic Auth sends the password base64-encoded, so **HTTPS is mandatory** in deployment; there is no brute-force protection, no logout (the browser caches credentials until it is closed), no CSRF protection for approve/reject, and a single shared account, so the log does not show which operator approved a reply.

## How I worked with the agent

The project was built with Claude Code as a pair, with me reviewing every step:

- **`SPEC.md` first.** Requirements, data model, status machine, flows, rate limiting and the test list were written up front and act as the single source of truth. When the code showed the spec was wrong (e.g. `websearch_to_tsquery` returning almost nothing for full-sentence tickets), the spec was updated rather than silently diverged from.
- **`CLAUDE.md` for the rules.** Stack, layout, commands and the non-negotiable rules (DB dedup, non-blocking webhook, conditional transitions, idempotent tasks, Celery settings, `Idempotency-Key`, Redis limiter, no-key mode, no secrets) are loaded into every session, and the agent must stop and ask when a task conflicts with the spec.
- **One stage per branch.** Work followed the stages in SPEC §11 (setup, webhook, knowledge base, generation, operator UI, sending, reaper, tests and docs), each on its own branch and merged after review. Before writing code, the agent states a plan and the files it will touch and waits for confirmation.
- **Review of each stage.** After each stage: `ruff check`, tests, a live check against the compose stack, then my review and commit. Several issues were caught at this point, both in the agent's code and in my own requirements.
- **A second Claude as reviewer.** I also went through the plan and the result of every stage in a separate chat with Claude acting as a reviewer. Decisions were mine; the corrections that came out of these reviews were recorded in `docs/decisions.md`.
- **Decision log.** [`docs/decisions.md`](docs/decisions.md) records every deviation from the spec and every correction (the agent's and mine) with the reason, so the history of the design is reviewable.

## What's next for production

- **pgvector and hybrid search** for a larger knowledge base: embeddings plus FTS, merged by rank, instead of FTS-only stem matching.
- **Queue monitoring:** RabbitMQ queue depth and consumer count, task latency, worker liveness, LLM limiter usage.
- **Alerts on `send_failed`** (and `generation_failed`), plus on tickets stuck in a status longer than the reaper thresholds.
- **A retry button for failed tickets** in the operator UI (`send_failed → approved`, `generation_failed → received` as conditional transitions).
- **Sliding-window or token-bucket limiter** to remove the 2× burst at minute boundaries.
- Also: per-operator accounts instead of a shared Basic Auth login, and load-testing the webhook on the real deployment target (uvicorn workers, `uvicorn[standard]`, pool size).
