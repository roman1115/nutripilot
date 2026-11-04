"""Repeatable browser regression against an already-running local stack."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import Page, Request, Route, expect, sync_playwright


def verify_chat_navigation(page: Page) -> list[str]:
    """Hold the stream open so navigation cannot pass by finishing too quickly."""
    streams: list[Route] = []
    runs: list[dict[str, str]] = []
    cancellations: list[Request] = []
    navigation = page.locator(
        ".mobile-bottom-nav"
        if page.viewport_size and page.viewport_size["width"] < 1100
        else ".sidebar"
    )
    composer = page.get_by_role("textbox", name="发送给 NutriPilot 的消息")

    def hold_stream(route: Route) -> None:
        streams.append(route)

    def submit_run(route: Route) -> None:
        thread_id = route.request.url.split("/threads/")[1].split("/")[0]
        run = {
            "id": uuid4().hex,
            "thread_id": thread_id,
            "turn_id": uuid4().hex,
            "trace_id": "",
            "status": "queued",
            "kind": "chat",
        }
        runs.append(run)
        route.fulfill(status=202, json=run, headers={"Access-Control-Allow-Origin": "*"})

    def cancel_run(route: Route) -> None:
        route.fulfill(
            status=200, json={"status": "cancelling"}, headers={"Access-Control-Allow-Origin": "*"}
        )
        completed = {
            "thread_id": runs[-1]["thread_id"],
            "run_status": "cancelled",
            "final_response": {"message": "已停止本轮请求。"},
        }
        streams[-1].fulfill(
            status=200,
            headers={"Content-Type": "text/event-stream", "Access-Control-Allow-Origin": "*"},
            body=f"id: 1\nevent: completed\ndata: {json.dumps(completed, ensure_ascii=False)}\n\n",
        )

    def record_cancellation(request: Request) -> None:
        if request.url.endswith("/cancel"):
            cancellations.append(request)

    page.route("**/api/v1/threads/*/runs", submit_run)
    page.route("**/api/v1/runs/*/events?after=*", hold_stream)
    page.route("**/api/v1/runs/*/cancel", cancel_run)
    page.on("request", record_cancellation)
    try:
        navigation.get_by_role("button", name="对话", exact=True).click()
        page.get_by_role("button", name="新对话", exact=True).click()
        expect(composer).to_be_enabled()
        composer.fill("页面切换回归")
        page.get_by_role("button", name="发送消息", exact=True).click()
        expect(page.get_by_role("button", name="停止生成")).to_be_visible()
        composer.fill("保留未发送的草稿")
        for view, label in (
            ("profile", "饮食偏好"),
            ("plans", "饮食计划"),
            ("traces", "执行记录"),
            ("evaluations", "质量评估"),
        ):
            navigation.get_by_role("button", name=label, exact=True).click()
            expect(page.locator(f".main-content.view-{view}")).to_be_visible()
            expect(page.locator(".chat-layout")).not_to_be_visible()
        navigation.get_by_role("button", name="对话", exact=True).click()
        expect(page.get_by_role("button", name="停止生成")).to_be_visible()
        expect(composer).to_have_value("保留未发送的草稿")
        expect(page.locator(".message-user")).to_have_count(1)
        assert len(streams) == 1
        assert not cancellations
        navigation.get_by_role("button", name="饮食偏好", exact=True).click()
        thread_id = runs[0]["thread_id"]
        completed = {
            "thread_id": thread_id,
            "phase": "COMPLETED",
            "final_response": {"message": "切页后仍然完成的回答"},
        }
        streams[0].fulfill(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Access-Control-Allow-Origin": "*",
            },
            body=f"id: 1\nevent: completed\ndata: {json.dumps(completed, ensure_ascii=False)}\n\n",
        )
        expect(page.locator(".message-assistant")).to_contain_text("切页后仍然完成的回答")
        expect(page.locator(".chat-layout")).not_to_be_visible()
        navigation.get_by_role("button", name="对话", exact=True).click()
        expect(page.get_by_text("切页后仍然完成的回答", exact=True)).to_be_visible()
        expect(page.get_by_role("button", name="停止生成")).not_to_be_visible()
        expect(composer).to_have_value("保留未发送的草稿")
        assert not cancellations
        composer.fill("验证主动停止")
        page.get_by_role("button", name="发送消息", exact=True).click()
        expect(page.get_by_role("button", name="停止生成")).to_be_visible()
        page.get_by_role("button", name="停止生成").click()
        expect(page.get_by_role("button", name="停止生成")).not_to_be_visible()
        expect(page.get_by_text("已停止本轮请求。", exact=True)).to_be_visible()
        assert len(cancellations) == 1
        assert len(streams) == 2
        return [
            "running chat and draft survive navigation through all four other views",
            "hidden chat finishes and displays its answer on return without cancellation",
            "explicit stop still calls cancellation and stops the stream",
        ]
    finally:
        page.unroute("**/api/v1/threads/*/runs", submit_run)
        page.unroute("**/api/v1/runs/*/events?after=*", hold_stream)
        page.unroute("**/api/v1/runs/*/cancel", cancel_run)
        page.remove_listener("request", record_cancellation)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(".runtime/web-regression"))
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--navigation-only", action="store_true")
    parser.add_argument("--user-id", type=int, default=910024)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    checks: list[str] = []
    run_tag = f"browser-{uuid4().hex[:8]}"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(args.executable) if args.executable else None,
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
            if args.navigation_only:
                for width, height in ((1440, 1000), (390, 844)):
                    page.set_viewport_size({"width": width, "height": height})
                    checks.extend(f"{width}px: {check}" for check in verify_chat_navigation(page))
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )
                    page.screenshot(
                        path=str(args.output_dir / f"navigation-{width}.png"), full_page=True
                    )
                assert not errors, errors
                result = {"checks": checks, "page_errors": errors, "stream": "mocked"}
                (args.output_dir / "result.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
            page.get_by_role("textbox", name="发送给 NutriPilot 的消息").fill(
                f"推荐一份清淡、高蛋白、不超过600千卡的午餐（验收编号 {run_tag}）"
            )
            page.get_by_role("button", name="发送消息", exact=True).click()
            expect(page.locator(".meal-result").first).to_be_visible(
                timeout=120000 if args.real else 15000
            )
            expect(page.locator(".meal-result p")).to_have_count(0)
            expect(page.locator(".meal-result .tag-row")).to_have_count(0)
            expect(page.locator(".meal-result-basis").first).to_have_text("整道菜 · 营养估算")
            for nutrient in ("蛋白质", "碳水", "脂肪"):
                expect(page.locator(".meal-result .nutrition-row").first).to_contain_text(nutrient)
            expect(page.get_by_text("餐食记录标注的过敏原", exact=False).first).to_be_visible()
            page.screenshot(path=str(args.output_dir / "recommendation.png"), full_page=True)
            checks.append("recommendation includes authoritative nutrition/allergen explanations")
            checks.append("cards contain objective nutrients without recommendation reasons")
            page.get_by_role("button", name="查看执行记录", exact=True).first.click()
            expect(page.get_by_role("heading", name="调用链回放")).to_be_visible()
            checks.append("trace can be opened")
            page.locator(".sidebar").get_by_role("button", name="对话", exact=True).click()
            page.get_by_role("button", name="新对话", exact=True).click()
            page.get_by_role("textbox", name="发送给 NutriPilot 的消息").fill(
                "糖拌西红柿有多少热量？"
            )
            page.get_by_role("button", name="发送消息", exact=True).click()
            expect(page.get_by_text("糖拌西红柿：", exact=False)).to_be_visible(
                timeout=120000 if args.real else 15000
            )
            checks.append("source recipe nutrition resolves in the browser")
            page.get_by_role("textbox", name="发送给 NutriPilot 的消息").fill(
                "糖拌西红柿有多少蛋白质？"
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
            expect(page.locator(".confirmation-meals")).to_contain_text("整菜营养合计")
            expect(page.locator(".confirmation-meals")).not_to_contain_text("匹配容差")
            original_names = page.locator(".plan-preview li strong").all_text_contents()
            page.get_by_role("button", name="换一批菜品", exact=True).click()
            expect(page.locator(".plan-preview summary")).to_have_text(
                "餐单明细（3 餐）", timeout=120000 if args.real else 15000
            )
            expect(page.locator(".plan-preview li strong").first).not_to_have_text(
                original_names[0], timeout=120000 if args.real else 15000
            )
            replacement_names = page.locator(".plan-preview li strong").all_text_contents()
            assert not set(original_names) & set(replacement_names)
            checks.append("pending plan can replace recipes before confirmation")
            page.locator(".plan-preview summary").click()
            expect(page.locator(".plan-preview li")).to_have_count(3)
            expect(page.locator(".plan-preview li").first).to_contain_text("蛋白质")
            page.screenshot(path=str(args.output_dir / "plan-preview.png"), full_page=True)
            for width, height in ((390, 844), (320, 740)):
                page.set_viewport_size({"width": width, "height": height})
                assert page.locator(".confirmation-panel").evaluate(
                    "node => node.scrollWidth <= node.clientWidth"
                )
                assert page.locator(".confirmation-actions .button").evaluate_all(
                    "nodes => nodes.every(node => node.scrollWidth <= node.clientWidth)"
                )
                page.screenshot(
                    path=str(args.output_dir / f"plan-preview-{width}.png"),
                    full_page=True,
                    animations="disabled",
                )
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.get_by_role("button", name="确认执行", exact=True).click()
            expect(page.locator(".confirmation-panel")).not_to_be_visible(
                timeout=120000 if args.real else 15000
            )
            checks.append(
                "plan preview shows whole recipe ranges without personal target judgments"
            )
            page.locator(".sidebar").get_by_role("button", name="饮食计划", exact=True).click()
            expect(page.locator(".plan-meal-row")).to_have_count(3, timeout=10000)
            saved_names = [
                name.replace("菜谱来源", "").strip()
                for name in page.locator(".plan-meal-row > strong").all_text_contents()
            ]
            assert saved_names == replacement_names
            expect(page.locator(".plan-day-heading")).to_contain_text("蛋白质")
            expect(page.locator(".plan-calories")).to_contain_text("所选整菜合计")
            assert page.locator(".notice-warning > div").evaluate_all(
                "nodes => nodes.every(node => node.getBoundingClientRect().width > 200)"
            )
            page.screenshot(path=str(args.output_dir / "saved-plan.png"), full_page=True)
            checks.append("approved plan persists and lists calories and protein")
            for width, height in ((1440, 1000), (390, 844), (320, 740)):
                page.set_viewport_size({"width": width, "height": height})
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                assert page.locator(".notice-warning > div").evaluate_all(
                    "nodes => nodes.every(node => node.getBoundingClientRect().width > 150)"
                )
                page.screenshot(
                    path=str(args.output_dir / f"saved-plan-{width}.png"),
                    full_page=True,
                    animations="disabled",
                )
            checks.append("saved plan ranges, sources and notices fit desktop and mobile layouts")
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.locator(".sidebar").get_by_role("button", name="对话", exact=True).click()
            close_activity = page.get_by_role("button", name="关闭执行过程", exact=True)
            if close_activity.is_visible():
                close_activity.click()
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
