#!/usr/bin/env python3
"""End-to-end API test for the Claude SEO SaaS console."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SERVER_PYTHON = os.environ.get("CLAUDE_SEO_WEB_SERVER_PYTHON", "/opt/homebrew/bin/python3.11")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def request(base: str, path: str, payload: dict | None = None, token: str | None = None) -> tuple[int, str]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{base}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def wait_for_server(base: str, proc: subprocess.Popen[str]) -> None:
    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early: {proc.stderr.read() if proc.stderr else ''}")
        try:
            status, _ = request(base, "/api/health")
            if status == 200:
                return
        except Exception:
            time.sleep(0.1)
    raise AssertionError("server did not start")


def wait_for_job(base: str, job_id: str, token: str) -> dict:
    deadline = time.time() + 12
    last = None
    while time.time() < deadline:
        status, body = request(base, f"/api/jobs/{job_id}", token=token)
        assert status == 200, body
        last = json.loads(body)
        if last["status"] in {"complete", "error"}:
            return last
        time.sleep(0.1)
    raise AssertionError(f"job did not finish: {last}")


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="claude-seo-saas-test-"))
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["CLAUDE_SEO_WEB_FAKE_RUNS"] = "1"
    env["CLAUDE_SEO_WEB_DATA"] = str(data_dir)
    proc = subprocess.Popen(
        [SERVER_PYTHON, "-m", "uvicorn", "web.backend:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        wait_for_server(base, proc)

        status, body = request(base, "/api/modules")
        assert status == 200, body
        assert "full-audit" in {item["id"] for item in json.loads(body)}

        status, body = request(base, "/api/auth/signup", {"email": "owner@example.com", "password": "long-password", "name": "Owner"})
        assert status == 200, body
        owner = json.loads(body)
        token = owner["token"]
        verification_token = owner["verification_token"]

        status, body = request(base, "/api/auth/verify-email", {"token": verification_token})
        assert status == 200, body

        status, body = request(base, "/api/billing/dev-upgrade", {"plan": "pro"}, token)
        assert status == 200, body
        assert json.loads(body)["user"]["plan"] == "pro"

        status, body = request(base, "/api/auth/password-reset/request", {"email": "owner@example.com"})
        assert status == 200, body
        reset_token = json.loads(body)["reset_token"]
        status, body = request(base, "/api/auth/password-reset/confirm", {"token": reset_token, "password": "new-long-password"})
        assert status == 200, body
        status, body = request(base, "/api/auth/login", {"email": "owner@example.com", "password": "new-long-password"})
        assert status == 200, body
        token = json.loads(body)["token"]

        status, body = request(base, "/api/projects", {"name": "Acme Client"}, token)
        assert status == 200, body
        project_id = json.loads(body)["id"]

        status, body = request(base, "/api/sites", {"project_id": project_id, "url": "https://example.com", "name": "Example"}, token)
        assert status == 200, body
        site_id = json.loads(body)["id"]

        status, body = request(
            base,
            "/api/jobs",
            {"url": "https://example.com", "module": "full-audit", "project_id": project_id, "site_id": site_id},
            token,
        )
        assert status == 200, body
        job_id = json.loads(body)["id"]
        completed = wait_for_job(base, job_id, token)
        assert completed["status"] == "complete", completed
        assert completed["share_url"], completed
        assert completed["findings"], completed
        assert "overview" in completed["output"]["stdout"]

        status, body = request(base, "/api/history", token=token)
        assert status == 200, body
        assert any(row["id"] == job_id for row in json.loads(body))

        status, body = request(base, completed["share_url"].replace("/share/", "/api/share/"))
        assert status == 200, body
        shared = json.loads(body)
        assert shared["id"] == job_id
        assert "user_id" not in shared

        status, body = request(base, completed["share_url"])
        assert status == 200, body
        assert "Raw Audit JSON" in body

        status, body = request(base, f"/api/jobs/{job_id}/report.html", token=token)
        assert status == 200, body
        assert "Findings" in body

        status, body = request(base, f"/api/jobs/{job_id}/export.json", token=token)
        assert status == 200, body
        assert json.loads(body)["id"] == job_id

        status, body = request(base, "/api/auth/signup", {"email": "other@example.com", "password": "long-password", "name": "Other"})
        assert status == 200, body
        other_token = json.loads(body)["token"]
        status, body = request(base, f"/api/jobs/{job_id}", token=other_token)
        assert status == 404, body

        status, body = request(base, "/")
        assert status == 200, body[:200]
        assert "Client SEO audits" in body
        assert (data_dir / "console.sqlite3").exists()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("saas web e2e passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
