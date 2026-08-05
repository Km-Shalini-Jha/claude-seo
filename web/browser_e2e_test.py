#!/usr/bin/env python3
"""Browser E2E test for the Claude SEO SaaS console."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

SERVER_PYTHON = os.environ.get("CLAUDE_SEO_WEB_SERVER_PYTHON", "/opt/homebrew/bin/python3.11")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_server(base: str, proc: subprocess.Popen[str]) -> None:
    import urllib.request

    deadline = time.time() + 10
    while time.time() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"server exited early: {proc.stderr.read() if proc.stderr else ''}")
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=2) as resp:
                if resp.status == 200:
                    return
        except Exception:
            time.sleep(0.1)
    raise AssertionError("server did not start")


def main() -> int:
    data_dir = Path(tempfile.mkdtemp(prefix="claude-seo-browser-test-"))
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
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(base, wait_until="networkidle")
            expect(page.get_by_text("Client SEO audits")).to_be_visible()
            page.fill("#authName", "Browser Test")
            page.fill("#authEmail", "browser@example.com")
            page.fill("#authPassword", "long-password")
            page.click("#authSubmitBtn")
            expect(page.get_by_text("Account created. Please log in.")).to_be_visible(timeout=5000)
            expect(page.locator("#authSubmitBtn")).to_have_text("Log In")
            expect(page.locator("#authNameWrap")).to_be_hidden()
            page.fill("#authPassword", "wrong-password")
            page.click("#authSubmitBtn")
            expect(page.locator("#authError")).to_contain_text("No matching account found")
            page.fill("#authPassword", "long-password")
            page.click("#authSubmitBtn")
            expect(page.get_by_text("SEO audit command center")).to_be_visible(timeout=5000)
            expect(page.locator("#logoutBtn")).to_have_count(0)
            expect(page.locator("#sidebarLogoutBtn")).to_have_text("Log Out")
            page.click("#sidebarLogoutBtn")
            expect(page.get_by_text("Client SEO audits")).to_be_visible(timeout=5000)
            page.click("#showLoginBtn")
            expect(page.locator("#authSubmitBtn")).to_have_text("Log In")
            expect(page.locator("#authNameWrap")).to_be_hidden()
            page.fill("#authEmail", "browser@example.com")
            page.fill("#authPassword", "long-password")
            page.click("#authSubmitBtn")
            expect(page.get_by_text("SEO audit command center")).to_be_visible(timeout=5000)
            page.get_by_role("button", name="Settings").click()
            expect(page.get_by_role("heading", name="Account and security settings")).to_be_visible()
            page.fill("#settingsName", "Browser Test Updated")
            page.click("#saveProfileBtn")
            expect(page.get_by_text("Profile saved.")).to_be_visible()
            page.fill("#inviteEmail", "teammate@example.com")
            page.select_option("#inviteRole", "manager")
            page.click("#sendInviteBtn")
            expect(page.get_by_text("Invite queued.")).to_be_visible()
            expect(page.get_by_text("teammate@example.com")).to_be_visible()
            page.get_by_role("button", name="Revoke").click()
            expect(page.get_by_text("Invitation revoked.")).to_be_visible()
            page.get_by_role("button", name="Settings").click()
            page.click("[data-plan='pro']")
            expect(page.locator("#planBadge")).to_contain_text("pro")
            page.get_by_role("button", name="Audit Desk").click()
            expect(page.get_by_role("heading", name="SEO audit command center")).to_be_visible()
            page.fill("#newProject", "Acme Browser Client")
            page.click("#createProjectBtn")
            expect(page.locator("#projectSelect")).to_contain_text("Acme Browser Client")
            page.get_by_role("button", name="Projects").click()
            expect(page.get_by_role("heading", name="Client project portfolio")).to_be_visible()
            expect(page.get_by_role("heading", name="Acme Browser Client")).to_be_visible()
            page.get_by_role("button", name="Sites").click()
            expect(page.get_by_role("heading", name="Managed site inventory")).to_be_visible()
            page.get_by_role("button", name="Audit Desk").click()
            expect(page.get_by_role("heading", name="SEO audit command center")).to_be_visible()
            page.fill("#url", "https://example.com")
            page.click("#saveSiteBtn")
            page.get_by_role("button", name="Sites").click()
            expect(page.get_by_role("heading", name="example.com")).to_be_visible()
            page.get_by_role("button", name="Audit Desk").click()
            page.select_option("#moduleSelect", "full-audit")
            page.select_option("#scheduleFrequency", "weekly")
            page.click("#createScheduleBtn")
            expect(page.get_by_text("Recurring audit scheduled.")).to_be_visible()
            expect(page.locator("#scheduleList")).to_contain_text("weekly")
            page.get_by_role("button", name="Pause").click()
            page.click("#startBtn")
            expect(page.get_by_role("heading", name="Full SEO Audit")).to_be_visible()
            expect(page.locator("#summaryView").get_by_text("HTML and On-page Signals: Image alt text gaps", exact=True)).to_be_visible(timeout=12000)
            page.select_option("#moduleSelect", "auto-fix-generator")
            page.click("#startBtn")
            expect(page.get_by_role("heading", name="CMS Auto-Fix Code Generator")).to_be_visible(timeout=12000)
            expect(page.get_by_role("button", name="Copy Snippet").first).to_be_visible()
            page.get_by_role("button", name="Copy Snippet").first.click()
            expect(page.get_by_text("Copied to clipboard!")).to_be_visible()
            page.get_by_role("button", name="Reports").click()
            expect(page.get_by_role("heading", name="Executive report library")).to_be_visible()
            expect(page.get_by_text("Report Preview")).to_be_visible()
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("browser e2e passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
