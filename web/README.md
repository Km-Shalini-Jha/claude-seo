# Claude SEO SaaS Console

Browser console for customer SEO audits with SaaS foundations:
accounts, email verification tokens, password reset tokens, plan quotas,
optional Stripe Checkout/webhooks, projects, saved sites, protected audit jobs,
Postgres/SQLite-backed worker, durable history, report exports, audit logs, admin
endpoints, legal pages, and shareable completed-report links.

## Run Locally

```bash
CLAUDE_SEO_PYTHON=/opt/homebrew/bin/python3.11 ./bin/claude-seo setup --skip-browser
CLAUDE_SEO_PYTHON=/opt/homebrew/bin/python3.11 /opt/homebrew/bin/python3.11 -m uvicorn web.backend:app --host 127.0.0.1 --port 8001
```

Open:

```text
http://127.0.0.1:8001
```

## Test

```bash
/opt/homebrew/bin/python3.11 web/e2e_test.py
```

The E2E test starts a temporary local server and separate worker process,
enables fake script outputs,
creates two accounts, creates a project and site, runs a full audit, polls it
to completion, verifies history, verifies report/share exports, exercises email
verification, password reset, dev billing upgrade, signed Stripe webhook
handling, admin endpoints, backup export, legal pages, and checks cross-user
job isolation.

Browser E2E:

```bash
/Users/shalinijha/Documents/claude-seo/.venv/bin/python web/browser_e2e_test.py
```

## Customer Workflow

1. Sign up or log in.
2. Create a client project.
3. Save a website under that project.
4. Run `Full SEO Audit` or an individual audit module.
5. Review findings and raw JSON.
6. Export HTML/JSON or share the completed report link.

## Storage

Production data should be stored in hosted PostgreSQL. Set `DATABASE_URL` to
your Supabase Transaction pooler connection string:

```bash
DATABASE_URL='postgresql://postgres.<project-ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require'
```

When `DATABASE_URL` or `CLAUDE_SEO_DATABASE_URL` is present, the app creates
and uses the hosted Postgres tables automatically. Without that variable,
development data falls back to local SQLite:

```text
web/data/console.sqlite3
```

Tables include users, sessions, verification tokens, password reset tokens,
projects, sites, jobs, audit logs, Stripe event receipts, and email outbox.

## Environment

```bash
CLAUDE_SEO_WEB_DATA=/path/to/data
DATABASE_URL=postgresql://...
CLAUDE_SEO_WEB_AUTO_WORKER=1
CLAUDE_SEO_WEB_RATE_LIMIT=30
CLAUDE_SEO_WEB_FAKE_RUNS=1
CLAUDE_SEO_WEB_DEV_EMAIL_TOKENS=0
CLAUDE_SEO_WEB_ORIGINS=http://127.0.0.1:8001
CLAUDE_SEO_PUBLIC_URL=http://127.0.0.1:8001
CLAUDE_SEO_LAUNCHER=/path/to/bin/claude-seo
CLAUDE_SEO_PYTHON=/opt/homebrew/bin/python3.11
CLAUDE_SEO_STRIPE_SECRET_KEY=sk_live_...
CLAUDE_SEO_STRIPE_WEBHOOK_SECRET=whsec_...
CLAUDE_SEO_STRIPE_PRICE_PRO=price_...
CLAUDE_SEO_STRIPE_PRICE_AGENCY=price_...
```

## Separate Worker

For production-like local runs, disable the app's in-process worker and run the
worker separately:

```bash
CLAUDE_SEO_WEB_AUTO_WORKER=0 /opt/homebrew/bin/python3.11 -m uvicorn web.backend:app --host 127.0.0.1 --port 8001
CLAUDE_SEO_WEB_AUTO_WORKER=0 /opt/homebrew/bin/python3.11 -m web.worker
```

## Docker Compose

```bash
cd web
docker compose up --build
```

The compose setup runs `web` and `worker` services against a shared data volume.

## Email

Without SMTP configuration, verification and password-reset messages are stored
in `email_outbox`. Set SMTP variables in `.env.example` to send real email.
For local tests only, `CLAUDE_SEO_WEB_DEV_EMAIL_TOKENS=1` exposes tokens through
`/api/dev/email-outbox`.

## Billing Scaffold

Plans are enforced locally:

- `free`: 10 audits/hour, 3 projects, 10 sites
- `pro`: 60 audits/hour, 25 projects, 100 sites
- `agency`: 240 audits/hour, 250 projects, 1000 sites

`/api/billing/checkout` creates a hosted Stripe Checkout Session when
`CLAUDE_SEO_STRIPE_SECRET_KEY` and a matching plan price ID are configured.
Without Stripe settings, it returns a development-mode response and
`/api/billing/dev-upgrade` upgrades the logged-in account without external
payment infrastructure.

Configure this webhook endpoint in Stripe:

```text
POST https://your-app.example.com/api/billing/webhook
```

The endpoint verifies the raw payload using `Stripe-Signature` and
`CLAUDE_SEO_STRIPE_WEBHOOK_SECRET`, stores event IDs for idempotency, and
handles:

- `checkout.session.completed`
- `customer.subscription.created`
- `customer.subscription.updated`
- `customer.subscription.deleted`
- `invoice.payment_failed`

## Admin And Ops

The first registered account becomes `admin`. Admin endpoints include:

- `/api/admin/summary`
- `/api/admin/users`
- `/api/admin/jobs`
- `/api/admin/audit-logs`
- `/api/admin/backup.json`

Operational endpoints:

- `/api/health`: Claude SEO runtime readiness.
- `/api/ready`: runtime + database readiness and queued-job count.

Legal placeholders:

- `/terms`
- `/privacy`

## Production Still Needed

Before a public launch, replace SQLite with hosted Postgres, replace the local
worker thread with a managed queue, add real email delivery, configure HTTPS,
managed secrets, observability, backups, and stricter abuse controls.
