"""Week-plan browser acceptance on a dedicated demo; no model calls."""

from __future__ import annotations

import argparse
import json
from functools import partial
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import Error, Page, expect, sync_playwright


def record_error(errors: list[str], error: Error) -> None:
    errors.append(str(error))


def save_preview(page: Page, title: str) -> None:
    page.get_by_role("dialog", name=title, exact=True).get_by_role(
        "button", name="确认保存", exact=True
    ).click()
    expect(page.get_by_role("dialog", name=title, exact=True)).to_have_count(0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5187")
    parser.add_argument("--output-dir", type=Path, default=Path(".runtime/diet-plan-web"))
    parser.add_argument("--executable", type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, executable_path=str(args.executable) if args.executable else None
        )
        try:
            for width, height in ((1440, 900), (390, 844)):
                context = browser.new_context(viewport={"width": width, "height": height})
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", partial(record_error, errors))
                name = f"week-fixture-{width}-{uuid4().hex[:6]}"
                try:
                    page.goto(args.base_url + "/#plans")
                    page.wait_for_load_state("networkidle")
                    expect(page.get_by_role("heading", name="周餐单", exact=True)).to_be_visible()
                    page.get_by_role("button", name="新建周餐单", exact=True).click()
                    editor = page.get_by_role("dialog", name="添加计划餐次", exact=True)
                    editor.get_by_label("餐单标题", exact=True).fill(name)
                    editor.get_by_role("combobox").first.select_option("breakfast")
                    editor.get_by_label("名称", exact=True).fill("test-breakfast")
                    editor.get_by_role("button", name="加入本餐并继续添加", exact=True).click()
                    editor.get_by_role("combobox").first.select_option("lunch")
                    editor.get_by_label("名称", exact=True).fill("test-lunch")
                    editor.get_by_role("button", name="查看预览", exact=True).click()
                    preview = page.get_by_role("dialog", name="确认餐单安排", exact=True)
                    expect(preview).to_contain_text("test-breakfast")
                    expect(preview).to_contain_text("test-lunch")
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    save_preview(page, "确认餐单安排")
                    plan_button = page.locator(".diet-plan-selector").get_by_role(
                        "button", name=name, exact=True
                    )
                    expect(plan_button).to_be_visible()
                    plan_button.click()
                    first_day = page.locator(".diet-plan-day").first
                    breakfast = first_day.locator(".diet-plan-slot").nth(0)
                    expect(breakfast).to_contain_text("test-breakfast")
                    breakfast.get_by_role("button", name="编辑餐次", exact=True).click()
                    editor = page.get_by_role("dialog", name="编辑计划餐次", exact=True)
                    editor.get_by_label("本餐备注", exact=True).fill("plan edit")
                    editor.get_by_role("button", name="查看预览", exact=True).click()
                    save_preview(page, "确认餐次更改")
                    expect(breakfast).to_contain_text("plan edit")
                    breakfast.get_by_role("button", name="复制餐次", exact=True).click()
                    copy = page.get_by_role("dialog", name="复制餐次", exact=True)
                    copy.get_by_label("目标餐次", exact=True).select_option("dinner")
                    copy.get_by_role("button", name="查看复制预览", exact=True).click()
                    save_preview(page, "确认复制餐次")
                    expect(
                        page.locator(".diet-plan-day").nth(1).locator(".diet-plan-slot").nth(2)
                    ).to_contain_text("test-breakfast")
                    breakfast.get_by_role("button", name="标记已吃", exact=True).click()
                    editor = page.get_by_role("dialog", name="标记已吃", exact=True)
                    editor.get_by_role("button", name="查看预览", exact=True).click()
                    save_preview(page, "确认饮食记录")
                    expect(breakfast).to_contain_text("已记实际 1 次")
                    breakfast.get_by_role("button", name="再记一次", exact=True).click()
                    editor = page.get_by_role("dialog", name="再记一次", exact=True)
                    editor.get_by_role("button", name="查看预览", exact=True).click()
                    save_preview(page, "确认饮食记录")
                    expect(breakfast).to_contain_text("已记实际 2 次")
                    page.get_by_role("button", name="复制整周", exact=True).click()
                    copy = page.get_by_role("dialog", name="复制整周", exact=True)
                    copy.get_by_role("button", name="查看复制预览", exact=True).click()
                    save_preview(page, "确认复制整周")
                    page.get_by_role("button", name="下一周", exact=True).click()
                    expect(page.locator(".diet-plan-selector button")).not_to_have_count(0)
                    page.get_by_role("button", name="上一周", exact=True).click()
                    page.locator(".diet-plan-selector").get_by_role(
                        "button", name=name, exact=True
                    ).click()
                    breakfast.get_by_role("button", name="删除餐次", exact=True).click()
                    save_preview(page, "确认删除餐次")
                    expect(breakfast).not_to_contain_text("test-breakfast")
                    page.get_by_role("button", name="归档餐单", exact=True).click()
                    save_preview(page, "确认归档餐单")
                    expect(
                        page.locator(".diet-plan-selector").get_by_role(
                            "button", name=name, exact=True
                        )
                    ).to_have_count(0)
                    page.get_by_label("显示已归档", exact=True).check()
                    page.locator(".diet-plan-selector").get_by_role(
                        "button", name=name + " · 已归档", exact=True
                    ).click()
                    page.get_by_role("button", name="恢复餐单", exact=True).click()
                    save_preview(page, "确认恢复餐单")
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    page.locator(".diet-plan-selector").get_by_role(
                        "button", name=name, exact=True
                    ).click()
                    expect(page.get_by_role("button", name="归档餐单", exact=True)).to_be_visible()
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )
                    assert not errors, errors
                    page.screenshot(
                        path=str(args.output_dir / f"{width}-week-plan.png"), full_page=True
                    )
                    results.append(
                        f"{width}x{height}: batch, preview reload, edit, meal/week copy, "
                        "intake/repeat, delete, archive/restore, no overflow"
                    )
                except BaseException:
                    page.screenshot(
                        path=str(args.output_dir / f"{width}-failed.png"), full_page=True
                    )
                    (args.output_dir / f"{width}-failed.html").write_text(page.content())
                    raise
                finally:
                    context.close()
        finally:
            browser.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
