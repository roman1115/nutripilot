"""Repeatable browser regression against an already-running local stack."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(".runtime/web-regression"))
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--user-id", type=int, default=910024)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    checks: list[str] = []
    run_tag = f"browser-{uuid4().hex[:8]}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            **({"executable_path": str(args.executable)} if args.executable else {}),
        )
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        if not os.getenv("NUTRIPILOT_ACCESS_TOKEN"):
            page.add_init_script(f"sessionStorage.setItem('nutripilot.userId', '{args.user_id}');")
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            page.goto(args.base_url)
            page.wait_for_load_state("networkidle")
            expect(page.get_by_text("服务已就绪", exact=True)).to_be_visible()
            expect(page.locator("#chat-activity")).not_to_be_visible()
            expect(page.get_by_role("region", name="历史会话列表")).to_be_visible()
            checks.append("desktop history is visible and execution details start collapsed")
            token = os.getenv("NUTRIPILOT_ACCESS_TOKEN")
            if token:
                page.screenshot(path=str(args.output_dir / "access.png"), full_page=True)
                page.get_by_label("访问令牌").fill(token)
                page.get_by_role("button", name="连接", exact=True).click()
                expect(
                    page.get_by_role(
                        "textbox",
                        name="发送给 NutriPilot 的消息",
                    )
                ).to_be_visible()
                checks.append("authenticated browser identity accepted")
            page.get_by_role("textbox", name="发送给 NutriPilot 的消息").fill(
                f"推荐一份清淡、高蛋白、不超过600千卡的午餐（验收编号 {run_tag}）"
            )
            page.get_by_role("button", name="发送消息", exact=True).click()
            expect(page.locator(".meal-result").first).to_be_visible(
                timeout=120000 if args.real else 15000
            )
            expect(page.get_by_text("餐食记录标注的过敏原", exact=False).first).to_be_visible()
            page.screenshot(path=str(args.output_dir / "recommendation.png"), full_page=True)
            checks.append("recommendation includes authoritative nutrition/allergen explanations")
            page.get_by_role("button", name="查看执行记录", exact=True).first.click()
            expect(page.get_by_role("heading", name="调用链回放")).to_be_visible()
            checks.append("trace can be opened")
            page.locator(".sidebar").get_by_role("button", name="对话", exact=True).click()
            page.get_by_role("button", name="新对话", exact=True).click()
            page.get_by_role("textbox", name="发送给 NutriPilot 的消息").fill(
                "鸡胸肉藜麦能量碗有多少热量？"
            )
            page.get_by_role("button", name="发送消息", exact=True).click()
            expect(page.get_by_text("鸡胸藜麦能量碗：", exact=False)).to_be_visible(
                timeout=120000 if args.real else 15000
            )
            checks.append("nutrition alias resolves in the browser")
            page.get_by_role("textbox", name="发送给 NutriPilot 的消息").fill(
                "鸡胸藜麦能量碗有多少蛋白质？"
            )
            page.get_by_role("button", name="发送消息", exact=True).click()
            expect(page.locator(".message-assistant")).to_have_count(2, timeout=120000)
            expect(page.get_by_role("button", name="停止生成")).not_to_be_visible(
                timeout=120000 if args.real else 15000
            )
            page.reload()
            page.wait_for_load_state("networkidle")
            expect(page.locator(".message-user")).to_have_count(2)
            expect(page.locator(".message-assistant")).to_have_count(2)
            checks.append("all messages restore after reload")
            history_toggle = page.get_by_role("button", name="历史会话", exact=True)
            if history_toggle.get_attribute("aria-expanded") != "true":
                history_toggle.click()
            page.get_by_label("搜索会话").fill(run_tag)
            history = page.get_by_role("region", name="历史会话列表")
            expect(history.locator(".conversation-item")).to_have_count(1)
            history.locator(".conversation-item").click()
            expect(page.locator(".meal-result").first).to_be_visible()
            checks.append("history search and conversation switching restore recommendations")
            page.get_by_role("button", name="新对话", exact=True).click()
            page.get_by_role("textbox", name="发送给 NutriPilot 的消息").fill(
                "制定一日三餐饮食计划，每日1500千卡，每天蛋白质至少80克。没有口味菜系和食材限制。"
            )
            page.get_by_role("button", name="发送消息", exact=True).click()
            expect(page.locator(".plan-preview summary")).to_have_text(
                "餐单明细（3 餐）", timeout=120000 if args.real else 15000
            )
            expect(page.locator(".confirmation-meals")).to_contain_text("1500")
            page.locator(".plan-preview summary").click()
            expect(page.locator(".plan-preview li")).to_have_count(3)
            expect(page.locator(".plan-preview li").first).to_contain_text("蛋白质")
            page.screenshot(path=str(args.output_dir / "plan-preview.png"), full_page=True)
            page.get_by_role("button", name="确认执行", exact=True).click()
            expect(page.locator(".confirmation-panel")).not_to_be_visible(
                timeout=120000 if args.real else 15000
            )
            checks.append(
                "plan preview shows authoritative names and daily nutrition before approval"
            )
            page.locator(".sidebar").get_by_role("button", name="饮食计划", exact=True).click()
            expect(page.locator(".plan-meal-row")).to_have_count(3)
            expect(page.locator(".plan-day-heading")).to_contain_text("蛋白质")
            page.screenshot(path=str(args.output_dir / "saved-plan.png"), full_page=True)
            checks.append("approved plan persists and lists calories and protein")
            page.locator(".sidebar").get_by_role("button", name="对话", exact=True).click()
            page.get_by_role("button", name="新对话", exact=True).click()
            page.screenshot(path=str(args.output_dir / "welcome.png"), full_page=True)
            for size in ((1440, 1000), (1024, 768), (768, 1024), (390, 844), (320, 740)):
                page.set_viewport_size({"width": size[0], "height": size[1]})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert page.locator(".prompt-suggestions img").evaluate_all(
                    "images => images.every(image => image.complete && image.naturalWidth > 0)"
                )
                page.screenshot(
                    path=str(args.output_dir / f"welcome-{size[0]}.png"),
                    full_page=True,
                    animations="disabled",
                )
            checks.append("welcome images load and five responsive widths fit without overflow")
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.locator(".sidebar")).not_to_be_in_viewport()
            expect(page.get_by_role("textbox", name="发送给 NutriPilot 的消息")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            page.screenshot(
                path=str(args.output_dir / "mobile.png"), full_page=True, animations="disabled"
            )
            checks.append("mobile layout fits without horizontal overflow")
            assert not errors, errors
            result = {"checks": checks, "page_errors": errors}
            (args.output_dir / "result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
