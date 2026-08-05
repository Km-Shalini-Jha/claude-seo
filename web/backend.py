#!/usr/bin/env python3
"""FastAPI backend for the Claude SEO SaaS console."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import subprocess
import threading
import time
import uuid
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

from fastapi import Depends, Header, HTTPException, Request
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, HttpUrl
from starlette.middleware.cors import CORSMiddleware

ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CLAUDE_SEO_WEB_DATA", WEB_ROOT / "data")).expanduser()
DB_PATH = DATA_DIR / "console.sqlite3"
DATABASE_URL = os.environ.get("DATABASE_URL") or os.environ.get("CLAUDE_SEO_DATABASE_URL")
DATABASE_BACKEND = "postgres" if DATABASE_URL and DATABASE_URL.startswith(("postgres://", "postgresql://")) else "sqlite"
DEFAULT_LAUNCHER = ROOT / "bin" / "claude-seo"
LAUNCHER = Path(os.environ.get("CLAUDE_SEO_LAUNCHER", DEFAULT_LAUNCHER)).expanduser()
PYTHON_OVERRIDE = os.environ.get("CLAUDE_SEO_PYTHON")
MAX_HISTORY = 80
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14
RATE_LIMIT_WINDOW = 60 * 60
RATE_LIMIT_MAX_JOBS = int(os.environ.get("CLAUDE_SEO_WEB_RATE_LIMIT", "30"))
PLAN_LIMITS = {
    "free": {"hourly_jobs": 10, "projects": 3, "sites": 10},
    "pro": {"hourly_jobs": 60, "projects": 25, "sites": 100},
    "agency": {"hourly_jobs": 240, "projects": 250, "sites": 1000},
}
WORKER_EVENT = threading.Event()
WORKER_STARTED = False
AUTO_WORKER_DEFAULT = "0" if os.environ.get("VERCEL") else "1"
AUTO_WORKER = os.environ.get("CLAUDE_SEO_WEB_AUTO_WORKER", AUTO_WORKER_DEFAULT) != "0"
WORKER_POLL_SECONDS = float(os.environ.get("CLAUDE_SEO_WORKER_POLL_SECONDS", "0.25"))
WORKER_LOG = os.environ.get("CLAUDE_SEO_WORKER_LOG")
DEV_EMAIL_TOKENS = os.environ.get("CLAUDE_SEO_WEB_DEV_EMAIL_TOKENS", "0") == "1"
PUBLIC_SIGNUP_ENABLED = os.environ.get("CLAUDE_SEO_PUBLIC_SIGNUP", "1") == "1"
PUBLIC_BASE_URL = os.environ.get("CLAUDE_SEO_PUBLIC_URL", "http://127.0.0.1:8001")
SMTP_HOST = os.environ.get("CLAUDE_SEO_SMTP_HOST")
SMTP_PORT = int(os.environ.get("CLAUDE_SEO_SMTP_PORT", "587"))
SMTP_USER = os.environ.get("CLAUDE_SEO_SMTP_USER")
SMTP_PASSWORD = os.environ.get("CLAUDE_SEO_SMTP_PASSWORD")
MAIL_FROM = os.environ.get("CLAUDE_SEO_MAIL_FROM", "RankForge Command <noreply@example.com>")
STRIPE_SECRET_KEY = os.environ.get("CLAUDE_SEO_STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("CLAUDE_SEO_STRIPE_WEBHOOK_SECRET")
STRIPE_PRICE_IDS = {
    "pro": os.environ.get("CLAUDE_SEO_STRIPE_PRICE_PRO"),
    "agency": os.environ.get("CLAUDE_SEO_STRIPE_PRICE_AGENCY"),
}
STRIPE_SUCCESS_URL = os.environ.get("CLAUDE_SEO_BILLING_SUCCESS_URL")
STRIPE_CANCEL_URL = os.environ.get("CLAUDE_SEO_BILLING_CANCEL_URL")
STRIPE_API_BASE = os.environ.get("CLAUDE_SEO_STRIPE_API_BASE", "https://api.stripe.com")

app = FastAPI(title="RankForge Command API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "CLAUDE_SEO_WEB_ORIGINS",
        "http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:8001,http://localhost:8001",
    ).split(","),
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    if os.environ.get("CLAUDE_SEO_WEB_HSTS") == "1":
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


class AuthRequest(BaseModel):
    email: str
    password: str
    name: str | None = None


class VerifyEmailRequest(BaseModel):
    token: str


class PasswordResetRequest(BaseModel):
    email: str


class PasswordResetConfirm(BaseModel):
    token: str
    password: str


class ProfileUpdateRequest(BaseModel):
    name: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class TeamInviteRequest(BaseModel):
    email: str
    role: str = "member"


class TeamInviteAcceptRequest(BaseModel):
    token: str


class BillingPlanRequest(BaseModel):
    plan: str


class AdminUserPlanRequest(BaseModel):
    plan: str


class ProjectRequest(BaseModel):
    name: str


class ProjectUpdateRequest(BaseModel):
    name: str


class SiteRequest(BaseModel):
    project_id: str
    url: HttpUrl
    name: str | None = None


class SiteUpdateRequest(BaseModel):
    project_id: str | None = None
    url: HttpUrl | None = None
    name: str | None = None


class JobRequest(BaseModel):
    url: HttpUrl
    module: str
    project_id: str | None = None
    site_id: str | None = None


class ScheduleRequest(BaseModel):
    url: HttpUrl
    module: str
    frequency: Literal["hourly", "daily", "weekly"]
    project_id: str | None = None
    site_id: str | None = None


class JobRecord(BaseModel):
    id: str
    user_id: str | None = None
    project_id: str | None = None
    site_id: str | None = None
    module: str
    label: str
    url: str
    status: Literal["queued", "running", "complete", "error", "cancelled"]
    created_at: float
    updated_at: float
    command: str | None = None
    output: dict[str, Any] | None = None
    error: str | None = None
    share_token: str | None = None


class AuditRequest(BaseModel):
    url: HttpUrl


class AuditResult(BaseModel):
    success: bool
    command: str
    output: Any


MODULES: dict[str, dict[str, Any]] = {
    "overview": {
        "label": "HTML and On-page Signals",
        "category": "Audit",
        "description": "Titles, headings, canonicals, meta tags, links, images, and schema extraction.",
        "script": "parse_html.py",
        "args": lambda url: ["--url", url, "--json"],
        "timeout": 45,
    },
    "full-audit": {
        "label": "Full SEO Audit",
        "category": "Audit",
        "description": "Runs the core customer audit sequence: on-page, sitemap, preload, UX, content, and local signals.",
        "compound": "full_audit",
        "children": ["overview", "sitemap", "preload", "ux", "content", "local"],
        "timeout": 420,
    },
    "sitemap": {
        "label": "Sitemap Discovery",
        "category": "Technical",
        "description": "Discovers declared and common XML sitemap locations.",
        "script": "sitemap_discovery.py",
        "args": lambda url: [url, "--json"],
        "timeout": 75,
    },
    "performance": {
        "label": "PageSpeed and Core Web Vitals",
        "category": "Performance",
        "description": "Runs PageSpeed Insights checks for mobile and desktop when runtime credentials allow it.",
        "script": "pagespeed_check.py",
        "args": lambda url: [url, "--strategy", "both", "--json"],
        "timeout": 180,
    },
    "preload": {
        "label": "Preload and Resource Hints",
        "category": "Performance",
        "description": "Checks preload usage, blocking resources, and common performance hint issues.",
        "script": "preload_check.py",
        "args": lambda url: [url, "--json"],
        "timeout": 75,
    },
    "ux": {
        "label": "UX Heuristic Review",
        "category": "Experience",
        "description": "Reviews page experience, readability, interaction, and conversion clarity.",
        "script": "agent_ux_check.py",
        "args": lambda url: [url, "--json"],
        "timeout": 120,
    },
    "visual": {
        "label": "Visual Rendering Analysis",
        "category": "Experience",
        "description": "Uses a browser render to inspect visible layout and viewport issues.",
        "script": "analyze_visual.py",
        "args": lambda url: [url, "--json"],
        "timeout": 120,
    },
    "content": {
        "label": "Content Quality",
        "category": "Content",
        "description": "Evaluates specificity, density, repetition, and thin-content risk.",
        "compound": "content_quality",
        "timeout": 90,
    },
    "local": {
        "label": "Local SEO Signal Lint",
        "category": "Local",
        "description": "Flags deprecated GBP patterns and local-business markup issues detectable from page HTML.",
        "script": "gbp_deprecation_lint.py",
        "args": lambda url: [url, "--json"],
        "timeout": 75,
    },
    "schema-commerce": {
        "label": "Ecommerce Schema Validation",
        "category": "Schema",
        "description": "Validates product and merchant structured data from the page source.",
        "script": "schema_ecommerce_validate.py",
        "args": lambda url: [url, "--json"],
        "timeout": 75,
    },
    "lcp": {
        "label": "LCP Subparts",
        "category": "Performance",
        "description": "Breaks down LCP field data when a Google API key is configured.",
        "script": "lcp_subparts.py",
        "args": lambda url: [url, "--json"],
        "timeout": 75,
    },
}

JOBS: dict[str, JobRecord] = {}
JOBS_LOCK = threading.Lock()
RATE_LIMITS: dict[str, list[float]] = {}


def now() -> float:
    return time.time()


class AppDatabase:
    def __init__(self, raw: Any, backend: str):
        self.raw = raw
        self.backend = backend

    def __enter__(self) -> "AppDatabase":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.backend == "sqlite":
            if exc_type:
                self.raw.rollback()
            else:
                self.raw.commit()
        self.raw.close()

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        if self.backend == "postgres":
            sql = sql.replace("?", "%s")
        return self.raw.execute(sql, params)

    def commit(self) -> None:
        self.raw.commit()


def postgres_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured")
    if "sslmode=" in DATABASE_URL:
        return DATABASE_URL
    separator = "&" if "?" in DATABASE_URL else "?"
    return f"{DATABASE_URL}{separator}sslmode=require"


def db() -> AppDatabase:
    if DATABASE_BACKEND == "postgres":
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("PostgreSQL support requires psycopg. Run `pip install -r web/requirements.txt`.") from exc
        raw = psycopg.connect(postgres_url(), row_factory=dict_row, autocommit=True, prepare_threshold=None)
        conn = AppDatabase(raw, "postgres")
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        raw = sqlite3.connect(DB_PATH)
        raw.row_factory = sqlite3.Row
        conn = AppDatabase(raw, "sqlite")
    ensure_schema(conn)
    return conn


def db_integrity_error(exc: Exception) -> bool:
    return isinstance(exc, sqlite3.IntegrityError) or exc.__class__.__name__ in {"IntegrityError", "UniqueViolation"}


def table_columns(conn: AppDatabase, table: str) -> set[str]:
    if conn.backend == "postgres":
        rows = conn.execute(
            "SELECT column_name AS name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = ?",
            (table,),
        ).fetchall()
    else:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


def ensure_schema(conn: AppDatabase) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            name TEXT,
            password_hash TEXT NOT NULL,
            plan TEXT NOT NULL DEFAULT 'free',
            email_verified INTEGER NOT NULL DEFAULT 0,
            role TEXT NOT NULL DEFAULT 'user',
            stripe_customer_id TEXT,
            stripe_subscription_id TEXT,
            subscription_status TEXT NOT NULL DEFAULT 'dev',
            subscription_current_period_end REAL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            name TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sites (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            name TEXT,
            url TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(project_id) REFERENCES projects(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_verification_tokens (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            used_at REAL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS password_reset_tokens (
            token TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            used_at REAL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS email_outbox (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            recipient TEXT NOT NULL,
            subject TEXT NOT NULL,
            body TEXT NOT NULL,
            purpose TEXT NOT NULL,
            token TEXT,
            provider TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            sent_at REAL,
            error TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            action TEXT NOT NULL,
            target_type TEXT,
            target_id TEXT,
            metadata_json TEXT,
            created_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS team_invitations (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            email TEXT NOT NULL,
            role TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            accepted_at REAL,
            FOREIGN KEY(owner_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS team_members (
            id TEXT PRIMARY KEY,
            owner_id TEXT NOT NULL,
            user_id TEXT,
            email TEXT NOT NULL,
            role TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            FOREIGN KEY(owner_id) REFERENCES users(id),
            FOREIGN KEY(user_id) REFERENCES users(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stripe_events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            received_at REAL NOT NULL,
            processed_at REAL,
            payload_json TEXT NOT NULL,
            error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            user_id TEXT,
            project_id TEXT,
            site_id TEXT,
            module TEXT NOT NULL,
            label TEXT NOT NULL,
            url TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            command TEXT,
            output_json TEXT,
            error TEXT,
            share_token TEXT UNIQUE,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(project_id) REFERENCES projects(id),
            FOREIGN KEY(site_id) REFERENCES sites(id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduled_audits (
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            project_id TEXT,
            site_id TEXT,
            module TEXT NOT NULL,
            label TEXT NOT NULL,
            url TEXT NOT NULL,
            frequency TEXT NOT NULL,
            status TEXT NOT NULL,
            next_run_at REAL NOT NULL,
            last_run_at REAL,
            last_job_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(project_id) REFERENCES projects(id),
            FOREIGN KEY(site_id) REFERENCES sites(id),
            FOREIGN KEY(last_job_id) REFERENCES jobs(id)
        )
        """
    )
    existing = table_columns(conn, "jobs")
    migrations = {
        "user_id": "ALTER TABLE jobs ADD COLUMN user_id TEXT",
        "project_id": "ALTER TABLE jobs ADD COLUMN project_id TEXT",
        "site_id": "ALTER TABLE jobs ADD COLUMN site_id TEXT",
        "share_token": "ALTER TABLE jobs ADD COLUMN share_token TEXT",
    }
    for column, statement in migrations.items():
        if column not in existing:
            conn.execute(statement)
    user_columns = table_columns(conn, "users")
    if "plan" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
    if "email_verified" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
    if "role" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
    if "stripe_customer_id" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN stripe_customer_id TEXT")
    if "stripe_subscription_id" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN stripe_subscription_id TEXT")
    if "subscription_status" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN subscription_status TEXT NOT NULL DEFAULT 'dev'")
    if "subscription_current_period_end" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN subscription_current_period_end REAL")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user_updated ON jobs(user_id, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sites_user_project ON sites(user_id, project_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_share_token ON jobs(share_token)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_user_created ON audit_logs(user_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stripe_events_received ON stripe_events(received_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_team_invitations_owner ON team_invitations(owner_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_team_members_owner ON team_members(owner_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_scheduled_audits_due ON scheduled_audits(status, next_run_at)")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def hash_password(password: str) -> str:
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 210_000)
    return "pbkdf2_sha256$210000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_b64, digest_b64 = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    created = now()
    with db() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, created, created + SESSION_TTL_SECONDS),
        )
    return token


