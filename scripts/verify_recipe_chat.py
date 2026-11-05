"""Verify cooking follow-ups through the running browser, Worker and MCP service."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright

from nutripilot.safety.dietary import match_ingredient, meets_ingredient_requirements


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5178")
    parser.add_argument("--api-url", default="http://127.0.0.1:8006")
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(".runtime/recipe-chat"))
    parser.add_argument("--scenario", choices=("recipe", "ingredients"), default="recipe")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    thread = f"recipe-browser-{uuid4().hex}"
    user_id = 6_000_000_000 + uuid4().int % 1_000_000_000
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    with httpx.Client(
        base_url=args.api_url, headers={"X-User-ID": str(user_id)}, timeout=10, trust_env=False
    ) as client, sync_playwright() as playwright:
        runtime = client.get("/health/ready").json()
        assert runtime["chat_worker_ready"], runtime
        browser = playwright.chromium.launch(
            headless=True, executable_path=str(args.executable) if args.executable else None
        )
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 1000})
            context.add_init_script(
                f"sessionStorage.setItem('nutripilot.userId', '{user_id}');"
            )
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"{args.base_url}/?thread={thread}#chat")
            page.wait_for_load_state("networkidle")

            def send(message: str) -> dict[str, Any]:
                composer = page.get_by_role("textbox", name="发送给 NutriPilot 的消息")
                expect(composer).to_be_enabled()
                composer.fill(message)
                with page.expect_response(
                    lambda response: response.request.method == "POST"
                    and response.url.endswith("/runs")
                ) as accepted:
                    page.get_by_role("button", name="发送消息", exact=True).click()
                assert accepted.value.status == 202
                run_id = accepted.value.json()["id"]
                deadline = time.monotonic() + 150
                while True:
                    run = client.get(f"/api/v1/runs/{run_id}").json()
                    if run["status"] not in {"queued", "running", "recovering", "cancelling"}:
                        break
                    assert time.monotonic() < deadline, run
                    time.sleep(0.3)
                assert run["status"] == "completed", run
                result = cast("dict[str, Any]", run["result"])
                expect(page.locator(".message-assistant").last.locator(".message-content")).to_have_text(
                    result["final_response"]["message"], timeout=15000
                )
                trace = client.get(f"/api/v1/traces/{result['trace_id']}").json()["events"]
                fallbacks = [event for event in trace if "fallback" in event["event_type"]]
                assert all(
                    args.scenario == "ingredients" and event["component"] == "PlannerAgent"
                    for event in fallbacks
                ), trace
                models = [event for event in trace if event["event_type"] == "model.completed"]
                assert models
                for event in models:
                    assert event["input_tokens"] > 0 and event["output_tokens"] > 0
                    assert event["payload"]["model"] == runtime["model_name"]
                results.append({"message": message, "result": result, "trace": trace})
                (args.output_dir / "result.json").write_text(
                    json.dumps(
                        {"runtime": runtime, "thread_id": thread, "results": results,
                         "errors": errors}, ensure_ascii=False, indent=2,
                    ), encoding="utf-8",
                )
                return result

            if args.scenario == "ingredients":
                scenarios = [
                    ("想吃西红柿", ["西红柿"], [], [], True),
                    ("再加鸡蛋", ["西红柿", "鸡蛋"], [], [], True),
                    ("改成豆腐", ["豆腐"], [], [], True),
                    ("鸡胸肉或豆腐都可以", [], [["鸡胸肉", "豆腐"]], [], True),
                    ("换一批", [], [["鸡胸肉", "豆腐"]], [], True),
                    ("不限食材", [], [], [], True),
                    ("我对鸡蛋过敏，想吃鸡胸肉", ["鸡胸肉"], [], ["鸡蛋"], True),
                    ("不限食材", [], [], ["鸡蛋"], True),
                    ("想吃鸡胸肉，预算不超过20元", ["鸡胸肉"], [], ["鸡蛋"], False),
                ]
                for message, required, groups, excluded, nonempty in scenarios:
                    state = send(message)
                    cards = state["final_response"]["recommendations"]
                    assert bool(cards) == nonempty, (message, state)
                    assert state["slots"].get("required_ingredients", []) == required, state
                    assert state["slots"].get("ingredient_any_groups", []) == groups, state
                    if excluded:
                        assert set(excluded) <= set(state["slots"].get("allergens", []))
                    for card in cards:
                        recipe = client.get(f"/api/v1/recipes/{card['meal_id']}").json()
                        records = []
                        for part in recipe["ingredients"]:
                            if not part.get("ingredient_id"):
                                continue
                            ingredient = client.get(
                                f"/api/v1/ingredients/{part['ingredient_id']}"
                            ).json()
                            records.append({
                                "ingredient_id": ingredient["id"], "name": ingredient["name"],
                                "aliases": ingredient["aliases"], "consumed": part["consumed"],
                            })
                        meal = {"ingredient_records": records}
                        assert meets_ingredient_requirements(meal, required, groups), card
                        assert not any(
                            match_ingredient(term, [], records, exclusion=True) for term in excluded
                        ), card
                        for group in [[term] for term in required] + groups:
                            assert any(
                                term in card["reason"] and match_ingredient(term, [], records)
                                for term in group
                            ), card
                    if not nonempty:
                        assert "价格未知" in state["final_response"]["message"], state
                for width, height in ((1440, 1000), (390, 844), (320, 740)):
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_function(
                        "document.querySelector('.workspace').getBoundingClientRect().left === 0"
                    )
                    page.locator(".message-assistant").last.scroll_into_view_if_needed()
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )
                    page.screenshot(path=str(args.output_dir / f"dialogue-{width}.png"))
                page.reload()
                page.wait_for_load_state("networkidle")
                expect(page.locator(".message-assistant").last).to_contain_text("价格未知")
                assert not errors, errors
                print(json.dumps({
                    "thread_id": thread, "turns": len(results), "model": runtime["model_name"],
                    "report": str(args.output_dir / "result.json"), "browser_errors": errors,
                }, ensure_ascii=False))
                return

            first = send("推荐午餐")
            cards = first["final_response"]["recommendations"]
            assert len(cards) >= 2
            assert all(len(item["reason"]) < 100 for item in cards)
            assert all("投料" not in item["reason"] for item in cards)
            sources = page.locator(".message-assistant").last.locator(".citation-list")
            assert not sources.evaluate("(element) => element.open")
            sources.locator("summary").click()
            expect(sources).to_contain_text("本配方采用版本")
            sources.locator("summary").click()
            page.screenshot(path=str(args.output_dir / "recommendation-desktop.png"))
            ambiguous = send("这个怎么做？")
            assert ambiguous["phase"] == "CLARIFY"
            ordinal = send("第二道怎么做？")
            assert ordinal["intent"] == "RECIPE_QUERY"
            assert ordinal["slots"]["food_name"] == cards[1]["name"]
            assert not ordinal["final_response"]["recommendations"]
            recipe = send("给我西葫芦炒鸡蛋的做法")
            response = recipe["final_response"]
            assert recipe["intent"] == "RECIPE_QUERY"
            assert "西葫芦 500g" in response["message"]
            assert "中火翻炒 15 秒" in response["message"]
            assert not response["recommendations"]
            assert response["citations"][0]["source_type"] == "recipe"
            latest = page.locator(".message-assistant").last
            latest.scroll_into_view_if_needed()
            assert latest.locator(".meal-result").count() == 0
            page.screenshot(path=str(args.output_dir / "recipe-desktop.png"))
            page.reload()
            page.wait_for_load_state("networkidle")
            expect(page.locator(".message-assistant").last).to_contain_text("中火翻炒 15 秒")
            for width, height in ((390, 844), (320, 740)):
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_function(
                    "document.querySelector('.workspace').getBoundingClientRect().left === 0"
                )
                page.locator(".message-assistant").last.scroll_into_view_if_needed()
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                page.screenshot(path=str(args.output_dir / f"recipe-mobile-{width}.png"))
            page.set_viewport_size({"width": 1440, "height": 1000})
            chicken = send("想吃鸡胸肉")
            assert chicken["intent"] == "MEAL_RECOMMENDATION"
            assert chicken["slots"]["required_ingredients"] == ["鸡胸肉"]
            assert not chicken["missing_slots"]
            chicken_cards = chicken["final_response"]["recommendations"]
            assert chicken_cards
            for card in chicken_cards:
                assert card["meal_id"] != 1080
                assert "鸡胸肉" in card["reason"]
                assert "中式" not in card["reason"]
                meal = client.get(f"/api/v1/recipes/{card['meal_id']}").json()
                ingredients = [
                    client.get(f"/api/v1/ingredients/{part['ingredient_id']}").json()["name"]
                    for part in meal["ingredients"] if part.get("ingredient_id")
                ]
                assert any("鸡胸肉" in ingredient for ingredient in ingredients)
            limited = send("想吃鸡胸肉，不超过600千卡")
            assert limited["slots"]["required_ingredients"] == ["鸡胸肉"]
            assert limited["final_response"]["recommendations"]
            for card in limited["final_response"]["recommendations"]:
                assert card["reason"].index("鸡胸肉") < card["reason"].index("600 千卡")
                assert card["nutrient_ranges"]["calories_kcal"]["maximum"] <= 600
            page.locator(".message-assistant").last.scroll_into_view_if_needed()
            page.screenshot(path=str(args.output_dir / "ingredient-desktop.png"))
            page.reload()
            page.wait_for_load_state("networkidle")
            expect(page.locator(".message-assistant").last).to_contain_text("鸡胸肉")
            for width, height in ((390, 844), (320, 740)):
                page.set_viewport_size({"width": width, "height": height})
                page.wait_for_function(
                    "document.querySelector('.workspace').getBoundingClientRect().left === 0"
                )
                page.locator(".message-assistant").last.scroll_into_view_if_needed()
                assert page.evaluate(
                    "document.documentElement.scrollWidth <= window.innerWidth"
                )
                page.screenshot(path=str(args.output_dir / f"ingredient-mobile-{width}.png"))
            assert not errors, errors
            report = {"runtime": runtime, "thread_id": thread, "results": results, "errors": errors}
            (args.output_dir / "result.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps({
                "thread_id": thread, "turns": len(results),
                "model": runtime["model_name"], "browser_errors": errors,
                "report": str(args.output_dir / "result.json"),
            }, ensure_ascii=False))
        finally:
            browser.close()


if __name__ == "__main__":
    main()
