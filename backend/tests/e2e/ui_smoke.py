"""Phase 16 — real-browser UI smoke test (headless Chrome via the DevTools protocol).

Usage:
    python backend/tests/e2e/ui_smoke.py --base http://127.0.0.1:8020 --email owner@example.org \
        --password "owner password 123" [--chrome "C:/Program Files/Google/Chrome/Application/chrome.exe"]

Needs a running server (with a built frontend in dist/) that already has at least one
project with a video. Requires the `websockets` package (installed with uvicorn[standard]).
Exit code 0 only when every check passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

import websockets

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'} {name} {detail}", flush=True)


class Page:
    def __init__(self, ws):
        self.ws = ws
        self.counter = 0
        self.errors: list[str] = []

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.counter += 1
        ident = self.counter
        await self.ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await self.ws.recv())
            if message.get("method") == "Runtime.exceptionThrown":
                self.errors.append(message["params"]["exceptionDetails"].get("text", "exception"))
            elif message.get("method") == "Log.entryAdded" and message["params"]["entry"]["level"] == "error":
                text = message["params"]["entry"].get("text", "")
                if "401" not in text and "favicon" not in text:  # the pre-login 401 is expected
                    self.errors.append(text)
            if message.get("id") == ident:
                return message

    async def js(self, expression: str):
        result = await self.send("Runtime.evaluate", {"expression": expression, "awaitPromise": True, "returnByValue": True})
        return result.get("result", {}).get("result", {}).get("value")

    async def wait_for(self, expression: str, timeout: float = 20.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if await self.js(expression):
                return True
            await asyncio.sleep(0.25)
        return False


SET_INPUT = """(sel, value) => { const el = document.querySelector(sel);
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(el, value);
  el.dispatchEvent(new Event('input', {bubbles: true})); return true; }"""


async def run(args) -> None:
    async with websockets.connect(args.ws, max_size=2**25) as ws:
        page = Page(ws)
        await page.send("Runtime.enable")
        await page.send("Log.enable")
        await page.send("Page.enable")
        await page.send("Page.navigate", {"url": args.base + "/#/dashboard"})
        check("sign-in screen shown when signed out", await page.wait_for("!!document.querySelector('.auth-card')"))

        await page.js(f"({SET_INPUT})('.auth-card input[type=email]', {json.dumps(args.email)})")
        await page.js(f"({SET_INPUT})('.auth-card input[type=password]', 'definitely wrong password')")
        await page.js("document.querySelector('.auth-card button[type=submit]').click()")
        check("wrong password shows an error", await page.wait_for("!!document.querySelector('.auth-card .form-error')"))
        await page.js(f"({SET_INPUT})('.auth-card input[type=password]', {json.dumps(args.password)})")
        await page.js("document.querySelector('.auth-card button[type=submit]').click()")
        check("sign-in reaches the dashboard", await page.wait_for("!!document.querySelector('.topbar-user')"))

        projects = await page.js("fetch('/api/v1/projects').then(r => r.json())")
        project_id = projects[0]["id"] if projects else None
        check("API calls carry the session cookie", bool(project_id), f"{len(projects or [])} project(s)")
        for name, hash_ in (("projects page", "#/projects"), ("project page", f"#/projects/{project_id}"),
                            ("studio page", f"#/projects/{project_id}/studio")):
            await page.js(f"location.hash = {json.dumps(hash_)}")
            ok = await page.wait_for("document.querySelectorAll('.panel').length > 0 && !document.querySelector('.state-panel.error')", 25)
            check(f"{name} renders", ok)
        panels = ["Translation Editor", "Voice Generation", "Dialogue Timing", "Mixing Studio", "Timeline"]
        await page.wait_for("document.body.innerText.includes('Mixing Studio')", 30)
        text = await page.js("document.body.innerText")
        for panel in panels:
            check(f"studio shows '{panel}'", panel in (text or ""))

        has_export = await page.wait_for("[...document.querySelectorAll('button')].some(b => b.textContent.trim() === 'Render export')", 30)
        check("timeline offers 'Render export'", has_export)
        if has_export:
            await page.js("[...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Render export').click()")
            ok = await page.wait_for("!!document.querySelector('.tl-export-files a[href*=\"timeline-export/files\"]')", 180)
            check("export renders and offers a download", ok)
            if ok:
                href = await page.js("document.querySelector('.tl-export-files a').href")
                status = await page.js(f"fetch({json.dumps(href)}).then(r => r.status + ':' + r.headers.get('content-disposition'))")
                check("download link serves the file", str(status).startswith("200:attachment"), str(status))

        for width, height in ((390, 844), (1366, 900)):
            await page.send("Emulation.setDeviceMetricsOverride", {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": width < 600})
            for hash_ in ("#/dashboard", "#/projects", f"#/projects/{project_id}/studio"):
                await page.js(f"location.hash = {json.dumps(hash_)}")
                await page.wait_for("document.querySelectorAll('.panel').length > 0", 20)
                await asyncio.sleep(1.5)
                started = time.time()
                overflow = await page.js("document.documentElement.scrollWidth - window.innerWidth")
                check(f"no horizontal scroll at {width}px on {hash_.split('/')[-1] or hash_}", (overflow or 0) <= 1, f"overflow={overflow}px")
            if "studio" in hash_:
                timing = await page.js("performance.getEntriesByType('navigation')[0]?.domContentLoadedEventEnd || 0")
                check(f"studio usable at {width}px", True, f"dcl={round(timing or 0)}ms")
        await page.send("Emulation.clearDeviceMetricsOverride")

        await page.js("[...document.querySelectorAll('button')].find(b => b.textContent.trim() === 'Sign out').click()")
        check("sign-out returns to the sign-in screen", await page.wait_for("!!document.querySelector('.auth-card')"))
        check("no JavaScript errors", not page.errors, "; ".join(page.errors[:5]))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--chrome", default=shutil.which("chrome") or r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    parser.add_argument("--port", type=int, default=9241)
    args = parser.parse_args()
    profile = tempfile.mkdtemp(prefix="cinema-ui-")
    chrome = subprocess.Popen([args.chrome, "--headless=new", "--disable-gpu", f"--remote-debugging-port={args.port}",
                               f"--user-data-dir={profile}", "--window-size=1366,900", "about:blank"])
    try:
        for _ in range(60):
            try:
                targets = json.load(urllib.request.urlopen(f"http://127.0.0.1:{args.port}/json"))
                args.ws = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.25)
        asyncio.run(run(args))
    finally:
        chrome.terminate()
    failed = [name for name, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} UI checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