def row_keys(row: Any) -> set[str]:
    return set(row.keys())


def user_public(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "plan": row["plan"] if "plan" in row_keys(row) else "free",
        "email_verified": bool(row["email_verified"]) if "email_verified" in row_keys(row) else False,
        "role": row["role"] if "role" in row_keys(row) else "user",
        "subscription_status": row["subscription_status"] if "subscription_status" in row_keys(row) else "dev",
        "subscription_current_period_end": row["subscription_current_period_end"] if "subscription_current_period_end" in row_keys(row) else None,
        "created_at": row["created_at"],
    }


def is_first_user() -> bool:
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    return count == 0


def log_event(user_id: str | None, action: str, target_type: str | None = None, target_id: str | None = None, metadata: dict[str, Any] | None = None) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO audit_logs (id, user_id, action, target_type, target_id, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                user_id,
                action,
                target_type,
                target_id,
                json.dumps(metadata or {}, sort_keys=True),
                now(),
            ),
        )


def backup_payload() -> dict[str, Any]:
    tables = [
        "users", "projects", "sites", "jobs", "sessions",
        "email_verification_tokens", "password_reset_tokens", "email_outbox",
        "audit_logs", "stripe_events", "team_invitations", "team_members",
        "scheduled_audits",
    ]
    data = {}
    with db() as conn:
        for table in tables:
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            data[table] = [dict(row) for row in rows]
    return {"created_at": now(), "schema": 1, "tables": data}


