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
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Literal

from fastapi import Depends, Header, HTTPException
from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl
from starlette.middleware.cors import CORSMiddleware

ROOT = Path(__file__).resolve().parent.parent
WEB_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("CLAUDE_SEO_WEB_DATA", WEB_ROOT / "data")).expanduser()
DB_PATH = DATA_DIR / "console.sqlite3"
DEFAULT_LAUNCHER = ROOT / "bin" / "claude-seo"
LAUNCHER = Path(os.environ.get("CLAUDE_SEO_LAUNCHER", DEFAULT_LAUNCHER)).expanduser()
PYTHON_OVERRIDE = os.environ.get("CLAUDE_SEO_PYTHON")
MAX_HISTORY = 80
SESSION_TTL_SECONDS = 60 * 60 * 24 * 14
RATE_LIMIT_WINDOW = 60 * 60
RATE_LIMIT_MAX_JOBS = int(os.environ.get("CLAUDE_SEO_WEB_RATE_LIMIT", "30"))

app = FastAPI(title="Claude SEO SaaS Console API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(
        "CLAUDE_SEO_WEB_ORIGINS",
        "http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:8001,http://localhost:8001",
    ).split(","),
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


class AuthRequest(BaseModel):
    email: str
    password: str
    name: str | None = None


class ProjectRequest(BaseModel):
    name: str


class SiteRequest(BaseModel):
    project_id: str
    url: HttpUrl
    name: str | None = None


class JobRequest(BaseModel):
    url: HttpUrl
    module: str
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
    status: Literal["queued", "running", "complete", "error"]
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


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE,
            name TEXT,
            password_hash TEXT NOT NULL,
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
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    migrations = {
        "user_id": "ALTER TABLE jobs ADD COLUMN user_id TEXT",
        "project_id": "ALTER TABLE jobs ADD COLUMN project_id TEXT",
        "site_id": "ALTER TABLE jobs ADD COLUMN site_id TEXT",
        "share_token": "ALTER TABLE jobs ADD COLUMN share_token TEXT",
    }
    for column, statement in migrations.items():
        if column not in existing:
            conn.execute(statement)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_user_updated ON jobs(user_id, updated_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sites_user_project ON sites(user_id, project_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_share_token ON jobs(share_token)")


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


def user_public(row: sqlite3.Row) -> dict[str, Any]:
    return {"id": row["id"], "email": row["email"], "name": row["name"], "created_at": row["created_at"]}


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


def check_rate_limit(user_id: str) -> None:
    cutoff = now() - RATE_LIMIT_WINDOW
    runs = [ts for ts in RATE_LIMITS.get(user_id, []) if ts >= cutoff]
    if len(runs) >= RATE_LIMIT_MAX_JOBS:
        raise HTTPException(status_code=429, detail="Audit rate limit reached")
    runs.append(now())
    RATE_LIMITS[user_id] = runs


def row_to_job(row: sqlite3.Row) -> JobRecord:
    output = json.loads(row["output_json"]) if row["output_json"] else None
    return JobRecord(
        id=row["id"],
        user_id=row["user_id"] if "user_id" in row.keys() else None,
        project_id=row["project_id"] if "project_id" in row.keys() else None,
        site_id=row["site_id"] if "site_id" in row.keys() else None,
        module=row["module"],
        label=row["label"],
        url=row["url"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        command=row["command"],
        output=output,
        error=row["error"],
        share_token=row["share_token"] if "share_token" in row.keys() else None,
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


def run_runtime_doctor() -> dict[str, Any]:
    if not LAUNCHER.exists():
        return {"ready": False, "browser_ready": False, "mode": "missing", "python_version": None, "reasons": [f"Claude SEO launcher not found: {LAUNCHER}"]}
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


@app.get("/")
async def frontend() -> FileResponse:
    return FileResponse(WEB_ROOT / "frontend.html")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return run_runtime_doctor()


@app.get("/api/modules")
async def modules() -> list[dict[str, str]]:
    return [{"id": key, "label": value["label"], "category": value["category"], "description": value["description"]} for key, value in MODULES.items()]


@app.post("/api/auth/signup")
async def signup(request: AuthRequest) -> dict[str, Any]:
    email = normalize_email(request.email)
    user_id = uuid.uuid4().hex
    created = now()
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO users (id, email, name, password_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, email, request.name or email.split("@")[0], hash_password(request.password), created),
            )
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="An account already exists for this email") from exc
    return {"token": create_session(user_id), "user": user_public(row)}


@app.post("/api/auth/login")
async def login(request: AuthRequest) -> dict[str, Any]:
    email = normalize_email(request.email)
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not row or not verify_password(request.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    return {"token": create_session(row["id"]), "user": user_public(row)}


@app.get("/api/me")
async def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    return {"user": user}


@app.post("/api/projects")
async def create_project(request: ProjectRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name is required")
    project_id = uuid.uuid4().hex
    ts = now()
    with db() as conn:
        conn.execute(
            "INSERT INTO projects (id, user_id, name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (project_id, user["id"], name, ts, ts),
        )
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    return dict(row)


@app.get("/api/projects")
async def list_projects(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM projects WHERE user_id = ? ORDER BY updated_at DESC", (user["id"],)).fetchall()
    return [dict(row) for row in rows]


@app.post("/api/sites")
async def create_site(request: SiteRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
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


@app.post("/api/jobs")
async def create_job(request: JobRequest, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if request.module not in MODULES:
        raise HTTPException(status_code=404, detail="Unknown audit module")
    check_rate_limit(user["id"])
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
    threading.Thread(target=execute_job, args=(record.id,), daemon=True).start()
    return public_job(record)


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    with JOBS_LOCK:
        record = JOBS.get(job_id)
    if not record:
        record = load_job(job_id, user["id"])
    if not record or record.user_id != user["id"]:
        raise HTTPException(status_code=404, detail="Job not found")
    return public_job(record)


@app.get("/api/history")
async def history(user: dict[str, Any] = Depends(current_user)) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM jobs WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?", (user["id"], MAX_HISTORY)).fetchall()
    return [public_job(row_to_job(row)) for row in rows]


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


@app.post("/api/audit")
async def audit(request: AuditRequest, user: dict[str, Any] = Depends(current_user)) -> AuditResult:
    try:
        command, result = run_module("overview", str(request.url))
        return AuditResult(success=True, command=command, output=result)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
