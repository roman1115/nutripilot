"""Exercise natural answers and follow-ups through the durable HTTP run API."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx


def verify(api_url: str, output: Path) -> dict[str, Any]:
    user_id = 8_000_000_000 + uuid4().int % 1_000_000_000
    token = os.getenv("NUTRIPILOT_ACCESS_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {"X-User-ID": str(user_id)}
    report: dict[str, Any] = {"turns": [], "failures": [], "user_id": user_id}
    output.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=api_url, headers=headers, timeout=150, trust_env=False) as client:
        health = client.get("/health/ready")
        health.raise_for_status()
        report["runtime"] = health.json()
        assert report["runtime"]["chat_worker_ready"], report["runtime"]

        def send(thread: str, message: str, intent: str) -> dict[str, Any]:
            started = time.monotonic()
            accepted = client.post(f"/api/v1/threads/{thread}/runs", json={
                "message": message, "request_id": str(uuid4()),
            })
            accepted.raise_for_status()
            run_id = accepted.json()["id"]
            deadline = time.monotonic() + 150
            while True:
                fetched = client.get(f"/api/v1/runs/{run_id}")
                fetched.raise_for_status()
                run = fetched.json()
                if run["status"] not in {"queued", "running", "recovering", "cancelling"}:
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError(run_id)
                time.sleep(0.3)
            state = run.get("result") or {}
            trace = client.get(f"/api/v1/traces/{state['trace_id']}")
            trace.raise_for_status()
            events = trace.json()["events"]
            model_events = [
                event for event in events if event["event_type"] == "model.completed"
            ]
            checks = {
                "completed": run["status"] == "completed" and state.get("phase") == "COMPLETED",
                "intent": state.get("intent") == intent,
                "facts_checked": any(
                    event["event_type"] == "response.facts_checked"
                    and event.get("payload", {}).get("passed") is True
                    for event in events
                ),
                "agent_prose_published": any(
                    event["event_type"] == "safety.output_checked"
                    and event.get("payload", {}).get("approved_prose_applied") is True
                    for event in events
                ),
                "has_response": bool((state.get("final_response") or {}).get("message")),
                "no_fallback": not any("fallback" in event["event_type"] for event in events),
                "real_model": bool(model_events) and all(
                    event.get("payload", {}).get("model") == report["runtime"]["model_name"]
                    and not event.get("payload", {}).get("used_fallback_model")
                    and event.get("input_tokens", 0) > 0
                    and event.get("output_tokens", 0) > 0
                    for event in model_events
                ),
            }
            if intent != "CONVERSATION":
                checks["boundary_checked"] = any(
                    event["event_type"] == "response.boundary_checked"
                    and event.get("payload", {}).get("passed") is True
                    for event in events
                )
            if intent in {"FOOD_QUESTION", "CONVERSATION", "RECIPE_QUERY"}:
                checks["no_new_cards"] = not state["final_response"]["recommendations"]
            if intent == "CONVERSATION":
                checks["no_tools"] = not any(
                    event["event_type"] in {"tool.completed", "tool.failed"}
                    for event in events
                )
                checks["no_citations"] = not state["final_response"]["citations"]
            item = {
                "message": message, "thread_id": thread, "run_id": run_id,
                "seconds": round(time.monotonic() - started, 2),
                "checks": checks, "state": state, "trace": events,
            }
            report["turns"].append(item)
            for check, passed in checks.items():
                if not passed:
                    report["failures"].append({"message": message, "check": check})
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({
                "message": message, "intent": state.get("intent"), "checks": checks,
                "seconds": item["seconds"], "answer": state["final_response"]["message"],
            }, ensure_ascii=False), flush=True)
            return state

        lunch_thread = f"natural-lunch-{uuid4().hex}"
        lunch = send(lunch_thread, "推荐一份高蛋白的午餐", "MEAL_RECOMMENDATION")
        lunch_cards = lunch["final_response"]["recommendations"]
        if len(lunch_cards) != 1:
            report["failures"].append({"message": "高蛋白午餐", "check": "one_choice"})
        if lunch_cards:
            original = lunch_cards[0]
            replacement = send(
                lunch_thread, f"{original['name']}脂肪太高了，换一个", "MEAL_ADJUSTMENT",
            )
            new_cards = replacement["final_response"]["recommendations"]
            for check, passed in {
                "one_replacement": len(new_cards) == 1,
                "dish_not_excluded_ingredient": original["name"] not in replacement[
                    "slots"
                ].get("excluded_ingredients", []),
                "lower_fat": bool(new_cards) and all(
                    card["meal_id"] != original["meal_id"]
                    and card["fat_g"] is not None and original["fat_g"] is not None
                    and card["fat_g"] < original["fat_g"] for card in new_cards
                ),
            }.items():
                if not passed:
                    report["failures"].append({"message": "脂肪太高换一个", "check": check})
        ingredient_thread = f"natural-ingredient-{uuid4().hex}"
        send(ingredient_thread, "想吃鸡胸肉", "MEAL_RECOMMENDATION")
        send(ingredient_thread, "为什么推荐第一道？", "FOOD_QUESTION")
        send(ingredient_thread, "第一道怎么做？", "RECIPE_QUERY")
        send(
            ingredient_thread, "不要推荐菜，我只想知道鸡胸肉为什么容易做柴。",
            "FOOD_QUESTION",
        )
        send(ingredient_thread, "已经做柴了，现在怎么补救？", "FOOD_QUESTION")
        send(ingredient_thread, "好的，谢谢", "CONVERSATION")
        send(
            f"natural-ambiguous-{uuid4().hex}", "你说的那道菜为什么适合我？",
            "FOOD_QUESTION",
        )
        send(
            f"natural-empty-{uuid4().hex}",
            "想吃鸡胸肉，整道菜不超过1千卡", "MEAL_RECOMMENDATION",
        )
        comparison_thread = f"natural-comparison-{uuid4().hex}"
        first = send(comparison_thread, "推荐几道午餐菜品", "MEAL_RECOMMENDATION")
        cards = first["final_response"]["recommendations"]
        if len(cards) < 2:
            report["failures"].append({"message": "午餐", "check": "at_least_two_cards"})
        else:
            explained = send(comparison_thread, "为什么推荐第二个？", "FOOD_QUESTION")
            if cards[1]["name"] not in explained["final_response"]["message"]:
                report["failures"].append({"message": "第二个", "check": "correct_reference"})
            send(comparison_thread, "前两个有什么区别？", "FOOD_QUESTION")
            recipe = send(comparison_thread, "第二道怎么做？", "RECIPE_QUERY")
            if recipe["slots"].get("food_name") != cards[1]["name"]:
                report["failures"].append({"message": "第二道做法", "check": "correct_reference"})
        source_checks = 0
        for turn in report["turns"]:
            for card in turn["state"]["final_response"]["recommendations"]:
                source = client.get(f"/api/v1/recipes/{card['meal_id']}")
                source.raise_for_status()
                record = source.json()
                assert record["name"] == card["name"]
                nutrition = record["nutrition"]
                assert card["calories_kcal"] == nutrition["calories_kcal"], card
                assert card["protein_g"] == nutrition["protein_g"], card
                assert card["carbohydrates_g"] == nutrition["carbohydrates_g"], card
                assert card["fat_g"] == nutrition["fat_g"], card
                assert card["nutrient_ranges"] == nutrition["nutrient_ranges"], card
                assert nutrition["source_status"] == "calculated_estimate", nutrition
                assert any(
                    citation["source_id"] in {
                        f"recipe:{card['meal_id']}", f"meal:{card['meal_id']}",
                    }
                    and citation["uri"] == record["source_uri"]
                    for citation in turn["state"]["final_response"]["citations"]
                ), card
                source_checks += 1
        report["card_source_checks"] = source_checks
        report["model_calls"] = sum(
            event["event_type"] == "model.completed"
            for turn in report["turns"] for event in turn["trace"]
        )
        report["input_tokens"] = sum(
            event.get("input_tokens", 0)
            for turn in report["turns"] for event in turn["trace"]
        )
        report["output_tokens"] = sum(
            event.get("output_tokens", 0)
            for turn in report["turns"] for event in turn["trace"]
        )
        report["passed"] = not report["failures"]
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return report


def verify_browser(
    report: dict[str, Any], web_url: str, output: Path, executable: str | None = None,
) -> None:
    from playwright.sync_api import expect, sync_playwright
    from scripts.verify_web import navigate_view

    user_id = report["user_id"]
    last = report["turns"][-1]
    screenshots = output.with_suffix("")
    screenshots.mkdir(exist_ok=True)
    errors: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, executable_path=executable or playwright.chromium.executable_path,
        )
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.add_init_script(
                f"sessionStorage.setItem('nutripilot.userId','{user_id}');"
            )
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{web_url}/?thread={last['thread_id']}#chat")
            page.wait_for_load_state("networkidle")
            response = page.locator(".message-assistant").last
            expected = last["state"]["final_response"]["message"]
            expect(response.locator(".message-content")).to_have_text(expected, timeout=20000)
            assert response.locator(".message-content p").count() > 0
            assert response.locator(".meal-result").count() == 0
            navigate_view(page, "饮食偏好")
            navigate_view(page, "对话")
            expect(response.locator(".message-content")).to_have_text(expected)
            page.reload()
            page.wait_for_load_state("networkidle")
            expect(response.locator(".message-content")).to_have_text(expected, timeout=20000)
            expect(page.locator(".meal-result").first).to_be_attached()
            expect(page.locator(".meal-result p")).to_have_count(0)
            expect(page.locator(".meal-result .tag-row")).to_have_count(0)
            expect(page.locator(".meal-result-basis").first).to_have_text("整道菜 · 营养估算")
            comparison = page.locator(".message-assistant").nth(-2)
            expect(comparison.locator(".message-content")).to_have_text(
                report["turns"][-2]["state"]["final_response"]["message"],
            )
            assert comparison.locator(".message-content p").count() > 0
            for width, height in ((1440, 1000), (390, 844), (320, 740)):
                page.set_viewport_size({"width": width, "height": height})
                if width < 960:
                    expect(page.get_by_role("navigation", name="主导航")).not_to_be_visible()
                for name, message in (("comparison", comparison), ("recipe", response)):
                    message.scroll_into_view_if_needed()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(screenshots / f"{name}-{width}.png"))
            page.set_viewport_size({"width": 1440, "height": 1000})
            composer = page.get_by_role("textbox", name="发送给 NutriPilot 的消息")
            composer.fill("好的，谢谢")
            page.get_by_role("button", name="发送消息", exact=True).click()
            expect(page.locator(".message-user").last).to_contain_text("好的，谢谢")
            expect(page.get_by_role("button", name="发送消息", exact=True)).to_be_visible(
                timeout=120000,
            )
            final = page.locator(".message-assistant").last
            expect(final.locator(".stream-placeholder")).to_have_count(0)
            assert final.locator(".meal-result").count() == 0
            assert final.locator(".message-content").inner_text().strip()
            thanks = final.locator(".message-content").text_content()
            final.scroll_into_view_if_needed()
            page.screenshot(path=str(screenshots / "thanks-desktop.png"))
            page.reload()
            page.wait_for_load_state("networkidle")
            expect(page.locator(".message-assistant").last.locator(".message-content")).to_have_text(
                thanks or "", timeout=20000,
            )
            assert not errors, errors
            report["browser"] = {
                "passed": True, "errors": errors,
                "checks": ["restore", "paragraphs", "navigation", "reload", "send_thanks",
                           "no_unrequested_cards", "cards_facts_only", "desktop", "mobile_390",
                           "mobile_320"],
                "screenshots": str(screenshots),
            }
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8006")
    parser.add_argument("--output", type=Path, default=Path(".runtime/natural-answers.json"))
    parser.add_argument("--web-url")
    parser.add_argument("--browser-only", action="store_true")
    parser.add_argument("--executable")
    args = parser.parse_args()
    report = (
        json.loads(args.output.read_text(encoding="utf-8")) if args.browser_only
        else verify(args.api_url, args.output)
    )
    if args.web_url:
        verify_browser(report, args.web_url, args.output, args.executable)
    print(json.dumps({
        key: report[key] for key in (
            "passed", "failures", "model_calls", "input_tokens", "output_tokens",
            "card_source_checks",
        )
    }, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
