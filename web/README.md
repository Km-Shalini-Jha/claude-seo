# Claude SEO SaaS Console

Browser console for customer SEO audits with local SaaS foundations:
accounts, projects, saved sites, protected audit jobs, durable SQLite history,
and shareable completed-report links.

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

The E2E test starts a temporary local server, enables fake script outputs,
creates two accounts, creates a project and site, runs a full audit, polls it
to completion, verifies history, verifies the public share endpoint, and checks
cross-user job isolation.

## Customer Workflow

1. Sign up or log in.
2. Create a client project.
3. Save a website under that project.
4. Run `Full SEO Audit` or an individual audit module.
5. Review findings and raw JSON.
6. Share the completed report link.

## Storage

Development data is stored in SQLite:

```text
web/data/console.sqlite3
```

Tables include users, sessions, projects, sites, and jobs.

## Environment

```bash
CLAUDE_SEO_WEB_DATA=/path/to/data
CLAUDE_SEO_WEB_RATE_LIMIT=30
CLAUDE_SEO_WEB_FAKE_RUNS=1
CLAUDE_SEO_WEB_ORIGINS=http://127.0.0.1:8001
CLAUDE_SEO_LAUNCHER=/path/to/bin/claude-seo
CLAUDE_SEO_PYTHON=/opt/homebrew/bin/python3.11
```

## Production Still Needed

Before a public launch, add hosted Postgres, a real worker queue, HTTPS,
managed secrets, billing, email verification/password reset, observability,
backups, and stricter abuse controls.
