"""Browser acceptance with real queued tasks and an isolated, gated Fake Worker."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from playwright.sync_api import Page, expect, sync_playwright
from scripts.verify_web import navigate_view

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5177")
    parser.add_argument("--api-port", type=int, default=8007)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".runtime/durable-web")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    fixture = args.output_dir / uuid4().hex
    fixture.mkdir()
    processes = []
    errors: list[str] = []
    checks: list[str] = []
    api_url = f"http://127.0.0.1:{args.api_port}"
    with (fixture / "processes.log").open("wb") as log:
        try:
            queue = f"browser-{fixture.name}"
            for role in ("api", "worker"):
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            str(ROOT / "tests/integration/durable_chat_host.py"),
                            role,
                            "--directory",
                            str(fixture),
                            "--queue",
                            queue,
                            "--port",
                            str(args.api_port),
                        ],
                        cwd=ROOT,
                        stdout=log,
                        stderr=log,
                    )
                )
            with httpx.Client(base_url=api_url, trust_env=False) as client:
                deadline = time.monotonic() + 40
                while True:
                    if any(process.poll() is not None for process in processes):
                        raise RuntimeError(f"Fixture exited; see {fixture / 'processes.log'}")
                    try:
                        if client.get("/health/ready").json()["chat_worker_ready"]:
                            break
                    except httpx.TransportError:
                        pass
                    if time.monotonic() > deadline:
                        raise TimeoutError("Fixture startup timed out")
                    time.sleep(0.1)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(
                        headless=True,
                        executable_path=str(args.executable) if args.executable else None,
                    )
                    try:
                        for width, height in ((1440, 1000), (390, 844)):
                            release = fixture / "release"
                            release.unlink(missing_ok=True)
                            user_id = 5_000_000_000 + uuid4().int % 1_000_000_000
                            headers = {"X-User-ID": str(user_id)}
                            context = browser.new_context(
                                viewport={"width": width, "height": height}
                            )
                            context.add_init_script(
                                "if (location.protocol.startsWith('http')) {"
                                f"sessionStorage.setItem('nutripilot.userId','{user_id}');"
                                "}"
                            )

                            def forward(route):  # type: ignore[no-untyped-def]
                                url = urlsplit(route.request.url)
                                route.continue_(
                                    url=api_url + url.path + ("?" + url.query if url.query else "")
                                )

                            context.route("**/api/v1/**", forward)
                            context.route("**/health/**", forward)
                            context.on(
                                "page",
                                lambda page: page.on(
                                    "pageerror", lambda error: errors.append(str(error))
                                ),
                            )
                            page = context.new_page()
                            page.goto(args.base_url)
                            page.wait_for_load_state("networkidle")
                            composer = page.get_by_role("textbox", name="发送给 NutriPilot 的消息")
                            runs: list[dict[str, Any]] = []
                            urls: list[str] = []

                            def send(target: Page, message: str) -> dict[str, Any]:
                                target.get_by_role("textbox", name="发送给 NutriPilot 的消息").fill(
                                    message
                                )
                                with target.expect_response(
                                    lambda response: (
                                        response.request.method == "POST"
                                        and response.url.endswith("/runs")
                                    )
                                ) as accepted:
                                    target.get_by_role(
                                        "button", name="发送消息", exact=True
                                    ).click()
                                assert accepted.value.status == 202
                                return cast("dict[str, Any]", accepted.value.json())

                            def state(
                                run: dict[str, Any], identity: dict[str, str] = headers
                            ) -> str:
                                return str(
                                    client.get(
                                        f"/api/v1/runs/{run['id']}", headers=identity
                                    ).json()["status"]
                                )

                            runs.append(send(page, f"推荐午餐，并发验收{width}-1"))
                            urls.append(page.url)
                            composer.fill("保留草稿")
                            for label in ("饮食偏好", "饮食计划", "执行记录", "质量评估", "对话"):
                                navigate_view(page, label)
                            expect(composer).to_have_value("保留草稿")
                            expect(page.get_by_role("button", name="停止生成")).to_be_visible()
                            for number in (2, 3, 4):
                                page.get_by_role("button", name="新对话", exact=True).click()
                                runs.append(send(page, f"推荐午餐，并发验收{width}-{number}"))
                                urls.append(page.url)
                            deadline = time.monotonic() + 10
                            while [state(run) for run in runs] != ["running"] * 3 + ["queued"]:
                                assert time.monotonic() < deadline
                                page.wait_for_timeout(100)
                            checks.append(f"{width}px: three concurrent tasks and a queued fourth")
                            page.reload()
                            expect(page.get_by_role("button", name="停止生成")).to_be_visible()
                            assert page.url == urls[3]
                            page.close()
                            page = context.new_page()
                            page.goto(urls[0])
                            expect(page.get_by_role("button", name="停止生成")).to_be_visible()
                            assert state(runs[0]) == "running"
                            checks.append(
                                f"{width}px: navigation, reload and reopening preserve tasks"
                            )
                            other = context.new_page()
                            other.goto(urls[1])
                            expect(other.get_by_role("button", name="停止生成")).to_be_enabled()
                            other.get_by_role("button", name="停止生成").click()
                            expect(other.get_by_role("button", name="停止生成")).not_to_be_visible(
                                timeout=10000
                            )
                            assert state(runs[1]) == "cancelled"
                            assert state(runs[0]) == "running"
                            checks.append(f"{width}px: stopping one task preserves the others")
                            context.set_offline(True)
                            release.touch()
                            deadline = time.monotonic() + 20
                            while any(
                                state(run) in ("queued", "running", "cancelling") for run in runs
                            ):
                                assert time.monotonic() < deadline
                                page.wait_for_timeout(100)
                            context.set_offline(False)
                            expect(page.locator(".meal-result").first).to_be_visible(timeout=20000)
                            expect(page.get_by_role("button", name="停止生成")).not_to_be_visible()
                            page.reload()
                            expect(page.locator(".message-user")).to_have_count(1)
                            expect(page.locator(".message-assistant")).to_have_count(1)
                            checks.append(
                                f"{width}px: offline completion reconnects with one saved reply"
                            )
                            other.goto(urls[0])
                            expect(other.locator(".message-user")).to_have_count(1)
                            send(page, "糖拌西红柿有多少热量")
                            expect(other.locator(".message-user")).to_have_count(2, timeout=15000)
                            expect(other.locator(".message-assistant")).to_have_count(
                                2, timeout=15000
                            )
                            expect(other.get_by_role("button", name="停止生成")).not_to_be_visible(
                                timeout=15000
                            )
                            checks.append(
                                f"{width}px: another tab observes new turns automatically"
                            )
                            assert page.evaluate(
                                "document.documentElement.scrollWidth <= window.innerWidth"
                            )
                            page.screenshot(
                                path=str(args.output_dir / f"durable-{width}.png"), full_page=True
                            )
                            context.close()
                    finally:
                        browser.close()
            assert not errors, errors
            result = {
                "checks": checks,
                "page_errors": errors,
                "model": "gated fake",
                "storage": "PostgreSQL",
                "stream": "real SSE",
            }
            (args.output_dir / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(10)


if __name__ == "__main__":
    main()