def create_email_verification_token(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    ts = now()
    with db() as conn:
        conn.execute(
            "INSERT INTO email_verification_tokens (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, ts, ts + 60 * 60 * 24),
        )
    return token


def create_password_reset_token(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    ts = now()
    with db() as conn:
        conn.execute(
            "INSERT INTO password_reset_tokens (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (token, user_id, ts, ts + 60 * 30),
        )
    return token


def queue_email(user_id: str | None, recipient: str, subject: str, body: str, purpose: str, token: str | None = None) -> dict[str, Any]:
    provider = "smtp" if SMTP_HOST else "dev-outbox"
    status = "queued"
    error = None
    sent_at = None
    if SMTP_HOST:
        try:
            message = EmailMessage()
            message["From"] = MAIL_FROM
            message["To"] = recipient
            message["Subject"] = subject
            message.set_content(body)
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as smtp:
                smtp.starttls()
                if SMTP_USER and SMTP_PASSWORD:
                    smtp.login(SMTP_USER, SMTP_PASSWORD)
                smtp.send_message(message)
            status = "sent"
            sent_at = now()
        except Exception as exc:
            status = "error"
            error = str(exc)
    row = {
        "id": uuid.uuid4().hex,
        "user_id": user_id,
        "recipient": recipient,
        "subject": subject,
        "body": body,
        "purpose": purpose,
        "token": token,
        "provider": provider,
        "status": status,
        "created_at": now(),
        "sent_at": sent_at,
        "error": error,
    }
    with db() as conn:
        conn.execute(
            """
            INSERT INTO email_outbox (id, user_id, recipient, subject, body, purpose, token, provider, status, created_at, sent_at, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"], row["user_id"], row["recipient"], row["subject"], row["body"],
                row["purpose"], row["token"], row["provider"], row["status"],
                row["created_at"], row["sent_at"], row["error"],
            ),
        )
    return row


def send_verification_email(user_id: str, email: str, token: str) -> None:
    url = f"{PUBLIC_BASE_URL}/verify-email?token={token}"
    queue_email(
        user_id,
        email,
        "Verify your Claude SEO account",
        f"Verify your Claude SEO account:\n\n{url}\n\nIf you did not create this account, ignore this email.",
        "email_verification",
        token,
    )


def send_password_reset_email(user_id: str, email: str, token: str) -> None:
    url = f"{PUBLIC_BASE_URL}/reset-password?token={token}"
    queue_email(
        user_id,
        email,
        "Reset your Claude SEO password",
        f"Reset your Claude SEO password:\n\n{url}\n\nThis link expires in 30 minutes.",
        "password_reset",
        token,
    )


def send_team_invite_email(owner_id: str, email: str, token: str) -> None:
    url = f"{PUBLIC_BASE_URL}/?invite_token={token}"
    queue_email(
        owner_id,
        email,
        "You were invited to Claude SEO",
        f"You were invited to collaborate in Claude SEO:\n\n{url}\n\nCreate or log into an account with this email, then accept the invite.",
        "team_invite",
        token,
    )


def stripe_configured_for_plan(plan: str) -> bool:
    return bool(STRIPE_SECRET_KEY and STRIPE_PRICE_IDS.get(plan))


def stripe_checkout_url(path: str) -> str:
    return f"{STRIPE_API_BASE.rstrip('/')}{path}"


def stripe_success_url(plan: str) -> str:
    if STRIPE_SUCCESS_URL:
        return STRIPE_SUCCESS_URL
    return f"{PUBLIC_BASE_URL}/?billing=success&plan={urllib.parse.quote(plan)}&session_id={{CHECKOUT_SESSION_ID}}"


def stripe_cancel_url(plan: str) -> str:
    if STRIPE_CANCEL_URL:
        return STRIPE_CANCEL_URL
    return f"{PUBLIC_BASE_URL}/?billing=cancelled&plan={urllib.parse.quote(plan)}"


def stripe_api_post(path: str, fields: dict[str, str]) -> dict[str, Any]:
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("Stripe secret key is not configured")
    encoded = urllib.parse.urlencode(fields).encode("utf-8")
    auth = base64.b64encode(f"{STRIPE_SECRET_KEY}:".encode("utf-8")).decode("ascii")
    req = urllib.request.Request(
        stripe_checkout_url(path),
        data=encoded,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8")
        raise RuntimeError(detail or f"Stripe API returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Stripe API request failed: {exc.reason}") from exc


def create_stripe_checkout_session(plan: str, user: dict[str, Any]) -> dict[str, Any]:
    price_id = STRIPE_PRICE_IDS.get(plan)
    if not price_id:
        raise RuntimeError(f"Stripe price ID is not configured for {plan}")
    fields = {
        "mode": "subscription",
        "success_url": stripe_success_url(plan),
        "cancel_url": stripe_cancel_url(plan),
        "line_items[0][price]": price_id,
        "line_items[0][quantity]": "1",
        "client_reference_id": user["id"],
        "metadata[user_id]": user["id"],
        "metadata[plan]": plan,
        "subscription_data[metadata][user_id]": user["id"],
        "subscription_data[metadata][plan]": plan,
        "allow_promotion_codes": "true",
    }
    with db() as conn:
        row = conn.execute("SELECT stripe_customer_id FROM users WHERE id = ?", (user["id"],)).fetchone()
    if row and row["stripe_customer_id"]:
        fields["customer"] = row["stripe_customer_id"]
    else:
        fields["customer_email"] = user["email"]
    return stripe_api_post("/v1/checkout/sessions", fields)


def parse_stripe_signature(header: str) -> tuple[int, list[str]]:
    timestamp = None
    signatures: list[str] = []
    for item in header.split(","):
        key, _, value = item.partition("=")
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError:
                timestamp = None
        elif key == "v1":
            signatures.append(value)
    if timestamp is None or not signatures:
        raise HTTPException(status_code=400, detail="Invalid Stripe signature header")
    return timestamp, signatures


def verify_stripe_webhook_signature(payload: bytes, signature_header: str | None) -> None:
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Stripe webhook secret is not configured")
    if not signature_header:
        raise HTTPException(status_code=400, detail="Missing Stripe signature")
    timestamp, signatures = parse_stripe_signature(signature_header)
    if abs(int(now()) - timestamp) > 300:
        raise HTTPException(status_code=400, detail="Expired Stripe signature")
    signed_payload = str(timestamp).encode("utf-8") + b"." + payload
    expected = hmac.new(STRIPE_WEBHOOK_SECRET.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in signatures):
        raise HTTPException(status_code=400, detail="Invalid Stripe signature")


def event_already_processed(event_id: str) -> bool:
    with db() as conn:
        try:
            conn.execute(
                "INSERT INTO stripe_events (event_id, event_type, received_at, payload_json) VALUES (?, ?, ?, ?)",
                (event_id, "pending", now(), "{}"),
            )
            return False
        except Exception as exc:
            if db_integrity_error(exc):
                return True
            raise


def store_stripe_event(event: dict[str, Any], error: str | None = None) -> None:
    with db() as conn:
        conn.execute(
            """
            UPDATE stripe_events
            SET event_type = ?, processed_at = ?, payload_json = ?, error = ?
            WHERE event_id = ?
            """,
            (
                str(event.get("type") or "unknown"),
                now() if error is None else None,
                json.dumps(event, sort_keys=True),
                error,
                str(event.get("id")),
            ),
        )


def plan_from_metadata(metadata: Any) -> str | None:
    if not isinstance(metadata, dict):
        return None
    plan = metadata.get("plan")
    return str(plan) if plan in PLAN_LIMITS else None


def update_user_subscription(
    user_id: str,
    plan: str,
    status: str,
    customer_id: str | None = None,
    subscription_id: str | None = None,
    current_period_end: float | None = None,
) -> dict[str, Any] | None:
    if plan not in PLAN_LIMITS:
        raise ValueError("Unknown plan")
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        conn.execute(
            """
            UPDATE users
            SET plan = ?,
                subscription_status = ?,
                stripe_customer_id = COALESCE(?, stripe_customer_id),
                stripe_subscription_id = COALESCE(?, stripe_subscription_id),
                subscription_current_period_end = COALESCE(?, subscription_current_period_end)
            WHERE id = ?
            """,
            (plan, status, customer_id, subscription_id, current_period_end, user_id),
        )
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    log_event(user_id, "billing.subscription_updated", "user", user_id, {"plan": plan, "status": status})
    return user_public(row) if row else None


def downgrade_user_subscription(user_id: str, status: str = "cancelled") -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE users SET plan = 'free', subscription_status = ?, subscription_current_period_end = NULL WHERE id = ?",
            (status, user_id),
        )
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    log_event(user_id, "billing.subscription_downgraded", "user", user_id, {"status": status})
    return user_public(row) if row else None


def apply_stripe_event(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "")
    obj = ((event.get("data") or {}).get("object") or {}) if isinstance(event.get("data"), dict) else {}
    if not isinstance(obj, dict):
        return {"handled": False, "reason": "missing data.object"}
    metadata = obj.get("metadata") if isinstance(obj.get("metadata"), dict) else {}
    user_id = metadata.get("user_id") or obj.get("client_reference_id")
    plan = plan_from_metadata(metadata)
    if event_type == "checkout.session.completed":
        if not user_id or not plan:
            return {"handled": False, "reason": "missing checkout metadata"}
        user = update_user_subscription(
            str(user_id),
            plan,
            "active",
            str(obj.get("customer")) if obj.get("customer") else None,
            str(obj.get("subscription")) if obj.get("subscription") else None,
            float(obj["current_period_end"]) if obj.get("current_period_end") else None,
        )
        return {"handled": bool(user), "user": user}
    if event_type in {"customer.subscription.created", "customer.subscription.updated"}:
        if not user_id or not plan:
            return {"handled": False, "reason": "missing subscription metadata"}
        status = str(obj.get("status") or "active")
        if status in {"active", "trialing"}:
            user = update_user_subscription(
                str(user_id),
                plan,
                status,
                str(obj.get("customer")) if obj.get("customer") else None,
                str(obj.get("id")) if obj.get("id") else None,
                float(obj["current_period_end"]) if obj.get("current_period_end") else None,
            )
            return {"handled": bool(user), "user": user}
        user = downgrade_user_subscription(str(user_id), status)
        return {"handled": bool(user), "user": user}
    if event_type == "customer.subscription.deleted":
        if not user_id:
            return {"handled": False, "reason": "missing subscription metadata"}
        user = downgrade_user_subscription(str(user_id), "cancelled")
        return {"handled": bool(user), "user": user}
    if event_type == "invoice.payment_failed":
        customer_id = obj.get("customer")
        if not customer_id:
            return {"handled": False, "reason": "missing invoice customer"}
        with db() as conn:
            row = conn.execute("SELECT * FROM users WHERE stripe_customer_id = ?", (str(customer_id),)).fetchone()
            if not row:
                return {"handled": False, "reason": "customer not found"}
            conn.execute("UPDATE users SET subscription_status = 'past_due' WHERE id = ?", (row["id"],))
            row = conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()
        log_event(row["id"], "billing.payment_failed", "user", row["id"], {"customer_id": str(customer_id)})
        return {"handled": True, "user": user_public(row)}
    return {"handled": False, "reason": "ignored event type"}


def current_user(authorization: str | None = Header(default=None)) -> dict[str, Any]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Authentication required")
    token = authorization.removeprefix("Bearer ").strip()
    with db() as conn:
        row = conn.execute(
            """
            SELECT users.* FROM sessions
            JOIN users ON users.id = sessions.user_id
            WHERE sessions.token = ? AND sessions.expires_at > ?
            """,
            (token, now()),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user_public(row)


def require_admin(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def check_rate_limit(user: dict[str, Any]) -> None:
    plan = user.get("plan", "free")
    hourly_limit = PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])["hourly_jobs"]
    hourly_limit = min(hourly_limit, RATE_LIMIT_MAX_JOBS) if RATE_LIMIT_MAX_JOBS else hourly_limit
    cutoff = now() - RATE_LIMIT_WINDOW
    user_id = user["id"]
    runs = [ts for ts in RATE_LIMITS.get(user_id, []) if ts >= cutoff]
    if len(runs) >= hourly_limit:
        raise HTTPException(status_code=429, detail=f"Audit rate limit reached for {plan} plan")
    runs.append(now())
    RATE_LIMITS[user_id] = runs


def enforce_project_limit(user: dict[str, Any]) -> None:
    limit = PLAN_LIMITS.get(user.get("plan", "free"), PLAN_LIMITS["free"])["projects"]
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM projects WHERE user_id = ?", (user["id"],)).fetchone()["n"]
    if count >= limit:
        raise HTTPException(status_code=402, detail="Project limit reached for current plan")


def enforce_site_limit(user: dict[str, Any]) -> None:
    limit = PLAN_LIMITS.get(user.get("plan", "free"), PLAN_LIMITS["free"])["sites"]
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM sites WHERE user_id = ?", (user["id"],)).fetchone()["n"]
    if count >= limit:
        raise HTTPException(status_code=402, detail="Site limit reached for current plan")


def row_to_job(row: Any) -> JobRecord:
    output = json.loads(row["output_json"]) if row["output_json"] else None
    keys = row_keys(row)
    return JobRecord(
        id=row["id"],
        user_id=row["user_id"] if "user_id" in keys else None,
        project_id=row["project_id"] if "project_id" in keys else None,
        site_id=row["site_id"] if "site_id" in keys else None,
        module=row["module"],
        label=row["label"],
        url=row["url"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        command=row["command"],
        output=output,
        error=row["error"],
        share_token=row["share_token"] if "share_token" in keys else None,
    )


def persist_job(record: JobRecord) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (id, user_id, project_id, site_id, module, label, url, status, created_at, updated_at, command, output_json, error, share_token)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                user_id=excluded.user_id,
                project_id=excluded.project_id,
                site_id=excluded.site_id,
                module=excluded.module,
                label=excluded.label,
                url=excluded.url,
                status=excluded.status,
                updated_at=excluded.updated_at,
                command=excluded.command,
                output_json=excluded.output_json,
                error=excluded.error,
                share_token=excluded.share_token
            """,
            (
                record.id,
                record.user_id,
                record.project_id,
                record.site_id,
                record.module,
                record.label,
                record.url,
                record.status,
                record.created_at,
                record.updated_at,
                record.command,
                json.dumps(record.output, sort_keys=True) if record.output is not None else None,
                record.error,
                record.share_token,
            ),
        )


def load_job(job_id: str, user_id: str | None = None) -> JobRecord | None:
    if user_id:
        query = "SELECT * FROM jobs WHERE id = ? AND user_id = ?"
        params = (job_id, user_id)
    else:
        query = "SELECT * FROM jobs WHERE id = ?"
        params = (job_id,)
    with db() as conn:
        row = conn.execute(query, params).fetchone()
    return row_to_job(row) if row else None


def runtime_env() -> dict[str, str]:
    env = os.environ.copy()
    if PYTHON_OVERRIDE:
        env["CLAUDE_SEO_PYTHON"] = PYTHON_OVERRIDE
    return env


def run_subprocess(command: list[str], timeout: int = 300) -> dict[str, str]:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=timeout,
            env=runtime_env(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Audit timed out after {timeout} seconds") from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Unknown error")
    return {"stdout": result.stdout.strip(), "stderr": result.stderr.strip()}


def fake_cli_script(script: str, args: list[str]) -> dict[str, str]:
    url = next((arg for arg in args if isinstance(arg, str) and arg.startswith("http")), "https://example.com/")
    payloads: dict[str, Any] = {
        "parse_html.py": {
            "title": "Example Customer Page",
            "meta_description": "A concise customer page description.",
            "canonical": url,
            "h1": ["Example Customer Page"],
            "h2": ["Services"],
            "images": [{"src": f"{url.rstrip('/')}/hero.jpg", "alt": ""}],
            "links": {"internal": [], "external": []},
            "schema": [],
            "word_count": 420,
        },
        "sitemap_discovery.py": {"target": url, "found": [{"url": f"{url.rstrip('/')}/sitemap.xml", "kind": "sitemap"}], "warnings": [], "error": None},
        "preload_check.py": {"issues": [], "warnings": ["No hero image preload detected"], "recommendations": ["Preload the LCP image if stable."]},
        "agent_ux_check.py": {"score": 84, "issues": [], "recommendations": ["Keep the primary CTA visible above the fold."]},
        "fetch_page.py": "<html><title>Example Customer Page</title><body><h1>Example Customer Page</h1><p>Specific useful content with numbers.</p></body></html>",
        "content_quality.py": {"score": 78, "tokens": 420, "issues": [], "recommendations": ["Add first-hand proof points and named examples."]},
        "gbp_deprecation_lint.py": {"issues": [], "warnings": [], "recommendations": []},
        "schema_ecommerce_validate.py": {"issues": ["Product schema not found"], "warnings": [], "recommendations": ["Add Product JSON-LD on product pages."]},
        "lcp_subparts.py": {"error": "Google API key required for LCP subparts."},
        "pagespeed_check.py": {"error": "PageSpeed fake run skipped external API."},
        "analyze_visual.py": {"score": 80, "issues": [], "recommendations": ["Check mobile hero spacing."]},
    }
    value = payloads.get(script, {"ok": True, "script": script})
    if isinstance(value, str):
        return {"stdout": value, "stderr": ""}
    return {"stdout": json.dumps(value, indent=2), "stderr": ""}


def run_cli_script(script: str, args: list[str], timeout: int = 300) -> dict[str, Any]:
    if not LAUNCHER.exists():
        raise RuntimeError(f"Claude SEO launcher not found: {LAUNCHER}")
    if os.environ.get("CLAUDE_SEO_WEB_FAKE_RUNS") == "1":
        return fake_cli_script(script, args)
    return run_subprocess(["bash", str(LAUNCHER), "run", script, *args], timeout=timeout)


def run_module(module: str, url: str) -> tuple[str, dict[str, Any]]:
    config = MODULES[module]
    timeout = int(config.get("timeout", 300))
    if config.get("compound") == "full_audit":
        results: dict[str, Any] = {"url": url, "modules": {}, "errors": {}}
        commands = []
        for child in config["children"]:
            try:
                command, output = run_module(child, url)
                commands.append(command)
                results["modules"][child] = {
                    "label": MODULES[child]["label"],
                    "command": command,
                    "output": output,
                    "findings": build_findings(child, output),
                }
            except Exception as exc:
                results["errors"][child] = str(exc)
                results["modules"][child] = {
                    "label": MODULES[child]["label"],
                    "command": None,
                    "output": None,
                    "findings": build_findings(child, None, str(exc)),
                }
        return ", ".join(commands) or "full-audit", {"stdout": json.dumps(results, indent=2), "stderr": ""}
    if config.get("compound") == "content_quality":
        fetch_result = run_cli_script("fetch_page.py", [url], timeout=timeout)
        html = fetch_result["stdout"]
        with NamedTemporaryFile("w", encoding="utf-8", suffix=".html", delete=False) as handle:
            handle.write(html)
            temp_file = Path(handle.name)
        try:
            return "content_quality.py", run_cli_script("content_quality.py", [str(temp_file), "--json"], timeout=timeout)
        finally:
            temp_file.unlink(missing_ok=True)
    script = str(config["script"])
    args = config["args"](url)
    return script, run_cli_script(script, args, timeout=timeout)


def parse_stdout(output: dict[str, Any] | None) -> Any:
    if not output:
        return None
    stdout = output.get("stdout", "")
    if not isinstance(stdout, str) or not stdout:
        return None
    try:
        return json.loads(stdout)
    except ValueError:
        return stdout


def build_findings(module: str, output: dict[str, Any] | None, error: str | None = None) -> list[dict[str, str]]:
    if error:
        return [{"severity": "critical", "title": "Run failed", "detail": error}]
    data = parse_stdout(output)
    findings: list[dict[str, str]] = []
    if module == "overview" and isinstance(data, dict):
        if not data.get("title"):
            findings.append({"severity": "high", "title": "Missing title", "detail": "The page has no title tag."})
        if not data.get("meta_description"):
            findings.append({"severity": "medium", "title": "Missing meta description", "detail": "No meta description was detected."})
        h1_count = len(data.get("h1") or [])
        if h1_count != 1:
            findings.append({"severity": "medium", "title": "H1 count needs review", "detail": f"Detected {h1_count} H1 tags."})
        missing_alt = sum(1 for image in (data.get("images") or []) if not image.get("alt"))
        if missing_alt:
            findings.append({"severity": "medium", "title": "Image alt text gaps", "detail": f"{missing_alt} images are missing alt text."})
        if not data.get("schema"):
            findings.append({"severity": "low", "title": "No JSON-LD schema detected", "detail": "Structured data was not found in the page source."})
    elif module == "sitemap" and isinstance(data, dict):
        if data.get("error"):
            findings.append({"severity": "high", "title": "Sitemap discovery error", "detail": str(data["error"])})
        elif not data.get("found"):
            findings.append({"severity": "medium", "title": "No sitemap found", "detail": "No valid sitemap was discovered from robots.txt or common locations."})
    elif module == "full-audit" and isinstance(data, dict):
        modules = data.get("modules") or {}
        error_count = len(data.get("errors") or {})
        if error_count:
            findings.append({"severity": "high", "title": "Some audit modules failed", "detail": f"{error_count} modules returned errors. Review raw output for details."})
        for module_id, result in modules.items():
            for item in (result.get("findings") or [])[:4]:
                findings.append({
                    "severity": item.get("severity", "info"),
                    "title": f"{result.get('label', module_id)}: {item.get('title', 'Finding')}",
                    "detail": item.get("detail", ""),
                })
    elif isinstance(data, dict):
        if data.get("error"):
            findings.append({"severity": "high", "title": "Module returned an error", "detail": str(data["error"])})
        for key in ("issues", "warnings", "recommendations"):
            value = data.get(key)
            if isinstance(value, list):
                for item in value[:8]:
                    findings.append({"severity": "medium", "title": key.title(), "detail": str(item)})
    if not findings:
        findings.append({"severity": "info", "title": "No immediate issues summarized", "detail": "Open the raw JSON tab for complete module output."})
    return findings


def public_job(record: JobRecord) -> dict[str, Any]:
    payload = record.model_dump()
    payload["findings"] = build_findings(record.module, record.output, record.error)
    payload["share_url"] = f"/share/{record.share_token}" if record.share_token else None
    return payload


def update_job(job_id: str, **changes: Any) -> JobRecord:
    with JOBS_LOCK:
        record = JOBS.get(job_id) or load_job(job_id)
        if record is None:
            raise RuntimeError("Job not found")
        data = record.model_dump()
        data.update(changes)
        data["updated_at"] = now()
        updated = JobRecord(**data)
        JOBS[job_id] = updated
        persist_job(updated)
    return updated


def frequency_seconds(frequency: str) -> int:
    return {"hourly": 60 * 60, "daily": 60 * 60 * 24, "weekly": 60 * 60 * 24 * 7}.get(frequency, 60 * 60 * 24)


def process_due_schedules(limit: int = 5) -> int:
    ts = now()
    created = 0
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM scheduled_audits
            WHERE status = 'active' AND next_run_at <= ?
            ORDER BY next_run_at ASC
            LIMIT ?
            """,
            (ts, limit),
        ).fetchall()
    for row in rows:
        schedule = dict(row)
        job_id = uuid.uuid4().hex
        next_run = ts + frequency_seconds(schedule["frequency"])
        with db() as conn:
            claimed = conn.execute(
                """
                UPDATE scheduled_audits
                SET last_run_at = ?, last_job_id = ?, next_run_at = ?, updated_at = ?
                WHERE id = ? AND status = 'active' AND next_run_at <= ?
                """,
                (ts, job_id, next_run, ts, schedule["id"], ts),
            ).rowcount
        if not claimed:
            continue
        record = JobRecord(
            id=job_id,
            user_id=schedule["user_id"],
            project_id=schedule["project_id"],
            site_id=schedule["site_id"],
            module=schedule["module"],
            label=schedule["label"],
            url=schedule["url"],
            status="queued",
            created_at=ts,
            updated_at=ts,
        )
        persist_job(record)
        WORKER_EVENT.set()
        created += 1
    return created


def execute_job(job_id: str) -> None:
    record = update_job(job_id, status="running")
    try:
        command, output = run_module(record.module, record.url)
        completed = update_job(
            job_id,
            status="complete",
            command=command,
            output=output,
            error=None,
            share_token=record.share_token or secrets.token_urlsafe(18),
        )
    except Exception as exc:
        completed = update_job(job_id, status="error", error=str(exc))
    persist_job(completed)


def next_queued_job() -> JobRecord | None:
    ts = now()
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        changed = conn.execute(
            "UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ? AND status = 'queued'",
            (ts, row["id"]),
        ).rowcount
        if not changed:
            return None
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
    return row_to_job(row) if row else None


def worker_loop() -> None:
    while True:
        due = process_due_schedules()
        if due:
            worker_log(f"scheduled {due} due audits")
        job = next_queued_job()
        if job:
            worker_log(f"claimed {job.id} {job.module}")
            try:
                command, output = run_module(job.module, job.url)
                completed = update_job(
                    job.id,
                    status="complete",
                    command=command,
                    output=output,
                    error=None,
                    share_token=job.share_token or secrets.token_urlsafe(18),
                )
            except Exception as exc:
                completed = update_job(job.id, status="error", error=str(exc))
                worker_log(f"failed {job.id} {exc}")
            persist_job(completed)
            worker_log(f"finished {job.id} {completed.status}")
            continue
        WORKER_EVENT.wait(WORKER_POLL_SECONDS)
        WORKER_EVENT.clear()


def worker_log(message: str) -> None:
    if not WORKER_LOG:
        return
    try:
        with open(WORKER_LOG, "a", encoding="utf-8") as handle:
            handle.write(f"{time.time():.3f} {message}\n")
    except OSError:
        pass


@app.on_event("startup")
def start_worker() -> None:
    global WORKER_STARTED
    if not AUTO_WORKER:
        return
    if WORKER_STARTED:
        return
    WORKER_STARTED = True
    threading.Thread(target=worker_loop, daemon=True).start()


def run_runtime_doctor() -> dict[str, Any]:
    if not LAUNCHER.exists():
        return {"ready": False, "browser_ready": False, "mode": "missing", "python_version": None, "reasons": [f"Claude SEO launcher not found: {LAUNCHER}"]}
    if os.environ.get("CLAUDE_SEO_WEB_FAKE_RUNS") == "1":
        return {"ready": True, "browser_ready": True, "mode": "fake", "python_version": "3.11", "reasons": []}
    result = subprocess.run(
        ["bash", str(LAUNCHER), "doctor", "--json"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        timeout=30,
        env=runtime_env(),
    )
    try:
        payload = json.loads(result.stdout or "{}")
        if isinstance(payload, dict):
            return payload
    except ValueError:
        pass
    return {"ready": False, "browser_ready": False, "mode": "error", "python_version": None, "reasons": [result.stderr.strip() or result.stdout.strip() or "Runtime doctor failed"]}


def escape_html(value: object) -> str:
    text = str(value if value is not None else "")
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#039;")
    )


def render_report_html(job: JobRecord) -> str:
    public = public_job(job)
    findings = public.get("findings") or []
    finding_html = "\n".join(
        f"""
        <article class="finding">
          <div><strong>{escape_html(item.get('title'))}</strong><span>{escape_html(item.get('severity'))}</span></div>
          <p>{escape_html(item.get('detail'))}</p>
        </article>
        """
        for item in findings
    )
    raw = escape_html(json.dumps(public, indent=2, sort_keys=True))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape_html(job.label)} Report</title>
  <style>
    body{{margin:0;background:#f5f7f2;color:#18211c;font-family:Inter,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
    main{{max-width:1040px;margin:0 auto;padding:36px 20px}}
    header{{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;border-bottom:1px solid #dce4d8;padding-bottom:22px;margin-bottom:22px}}
    h1{{margin:0;font-size:32px;letter-spacing:0}} p{{color:#69766d;line-height:1.55}}
    .badge{{display:inline-flex;border-radius:99px;padding:6px 10px;background:#dff1e8;color:#237255;font-weight:800;font-size:12px;text-transform:uppercase}}
    .grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:18px 0}}
    .card,.finding{{background:white;border:1px solid #dce4d8;border-radius:8px;box-shadow:0 12px 34px rgba(31,44,34,.07)}}
    .card{{padding:16px}} .card span{{display:block;color:#69766d;font-size:12px;text-transform:uppercase;font-weight:800}} .card strong{{display:block;margin-top:8px;font-size:18px;word-break:break-word}}
    .findings{{display:grid;gap:10px;margin-top:12px}} .finding{{padding:14px}} .finding div{{display:flex;justify-content:space-between;gap:12px}} .finding span{{color:#a2651b;font-size:12px;font-weight:800;text-transform:uppercase}}
    pre{{white-space:pre-wrap;word-break:break-word;background:#eef2ed;border:1px solid #dce4d8;border-radius:8px;padding:14px;max-height:520px;overflow:auto;font-size:12px}}
    @media(max-width:760px){{header,.grid{{display:grid;grid-template-columns:1fr}}}}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>{escape_html(job.label)}</h1>
      <p>{escape_html(job.url)}</p>
    </div>
    <span class="badge">{escape_html(job.status)}</span>
  </header>
  <section class="grid">
    <div class="card"><span>Module</span><strong>{escape_html(job.module)}</strong></div>
    <div class="card"><span>Command</span><strong>{escape_html(job.command or "Completed")}</strong></div>
    <div class="card"><span>Findings</span><strong>{len(findings)}</strong></div>
  </section>
  <h2>Findings</h2>
  <section class="findings">{finding_html}</section>
  <h2>Raw Audit JSON</h2>
  <pre>{raw}</pre>
</main>
</body>
</html>"""


@app.get("/")
async def frontend() -> FileResponse:
    return FileResponse(WEB_ROOT / "frontend.html")


@app.get("/verify-email")
async def verify_email_page() -> FileResponse:
    return FileResponse(WEB_ROOT / "frontend.html")


@app.get("/reset-password")
async def reset_password_page() -> FileResponse:
    return FileResponse(WEB_ROOT / "frontend.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return run_runtime_doctor()


@app.get("/api/ready")
async def ready():
    runtime = run_runtime_doctor()
    try:
        with db() as conn:
            db_ok = conn.execute("SELECT 1 AS ok").fetchone()["ok"] == 1
            queued = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status = 'queued'").fetchone()["n"]
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={
                "ready": False,
                "database": False,
                "database_backend": DATABASE_BACKEND,
                "database_location": "DATABASE_URL" if DATABASE_BACKEND == "postgres" else str(DB_PATH),
                "database_error": str(exc),
                "runtime": runtime,
                "queued_jobs": None,
            },
        )
    return {
        "ready": bool(runtime.get("ready")) and db_ok,
        "database": db_ok,
        "database_backend": DATABASE_BACKEND,
        "database_location": "DATABASE_URL" if DATABASE_BACKEND == "postgres" else str(DB_PATH),
        "runtime": runtime,
        "queued_jobs": queued,
    }


@app.get("/api/modules")
async def modules() -> list[dict[str, str]]:
    return [{"id": key, "label": value["label"], "category": value["category"], "description": value["description"]} for key, value in MODULES.items()]


@app.get("/terms")
async def terms_page() -> HTMLResponse:
    return HTMLResponse("""<!doctype html><html><head><title>Terms - Ranklytics</title><meta name="viewport" content="width=device-width, initial-scale=1"></head><body style="font-family:system-ui;max-width:860px;margin:40px auto;line-height:1.6;padding:0 20px"><h1>Terms of Service</h1><p>This development build is provided for evaluation. Production terms should be reviewed by counsel before public launch.</p><h2>Acceptable Use</h2><p>Users may only audit websites they own, manage, or are authorized to evaluate.</p><h2>Service Availability</h2><p>Audit results are informational and depend on third-party services and website availability.</p></body></html>""")


@app.get("/privacy")
async def privacy_page() -> HTMLResponse:
    return HTMLResponse("""<!doctype html><html><head><title>Privacy - Ranklytics</title><meta name="viewport" content="width=device-width, initial-scale=1"></head><body style="font-family:system-ui;max-width:860px;margin:40px auto;line-height:1.6;padding:0 20px"><h1>Privacy Policy</h1><p>This development build stores account, project, site, audit, and email-outbox data in the configured application database.</p><h2>Customer Data</h2><p>Website URLs and audit outputs are stored to provide history, exports, and shareable reports.</p><h2>Production Review</h2><p>Before public launch, replace this placeholder with a jurisdiction-specific policy reviewed by counsel.</p></body></html>""")


@app.post("/api/auth/signup")
async def signup(request: AuthRequest) -> dict[str, Any]:
    if not PUBLIC_SIGNUP_ENABLED:
        raise HTTPException(status_code=403, detail="Public signup is disabled. Contact your workspace administrator for paid member access.")
    email = normalize_email(request.email)
    user_id = uuid.uuid4().hex
    created = now()
    role = "admin" if is_first_user() else "user"
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO users (id, email, name, password_hash, plan, email_verified, role, subscription_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, email, request.name or email.split("@")[0], hash_password(request.password), "free", 0, role, "dev", created),
            )
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    except Exception as exc:
        if db_integrity_error(exc):
            raise HTTPException(status_code=409, detail="An account already exists for this email") from exc
        raise
    verification_token = create_email_verification_token(user_id)
    send_verification_email(user_id, email, verification_token)
    log_event(user_id, "auth.signup", "user", user_id, {"role": role})
    response = {"token": create_session(user_id), "user": user_public(row), "verification_required": True}
    if DEV_EMAIL_TOKENS:
        response["verification_token"] = verification_token
    return response


@app.post("/api/auth/login")
async def login(request: AuthRequest) -> dict[str, Any]:
    email = normalize_email(request.email)
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not row or not verify_password(request.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    log_event(row["id"], "auth.login", "user", row["id"])
    return {"token": create_session(row["id"]), "user": user_public(row)}


@app.post("/api/auth/logout")
async def logout(authorization: str | None = Header(default=None)) -> dict[str, bool]:
    if authorization and authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        with db() as conn:
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    return {"ok": True}


@app.post("/api/auth/verify-email")
async def verify_email(request: VerifyEmailRequest) -> dict[str, bool]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM email_verification_tokens WHERE token = ? AND used_at IS NULL AND expires_at > ?",
            (request.token, now()),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="Invalid or expired verification token")
        ts = now()
        conn.execute("UPDATE users SET email_verified = 1 WHERE id = ?", (row["user_id"],))
        conn.execute("UPDATE email_verification_tokens SET used_at = ? WHERE token = ?", (ts, request.token))
    log_event(row["user_id"], "auth.verify_email", "user", row["user_id"])
    return {"ok": True}


@app.post("/api/auth/password-reset/request")
async def request_password_reset(request: PasswordResetRequest) -> dict[str, Any]:
    email = normalize_email(request.email)
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not row:
        return {"ok": True}
    token = create_password_reset_token(row["id"])
    send_password_reset_email(row["id"], email, token)
    log_event(row["id"], "auth.password_reset_requested", "user", row["id"])
    response = {"ok": True}
    if DEV_EMAIL_TOKENS:
        response["reset_token"] = token
    return response


@app.post("/api/auth/password-reset/confirm")
async def confirm_password_reset(request: PasswordResetConfirm) -> dict[str, bool]:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM password_reset_tokens WHERE token = ? AND used_at IS NULL AND expires_at > ?",
            (request.token, now()),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="Invalid or expired reset token")
        ts = now()
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(request.password), row["user_id"]))
        conn.execute("UPDATE password_reset_tokens SET used_at = ? WHERE token = ?", (ts, request.token))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["user_id"],))
    log_event(row["user_id"], "auth.password_reset_confirmed", "user", row["user_id"])
    return {"ok": True}


