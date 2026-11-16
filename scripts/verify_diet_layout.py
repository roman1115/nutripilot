"""Read-only browser checks for the diet layout, with explicit synthetic summary cases."""

from __future__ import annotations

import argparse
import json
from functools import partial
from pathlib import Path

from playwright.sync_api import Error, Request, Route, expect, sync_playwright


def record_error(errors: list[str], error: Error) -> None:
    errors.append(str(error))


def record_write(writes: list[str], request: Request) -> None:
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        writes.append(request.method + " " + request.url)


def summary_case(scenario: str, route: Route) -> None:
    response = route.fetch()
    payload = response.json()
    actual = payload["items"][0]["actual"]
    actual["day_status"] = "complete" if scenario == "overlap" else "open"
    for nutrient in actual["nutrients"].values():
        nutrient.update(
            total=None, known_subtotal={"lower": "0", "upper": "0"},
            complete=False, known_item_count=0, total_item_count=2,
        )
    if scenario != "unknown":
        actual["nutrients"]["calories_kcal"].update(
            known_subtotal={"lower": "120.2", "upper": "149.2"}, known_item_count=1,
        )
    if scenario == "overlap":
        actual["nutrients"]["calories_kcal"].update(
            total={"lower": "120.2", "upper": "149.2"}, complete=True,
        )
        payload["items"][0]["target_comparison"]["calories_kcal"] = {
            "status": "overlap", "reason": None,
        }
    route.fulfill(response=response, json=payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5190")
    parser.add_argument("--environment", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    token = None
    if args.environment:
        environment = json.loads(args.environment.read_text())
        token = next(
            (k for k, v in json.loads(environment["API_ACCESS_TOKENS"]).items() if v == 1),
            None,
        )
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(args.executable))
        try:
            for width, height in (
                (1440, 900), (1024, 768), (768, 1024),
                (390, 844), (375, 812), (320, 740),
            ):
                context = browser.new_context(
                    viewport={"width": width, "height": height}, reduced_motion="reduce",
                )
                if token:
                    context.add_init_script(
                        'sessionStorage.setItem("nutripilot.accessToken",'
                        + json.dumps(token) + ");"
                        'sessionStorage.setItem("nutripilot.userId","1");',
                    )
                page = context.new_page()
                errors: list[str] = []
                writes: list[str] = []
                page.on("pageerror", partial(record_error, errors))
                page.on("request", partial(record_write, writes))
                try:
                    page.goto(args.base_url + "/#diet")
                    page.wait_for_load_state("networkidle")
                    expect(page.get_by_role("region", name="实际饮食", exact=True)).to_be_visible()
                    bounds = page.locator(".diet-records").bounding_box()
                    assert bounds is not None and bounds["y"] < height * .8
                    assert page.locator(".diet-timezone-menu").get_attribute("open") is None
                    drafts = page.locator(".diet-drafts")
                    if drafts.count():
                        assert drafts.get_attribute("open") is None
                        drafts.locator(":scope > summary").focus()
                        page.keyboard.press("Enter")
                        expect(drafts).to_have_attribute("open", "")
                        page.keyboard.press("Enter")
                        assert drafts.get_attribute("open") is None
                        drafts.locator(":scope > summary").blur()
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                    page.screenshot(path=str(args.output_dir / f"{width}-records.png"))
                    page.get_by_role("button", name="每周", exact=True).click()
                    expect(page.locator(".diet-week-details")).to_be_visible()
                    expect(page.get_by_role("button", name="上一周", exact=True)).to_be_visible()
                    expect(page.get_by_role("button", name="下一周", exact=True)).to_be_visible()
                    page.get_by_role("button", name="目标管理", exact=True).click()
                    expect(page.get_by_role("heading", name="目标时间线")).to_be_visible()
                    expect(page.get_by_role("button", name="营养目标", exact=True)).to_have_count(1)
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    target_tab = page.get_by_role("button", name="目标管理", exact=True)
                    expect(target_tab).to_have_attribute("aria-pressed", "true")
                    page.screenshot(path=str(args.output_dir / f"{width}-targets.png"))
                    page.get_by_role("button", name="个人食物", exact=True).click()
                    expect(page.locator(".diet-personal-foods")).to_be_visible()
                    page.get_by_role("button", name="数据管理", exact=True).click()
                    expect(page.get_by_label("查询日期", exact=True)).to_be_visible()
                    expect(page.get_by_role("region", name="数据管理", exact=True)).to_be_visible()
                    page.get_by_role("button", name="记录与分析", exact=True).click()
                    page.get_by_role("button", name="每日", exact=True).click()
                    expect(page.locator(".diet-day-status")).to_be_visible()
                    day = page.get_by_label("查询日期", exact=True).input_value()
                    page.get_by_label("查询日期", exact=True).fill("2025-01-01")
                    expect(page.locator(".diet-records > .diet-section-heading")).to_contain_text(
                        "2025-01-01",
                    )
                    page.get_by_role("button", name="今天", exact=True).click()
                    expect(page.get_by_label("查询日期", exact=True)).to_have_value(day)
                    page.locator(".diet-timezone-menu > summary").click()
                    expect(page.get_by_label("记餐时区", exact=True)).to_be_visible()
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                    page.locator(".diet-timezone-menu > summary").click()
                    if width == 1440:
                        for scenario in ("unknown", "partial", "overlap"):
                            handler = partial(summary_case, scenario)
                            page.route("**/api/v1/diet/summary?*", handler)
                            refresh = page.get_by_role("button", name="刷新", exact=True)
                            refresh.click()
                            expect(refresh).to_be_enabled()
                            metric = page.locator(".diet-summary .nutrient-calories_kcal")
                            expect(metric.locator("strong")).to_have_text(
                                "未知" if scenario == "unknown" else "120–150 kcal",
                            )
                            if scenario == "partial":
                                expect(metric).to_contain_text("已知小计 · 1/2 项有依据")
                            if scenario == "overlap":
                                page.locator(".diet-nutrition-details > summary").click()
                                comparison = page.locator(".diet-comparison > div").first
                                expect(comparison).to_contain_text("与目标区间交叉")
                            page.unroute("**/api/v1/diet/summary?*", handler)
                    assert not errors, errors
                    assert not writes, "Layout checks must not alter user data."
                    results.append(
                        {"width": width, "height": height, "passed": True, "writes": 0},
                    )
                except BaseException:
                    page.screenshot(
                        path=str(args.output_dir / f"{width}-failure.png"), full_page=True,
                    )
                    raise
                finally:
                    context.close()
        finally:
            browser.close()
    report = {
        "scope": "Real read-only views; three summary states use synthetic HTTP fixtures.",
        "passed": True, "results": results,
    }
    (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
