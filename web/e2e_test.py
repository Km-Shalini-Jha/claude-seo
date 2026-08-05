#!/usr/bin/env python3
"""End-to-end API test for the Claude SEO SaaS console."""

from __future__ import annotations

import json
import hashlib
import hmac
import os
import socket
import sqlite3
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


def request(base: str, path: str, payload: dict | None = None, token: str | None = None, extra_headers: dict[str, str] | None = None, method: str | None = None) -> tuple[int, str]:
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(f"{base}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def stripe_signature(payload: dict, secret: str) -> tuple[dict, dict[str, str]]:
    timestamp = int(time.time())
    body = json.dumps(payload).encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), str(timestamp).encode("utf-8") + b"." + body, hashlib.sha256).hexdigest()
    return payload, {"Stripe-Signature": f"t={timestamp},v1={digest}"}


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


def wait_for_job(base: str, job_id: str, token: str, worker: subprocess.Popen[str] | None = None) -> dict:
    deadline = time.time() + 12
    last = None
    while time.time() < deadline:
        if worker and worker.poll() is not None:
            raise AssertionError(f"worker exited early: {worker.stderr.read() if worker.stderr else ''}")
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
    env["CLAUDE_SEO_WEB_DEV_EMAIL_TOKENS"] = "1"
    env["CLAUDE_SEO_WEB_AUTO_WORKER"] = "0"
    env["CLAUDE_SEO_WEB_DATA"] = str(data_dir)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    env["CLAUDE_SEO_WORKER_LOG"] = str(data_dir / "worker.log")
    env["CLAUDE_SEO_STRIPE_WEBHOOK_SECRET"] = "whsec_local_test"
    proc = subprocess.Popen(
        [SERVER_PYTHON, "-m", "uvicorn", "web.backend:app", "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    worker = None
    try:
        wait_for_server(base, proc)

        status, body = request(base, "/api/modules")
        assert status == 200, body
        modules = {item["id"] for item in json.loads(body)}
        assert "full-audit" in modules
        assert "auto-fix-generator" in modules
        assert "indexing" in modules
        assert "schema-generator" in modules

        status, body = request(base, "/api/auth/signup", {"email": "owner@example.com", "password": "long-password", "name": "Owner"})
        assert status == 200, body
        owner = json.loads(body)
        token = owner["token"]
        verification_token = owner["verification_token"]
        assert owner["user"]["role"] == "admin"

        status, body = request(base, "/api/ready")
        assert status == 200, body
        assert json.loads(body)["database"] is True

        status, body = request(base, "/api/dev/email-outbox?recipient=owner%40example.com")
        assert status == 200, body
        assert any(row["purpose"] == "email_verification" for row in json.loads(body))

        status, body = request(base, "/api/auth/verify-email", {"token": verification_token})
        assert status == 200, body

        status, body = request(base, "/api/billing/checkout", {"plan": "pro"}, token)
        assert status == 200, body
        assert json.loads(body)["mode"] == "dev"

        status, body = request(base, "/api/billing/dev-upgrade", {"plan": "pro"}, token)
        assert status == 200, body
        assert json.loads(body)["user"]["plan"] == "pro"

        stripe_event = {
            "id": "evt_checkout_completed",
            "type": "checkout.session.completed",
            "data": {
                "object": {
                    "id": "cs_test_123",
                    "mode": "subscription",
                    "customer": "cus_test_123",
                    "subscription": "sub_test_123",
                    "client_reference_id": owner["user"]["id"],
                    "metadata": {"user_id": owner["user"]["id"], "plan": "agency"},
                }
            },
        }
        payload, headers = stripe_signature(stripe_event, "whsec_local_test")
        status, body = request(base, "/api/billing/webhook", payload, extra_headers=headers)
        assert status == 200, body
        assert json.loads(body)["handled"] is True
        status, body = request(base, "/api/billing/webhook", payload, extra_headers=headers)
        assert status == 200, body
        assert json.loads(body)["duplicate"] is True
        status, body = request(base, "/api/me", token=token)
        assert status == 200, body
        assert json.loads(body)["user"]["plan"] == "agency"

        status, body = request(base, "/api/auth/password-reset/request", {"email": "owner@example.com"})
        assert status == 200, body
        reset_token = json.loads(body)["reset_token"]
        status, body = request(base, "/api/auth/password-reset/confirm", {"token": reset_token, "password": "new-long-password"})
        assert status == 200, body
        status, body = request(base, "/api/auth/login", {"email": "owner@example.com", "password": "new-long-password"})
        assert status == 200, body
        token = json.loads(body)["token"]

        status, body = request(base, "/api/me/profile", {"name": "Owner Updated"}, token)
        assert status == 200, body
        assert json.loads(body)["user"]["name"] == "Owner Updated"

        status, body = request(base, "/api/me/password", {"current_password": "new-long-password", "new_password": "changed-long-password"}, token)
        assert status == 200, body
        status, body = request(base, "/api/auth/login", {"email": "owner@example.com", "password": "changed-long-password"})
        assert status == 200, body
        token = json.loads(body)["token"]

        status, body = request(base, "/api/me/resend-verification", {}, token)
        assert status == 200, body
        assert json.loads(body)["already_verified"] is True

        status, body = request(base, "/api/team/invitations", {"email": "other@example.com", "role": "manager"}, token)
        assert status == 200, body
        team_invite = json.loads(body)
        assert team_invite["status"] == "pending"
        assert team_invite["token"]
        status, body = request(base, "/api/team/invitations", {"email": "temp@example.com", "role": "member"}, token)
        assert status == 200, body
        temp_invite_id = json.loads(body)["id"]
        status, body = request(base, f"/api/team/invitations/{temp_invite_id}/revoke", token=token, method="POST")
        assert status == 200, body
        status, body = request(base, "/api/team", token=token)
        assert status == 200, body
        team_state = json.loads(body)
        assert any(row["email"] == "other@example.com" and row["status"] == "pending" for row in team_state["invitations"])
        assert any(row["id"] == temp_invite_id and row["status"] == "revoked" for row in team_state["invitations"])

        status, body = request(base, "/api/projects", {"name": "Acme Client"}, token)
        assert status == 200, body
        project_id = json.loads(body)["id"]

        status, body = request(base, "/api/sites", {"project_id": project_id, "url": "https://example.com", "name": "Example"}, token)
        assert status == 200, body
        site_id = json.loads(body)["id"]

        status, body = request(base, f"/api/projects/{project_id}", {"name": "Acme Client Updated"}, token, method="PATCH")
        assert status == 200, body
        assert json.loads(body)["name"] == "Acme Client Updated"

        status, body = request(
            base,
            f"/api/sites/{site_id}",
            {"project_id": project_id, "url": "https://example.com/about", "name": "Example About"},
            token,
            method="PATCH",
        )
        assert status == 200, body
        assert json.loads(body)["name"] == "Example About"

        status, body = request(base, "/api/projects", {"name": "Delete Me"}, token)
        assert status == 200, body
        temp_project_id = json.loads(body)["id"]
        status, body = request(base, "/api/sites", {"project_id": temp_project_id, "url": "https://delete.example", "name": "Delete Site"}, token)
        assert status == 200, body
        temp_site_id = json.loads(body)["id"]
        status, body = request(base, f"/api/sites/{temp_site_id}", token=token, method="DELETE")
        assert status == 200, body
        status, body = request(base, f"/api/projects/{temp_project_id}", token=token, method="DELETE")
        assert status == 200, body

        status, body = request(
            base,
            "/api/schedules",
            {"url": "https://example.com", "module": "overview", "frequency": "daily", "project_id": project_id, "site_id": site_id},
            token,
        )
        assert status == 200, body
        paused_schedule_id = json.loads(body)["id"]
        status, body = request(base, "/api/schedules", token=token)
        assert status == 200, body
        assert any(row["id"] == paused_schedule_id and row["status"] == "active" for row in json.loads(body))
        status, body = request(base, f"/api/schedules/{paused_schedule_id}", token=token, method="DELETE")
        assert status == 200, body

        status, body = request(
            base,
            "/api/schedules",
            {"url": "https://example.com", "module": "overview", "frequency": "hourly", "project_id": project_id, "site_id": site_id},
            token,
        )
        assert status == 200, body
        due_schedule_id = json.loads(body)["id"]
        with sqlite3.connect(data_dir / "console.sqlite3") as conn:
            conn.execute("UPDATE scheduled_audits SET next_run_at = ? WHERE id = ?", (time.time() - 2, due_schedule_id))

        status, body = request(base, "/api/admin/summary", token=token)
        assert status == 200, body
        assert json.loads(body)["counts"]["users"] == 1

        status, body = request(
            base,
            "/api/jobs",
            {"url": "https://example.com", "module": "full-audit", "project_id": project_id, "site_id": site_id},
            token,
        )
        assert status == 200, body
        job_id = json.loads(body)["id"]
        worker = subprocess.Popen(
            [SERVER_PYTHON, "-m", "web.worker"],
            cwd=str(Path(__file__).resolve().parent.parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            completed = wait_for_job(base, job_id, token, worker)
        except AssertionError as exc:
            log = (data_dir / "worker.log").read_text(encoding="utf-8") if (data_dir / "worker.log").exists() else "<no worker log>"
            raise AssertionError(f"{exc}\nworker log:\n{log}") from exc
        assert completed["status"] == "complete", completed
        assert completed["share_url"], completed
        assert completed["findings"], completed
        assert "overview" in completed["output"]["stdout"]

        scheduled_job_id = None
        deadline = time.time() + 8
        while time.time() < deadline:
            status, body = request(base, "/api/schedules", token=token)
            assert status == 200, body
            schedule = next(row for row in json.loads(body) if row["id"] == due_schedule_id)
            scheduled_job_id = schedule["last_job_id"]
            if scheduled_job_id:
                break
            time.sleep(0.1)
        assert scheduled_job_id, "due schedule was not converted into a job"
        scheduled = wait_for_job(base, scheduled_job_id, token, worker)
        assert scheduled["status"] == "complete", scheduled

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

        status, body = request(base, "/api/admin/backup.json", token=token)
        assert status == 200, body
        assert "audit_logs" in json.loads(body)["tables"]

        status, body = request(base, "/terms")
        assert status == 200, body
        assert "Terms of Service" in body

        status, body = request(base, "/privacy")
        assert status == 200, body
        assert "Privacy Policy" in body

        status, body = request(base, "/api/auth/signup", {"email": "other@example.com", "password": "long-password", "name": "Other"})
        assert status == 200, body
        other = json.loads(body)
        other_token = other["token"]
        assert other["user"]["role"] == "user"
        status, body = request(base, "/api/team/accept", {"token": team_invite["token"]}, other_token)
        assert status == 200, body
        assert json.loads(body)["member"]["role"] == "manager"
        status, body = request(base, "/api/team", token=other_token)
        assert status == 200, body
        assert any(row["owner_id"] == owner["user"]["id"] for row in json.loads(body)["memberships"])
        status, body = request(base, f"/api/jobs/{job_id}", token=other_token)
        assert status == 404, body

        status, body = request(base, "/api/admin/summary", token=other_token)
        assert status == 403, body

        status, body = request(base, f"/api/admin/users/{other['user']['id']}/plan", {"plan": "agency"}, token)
        assert status == 200, body
        assert json.loads(body)["user"]["plan"] == "agency"

        status, body = request(base, "/")
        assert status == 200, body[:200]
        assert "Client SEO audits" in body
        assert (data_dir / "console.sqlite3").exists()
    finally:
        if worker:
            worker.terminate()
        proc.terminate()
        if worker:
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("saas web e2e passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