@app.get("/api/dev/email-outbox")
async def dev_email_outbox(recipient: str | None = None) -> list[dict[str, Any]]:
    if not DEV_EMAIL_TOKENS:
        raise HTTPException(status_code=404, detail="Dev email outbox is disabled")
    if recipient:
        query = "SELECT * FROM email_outbox WHERE recipient = ? ORDER BY created_at DESC"
        params = (normalize_email(recipient),)
    else:
        query = "SELECT * FROM email_outbox ORDER BY created_at DESC LIMIT 50"
        params = ()
    with db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/me")
async def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return {"user": user, "plan_limits": PLAN_LIMITS.get(user.get("plan", "free"), PLAN_LIMITS["free"])}


@app.post("/api/me/profile")
async def update_profile(request: ProfileUpdateRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    with db() as conn:
        conn.execute("UPDATE users SET name = ? WHERE id = ?", (name, user["id"]))
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    log_event(user["id"], "user.profile_update", "user", user["id"], {"name": name})
    return {"user": user_public(row)}


@app.post("/api/me/password")
async def change_password(request: ChangePasswordRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, bool]:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
        if not row or not verify_password(request.current_password, row["password_hash"]):
            raise HTTPException(status_code=401, detail="Current password is incorrect")
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(request.new_password), user["id"]))
    log_event(user["id"], "user.password_change", "user", user["id"])
    return {"ok": True}


@app.post("/api/me/resend-verification")
async def resend_verification(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if user.get("email_verified"):
        return {"ok": True, "already_verified": True}
    token = create_email_verification_token(user["id"])
    send_verification_email(user["id"], user["email"], token)
    log_event(user["id"], "auth.verify_email_resend", "user", user["id"])
    response = {"ok": True, "already_verified": False}
    if DEV_EMAIL_TOKENS:
        response["verification_token"] = token
    return response


@app.get("/api/team")
async def team_status(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as conn:
        invitations = [dict(row) for row in conn.execute("SELECT * FROM team_invitations WHERE owner_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()]
        members = [dict(row) for row in conn.execute("SELECT * FROM team_members WHERE owner_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()]
        memberships = [dict(row) for row in conn.execute("SELECT * FROM team_members WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()]
    return {"invitations": invitations, "members": members, "memberships": memberships}


@app.post("/api/team/invitations")
async def create_team_invitation(request: TeamInviteRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    email = normalize_email(request.email)
    role = request.role.strip().lower() or "member"
    if role not in {"member", "manager"}:
        raise HTTPException(status_code=400, detail="Team role must be member or manager")
    if email == user["email"]:
        raise HTTPException(status_code=400, detail="Invite a teammate email, not your own account")
    invite_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    ts = now()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO team_invitations (id, owner_id, email, role, token, status, created_at, expires_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (invite_id, user["id"], email, role, token, ts, ts + 60 * 60 * 24 * 7),
        )
        row = conn.execute("SELECT * FROM team_invitations WHERE id = ?", (invite_id,)).fetchone()
    send_team_invite_email(user["id"], email, token)
    log_event(user["id"], "team.invite_create", "team_invitation", invite_id, {"email": email, "role": role})
    return dict(row)


@app.post("/api/team/invitations/{invite_id}/revoke")
async def revoke_team_invitation(invite_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, bool]:
    with db() as conn:
        row = conn.execute("SELECT * FROM team_invitations WHERE id = ? AND owner_id = ?", (invite_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Invitation not found")
        conn.execute("UPDATE team_invitations SET status = 'revoked' WHERE id = ?", (invite_id,))
    log_event(user["id"], "team.invite_revoke", "team_invitation", invite_id)
    return {"ok": True}


@app.post("/api/team/accept")
async def accept_team_invitation(request: TeamInviteAcceptRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as conn:
        invite = conn.execute(
            "SELECT * FROM team_invitations WHERE token = ? AND status = 'pending' AND expires_at > ?",
            (request.token, now()),
        ).fetchone()
        if not invite:
            raise HTTPException(status_code=400, detail="Invalid or expired invitation")
        if normalize_email(invite["email"]) != normalize_email(user["email"]):
            raise HTTPException(status_code=403, detail="This invitation is for a different email")
        existing = conn.execute("SELECT * FROM team_members WHERE owner_id = ? AND user_id = ?", (invite["owner_id"], user["id"])).fetchone()
        member_id = existing["id"] if existing else uuid.uuid4().hex
        if not existing:
            conn.execute(
                "INSERT INTO team_members (id, owner_id, user_id, email, role, status, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?)",
                (member_id, invite["owner_id"], user["id"], user["email"], invite["role"], now()),
            )
        conn.execute("UPDATE team_invitations SET status = 'accepted', accepted_at = ? WHERE id = ?", (now(), invite["id"]))
        row = conn.execute("SELECT * FROM team_members WHERE id = ?", (member_id,)).fetchone()
    log_event(user["id"], "team.invite_accept", "team_invitation", invite["id"], {"owner_id": invite["owner_id"]})
    return {"member": dict(row)}


@app.get("/api/billing/plans")
async def billing_plans() -> dict[str, Any]:
    return {"plans": PLAN_LIMITS}


@app.post("/api/billing/dev-upgrade")
async def dev_upgrade(request: BillingPlanRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if request.plan not in PLAN_LIMITS:
        raise HTTPException(status_code=400, detail="Unknown plan")
    with db() as conn:
        conn.execute("UPDATE users SET plan = ?, subscription_status = 'dev_active' WHERE id = ?", (request.plan, user["id"]))
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user["id"],)).fetchone()
    log_event(user["id"], "billing.dev_upgrade", "user", user["id"], {"plan": request.plan})
    return {"user": user_public(row), "plan_limits": PLAN_LIMITS[request.plan]}


@app.post("/api/billing/checkout")
async def billing_checkout(request: BillingPlanRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if request.plan not in PLAN_LIMITS:
        raise HTTPException(status_code=400, detail="Unknown plan")
    if stripe_configured_for_plan(request.plan):
        try:
            session = create_stripe_checkout_session(request.plan, user)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        checkout_url = session.get("url")
        if not checkout_url:
            raise HTTPException(status_code=502, detail="Stripe did not return a checkout URL")
        log_event(user["id"], "billing.checkout_created", "user", user["id"], {"plan": request.plan, "session_id": session.get("id")})
        return {
            "checkout_url": checkout_url,
            "mode": "stripe",
            "requested_plan": request.plan,
            "session_id": session.get("id"),
        }
    return {
        "checkout_url": None,
        "mode": "dev",
        "message": "Stripe is not configured. Use /api/billing/dev-upgrade in development.",
        "requested_plan": request.plan,
    }


@app.post("/api/billing/webhook")
async def stripe_billing_webhook(request: Request, stripe_signature: str | None = Header(default=None, alias="Stripe-Signature")) -> dict[str, Any]:
    payload = await request.body()
    verify_stripe_webhook_signature(payload, stripe_signature)
    try:
        event = json.loads(payload.decode("utf-8"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook JSON") from exc
    event_id = str(event.get("id") or "")
    if not event_id:
        raise HTTPException(status_code=400, detail="Stripe webhook event id is required")
    if event_already_processed(event_id):
        return {"ok": True, "duplicate": True}
    try:
        result = apply_stripe_event(event)
        store_stripe_event(event)
    except Exception as exc:
        store_stripe_event(event, str(exc))
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "duplicate": False, **result}


@app.post("/api/projects")
async def create_project(request: ProjectRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name is required")
    enforce_project_limit(user)
    project_id = uuid.uuid4().hex
    ts = now()
    with db() as conn:
        conn.execute(
            "INSERT INTO projects (id, user_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (project_id, user["id"], name, ts, ts),
        )
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    log_event(user["id"], "project.create", "project", project_id, {"name": name})
    return dict(row)


@app.get("/api/projects")
async def list_projects(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM projects WHERE user_id = ? ORDER BY updated_at DESC", (user["id"],)).fetchall()
    return [dict(row) for row in rows]


@app.patch("/api/projects/{project_id}")
async def update_project(project_id: str, request: ProjectUpdateRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name is required")
    with db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ? AND user_id = ?", (project_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Project not found")
        conn.execute("UPDATE projects SET name = ?, updated_at = ? WHERE id = ?", (name, now(), project_id))
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    log_event(user["id"], "project.update", "project", project_id, {"name": name})
    return dict(row)


@app.delete("/api/projects/{project_id}")
async def delete_project(project_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, bool]:
    with db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ? AND user_id = ?", (project_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Project not found")
        job_count = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE project_id = ?", (project_id,)).fetchone()["n"]
        if job_count:
            raise HTTPException(status_code=409, detail="Projects with audit history cannot be deleted")
        conn.execute("DELETE FROM sites WHERE project_id = ? AND user_id = ?", (project_id, user["id"]))
        conn.execute("DELETE FROM projects WHERE id = ? AND user_id = ?", (project_id, user["id"]))
    log_event(user["id"], "project.delete", "project", project_id)
    return {"ok": True}


@app.post("/api/sites")
async def create_site(request: SiteRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    enforce_site_limit(user)
    with db() as conn:
        project = conn.execute("SELECT * FROM projects WHERE id = ? AND user_id = ?", (request.project_id, user["id"])).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        site_id = uuid.uuid4().hex
        ts = now()
        conn.execute(
            "INSERT INTO sites (id, user_id, project_id, name, url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (site_id, user["id"], request.project_id, request.name or str(request.url), str(request.url), ts, ts),
        )
        row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    log_event(user["id"], "site.create", "site", site_id, {"project_id": request.project_id, "url": str(request.url)})
    return dict(row)


@app.get("/api/sites")
async def list_sites(project_id: str | None = None, user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    if project_id:
        query = "SELECT * FROM sites WHERE user_id = ? AND project_id = ? ORDER BY updated_at DESC"
        params = (user["id"], project_id)
    else:
        query = "SELECT * FROM sites WHERE user_id = ? ORDER BY updated_at DESC"
        params = (user["id"],)
    with db() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


@app.patch("/api/sites/{site_id}")
async def update_site(site_id: str, request: SiteUpdateRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM sites WHERE id = ? AND user_id = ?", (site_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Site not found")
        project_id = request.project_id or row["project_id"]
        project = conn.execute("SELECT id FROM projects WHERE id = ? AND user_id = ?", (project_id, user["id"])).fetchone()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        url = str(request.url) if request.url else row["url"]
        name = request.name.strip() if request.name is not None else row["name"]
        if not name:
            name = url
        conn.execute(
            "UPDATE sites SET project_id = ?, name = ?, url = ?, updated_at = ? WHERE id = ? AND user_id = ?",
            (project_id, name, url, now(), site_id, user["id"]),
        )
        row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    log_event(user["id"], "site.update", "site", site_id, {"project_id": project_id, "url": url})
    return dict(row)


@app.delete("/api/sites/{site_id}")
async def delete_site(site_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, bool]:
    with db() as conn:
        row = conn.execute("SELECT * FROM sites WHERE id = ? AND user_id = ?", (site_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Site not found")
        job_count = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE site_id = ?", (site_id,)).fetchone()["n"]
        if job_count:
            raise HTTPException(status_code=409, detail="Sites with audit history cannot be deleted")
        conn.execute("DELETE FROM sites WHERE id = ? AND user_id = ?", (site_id, user["id"]))
    log_event(user["id"], "site.delete", "site", site_id)
    return {"ok": True}


@app.post("/api/jobs")
async def create_job(request: JobRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if request.module not in MODULES:
        raise HTTPException(status_code=404, detail="Unknown audit module")
    check_rate_limit(user)
    with db() as conn:
        if request.project_id:
            project = conn.execute("SELECT id FROM projects WHERE id = ? AND user_id = ?", (request.project_id, user["id"])).fetchone()
            if not project:
                raise HTTPException(status_code=404, detail="Project not found")
        if request.site_id:
            site = conn.execute("SELECT id FROM sites WHERE id = ? AND user_id = ?", (request.site_id, user["id"])).fetchone()
            if not site:
                raise HTTPException(status_code=404, detail="Site not found")
    ts = now()
    record = JobRecord(
        id=uuid.uuid4().hex,
        user_id=user["id"],
        project_id=request.project_id,
        site_id=request.site_id,
        module=request.module,
        label=MODULES[request.module]["label"],
        url=str(request.url),
        status="queued",
        created_at=ts,
        updated_at=ts,
    )
    with JOBS_LOCK:
        JOBS[record.id] = record
    persist_job(record)
    WORKER_EVENT.set()
    log_event(user["id"], "job.create", "job", record.id, {"module": request.module, "url": str(request.url)})
    return public_job(record)


@app.get("/api/schedules")
async def list_schedules(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM scheduled_audits WHERE user_id = ? ORDER BY created_at DESC", (user["id"],)).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/schedules")
async def create_schedule(request: ScheduleRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if request.module not in MODULES:
        raise HTTPException(status_code=404, detail="Unknown audit module")
    with db() as conn:
        if request.project_id:
            project = conn.execute("SELECT id FROM projects WHERE id = ? AND user_id = ?", (request.project_id, user["id"])).fetchone()
            if not project:
                raise HTTPException(status_code=404, detail="Project not found")
        if request.site_id:
            site = conn.execute("SELECT id FROM sites WHERE id = ? AND user_id = ?", (request.site_id, user["id"])).fetchone()
            if not site:
                raise HTTPException(status_code=404, detail="Site not found")
        schedule_id = uuid.uuid4().hex
        ts = now()
        conn.execute(
            """
            INSERT INTO scheduled_audits (id, user_id, project_id, site_id, module, label, url, frequency, status, next_run_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                schedule_id,
                user["id"],
                request.project_id,
                request.site_id,
                request.module,
                MODULES[request.module]["label"],
                str(request.url),
                request.frequency,
                ts + frequency_seconds(request.frequency),
                ts,
                ts,
            ),
        )
        row = conn.execute("SELECT * FROM scheduled_audits WHERE id = ?", (schedule_id,)).fetchone()
    log_event(user["id"], "schedule.create", "scheduled_audit", schedule_id, {"frequency": request.frequency, "module": request.module, "url": str(request.url)})
    return dict(row)


@app.delete("/api/schedules/{schedule_id}")
async def delete_schedule(schedule_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, bool]:
    with db() as conn:
        row = conn.execute("SELECT * FROM scheduled_audits WHERE id = ? AND user_id = ?", (schedule_id, user["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Schedule not found")
        conn.execute("UPDATE scheduled_audits SET status = 'paused', updated_at = ? WHERE id = ?", (now(), schedule_id))
    log_event(user["id"], "schedule.pause", "scheduled_audit", schedule_id)
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    record = load_job(job_id, user["id"])
    if not record or record.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="Job not found")
    return public_job(record)


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    record = load_job(job_id, user["id"])
    if not record or record.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="Job not found")
    if record.status != "queued":
        raise HTTPException(status_code=409, detail="Only queued jobs can be cancelled")
    cancelled = update_job(job_id, status="cancelled")
    return public_job(cancelled)


@app.get("/api/jobs/{job_id}/export.json")
async def export_job_json(job_id: str, user: dict[str, Any] = Depends(current_user)) -> Response:
    record = load_job(job_id, user["id"])
    if not record or record.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="Job not found")
    content = json.dumps(public_job(record), indent=2, sort_keys=True)
    return Response(content, media_type="application/json", headers={"Content-Disposition": f'attachment; filename="{job_id}.json"'})


@app.get("/api/jobs/{job_id}/report.html")
async def export_job_html(job_id: str, user: dict[str, Any] = Depends(current_user)) -> HTMLResponse:
    record = load_job(job_id, user["id"])
    if not record or record.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="Job not found")
    return HTMLResponse(render_report_html(record))


@app.get("/api/history")
async def history(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?", (user["id"], MAX_HISTORY)).fetchall()
    return [public_job(row_to_job(row)) for row in rows]


@app.get("/api/admin/summary")
async def admin_summary(admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    with db() as conn:
        counts = {
            "users": conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"],
            "projects": conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"],
            "sites": conn.execute("SELECT COUNT(*) AS n FROM sites").fetchone()["n"],
            "jobs": conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"],
            "queued_jobs": conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status = 'queued'").fetchone()["n"],
            "completed_jobs": conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status = 'complete'").fetchone()["n"],
        }
        plans = [dict(row) for row in conn.execute("SELECT plan, COUNT(*) AS users FROM users GROUP BY plan").fetchall()]
    return {"counts": counts, "plans": plans}


@app.get("/api/admin/users")
async def admin_users(admin: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 200").fetchall()
    return [user_public(row) for row in rows]


@app.post("/api/admin/users/{user_id}/plan")
async def admin_set_user_plan(user_id: str, request: AdminUserPlanRequest, admin: dict[str, Any] = Depends(require_admin)) -> dict[str, Any]:
    if request.plan not in PLAN_LIMITS:
        raise HTTPException(status_code=400, detail="Unknown plan")
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="User not found")
        conn.execute("UPDATE users SET plan = ?, subscription_status = 'admin_set' WHERE id = ?", (request.plan, user_id))
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    log_event(admin["id"], "admin.user_plan_update", "user", user_id, {"plan": request.plan})
    return {"user": user_public(row), "plan_limits": PLAN_LIMITS[request.plan]}


@app.get("/api/admin/jobs")
async def admin_jobs(admin: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT 200").fetchall()
    return [public_job(row_to_job(row)) for row in rows]


@app.get("/api/admin/audit-logs")
async def admin_audit_logs(admin: dict[str, Any] = Depends(require_admin)) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM audit_logs ORDER BY created_at DESC LIMIT 200").fetchall()
    return [dict(row) for row in rows]


@app.get("/api/admin/backup.json")
async def admin_backup(admin: dict[str, Any] = Depends(require_admin)) -> Response:
    payload = backup_payload()
    log_event(admin["id"], "admin.backup_export", "backup", None)
    return Response(
        json.dumps(payload, indent=2, sort_keys=True),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="claude-seo-backup.json"'},
    )


@app.get("/api/share/{share_token}")
async def shared_report(share_token: str) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE share_token = ? AND status = 'complete'", (share_token,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Shared report not found")
    job = row_to_job(row)
    public = public_job(job)
    public.pop("user_id", None)
    return public


@app.get("/share/{share_token}")
async def public_shared_report_page(share_token: str) -> HTMLResponse:
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE share_token = ? AND status = 'complete'", (share_token,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Shared report not found")
    return HTMLResponse(render_report_html(row_to_job(row)))


@app.get("/api/share/{share_token}/export.json")
async def public_shared_report_json(share_token: str) -> Response:
    with db() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE share_token = ? AND status = 'complete'", (share_token,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Shared report not found")
    public = public_job(row_to_job(row))
    public.pop("user_id", None)
    return Response(json.dumps(public, indent=2, sort_keys=True), media_type="application/json")


@app.post("/api/audit")
async def audit(request: AuditRequest, user: dict[str, Any] = Depends(current_user)) -> AuditResult:
    try:
        command, result = run_module("overview", str(request.url))
        return AuditResult(success=True, command=command, output=result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
