"""Manual-intake browser acceptance against a dedicated local demo, not production."""

from __future__ import annotations

import argparse
import json
from functools import partial
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import Error, expect, sync_playwright


def record_error(errors: list[str], error: Error) -> None:
    errors.append(str(error))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5186")
    parser.add_argument("--output-dir", type=Path, default=Path(".runtime/diet-web"))
    parser.add_argument("--executable", type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path=str(args.executable) if args.executable else None,
        )
        try:
            for width, height in ((1440, 900), (390, 844)):
                context = browser.new_context(viewport={"width": width, "height": height})
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", partial(record_error, errors))
                name = f"browser-fixture-{width}-{uuid4().hex[:6]}"
                try:
                    page.goto(args.base_url + "/#diet")
                    page.wait_for_load_state("networkidle")
                    expect(page.get_by_role("heading", name="饮食记录", exact=True)).to_be_visible()
                    page.get_by_role("button", name="记录已吃", exact=True).click()
                    editor = page.get_by_role("dialog", name="记录已吃", exact=True)
                    editor.get_by_role("combobox").first.select_option("lunch")
                    editor.get_by_label("名称", exact=True).fill(name)
                    editor.get_by_role("button", name="添加食物", exact=True).click()
                    editor.get_by_label("名称", exact=True).nth(1).fill(name + "-second")
                    editor.locator(".diet-item-fields").nth(1).get_by_role(
                        "combobox"
                    ).first.select_option("mass")
                    editor.get_by_label("克重 (g)", exact=True).fill("50")
                    editor.get_by_role("button", name="查看预览", exact=True).click()
                    preview = page.get_by_role("dialog", name="确认饮食记录", exact=True)
                    expect(preview).to_be_visible()
                    expect(preview).to_contain_text("未知")
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    preview = page.get_by_role("dialog", name="确认饮食记录", exact=True)
                    expect(preview).to_be_visible()
                    page.screenshot(
                        path=str(args.output_dir / f"{width}-preview.png"), full_page=True
                    )
                    preview.get_by_role("button", name="确认保存", exact=True).click()
                    expect(preview).not_to_be_visible()
                    event = page.locator(".diet-event").filter(has_text=name)
                    expect(event).to_have_count(1)
                    expect(event).to_contain_text("份量未知")
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    expect(page.locator(".diet-event").filter(has_text=name)).to_have_count(1)
                    event = page.locator(".diet-event").filter(has_text=name)
                    event.get_by_role("button", name="更正记录", exact=True).click()
                    editor = page.get_by_role("dialog", name="更正饮食记录", exact=True)
                    editor.get_by_label("本餐备注", exact=True).fill("browser correction")
                    editor.get_by_role("button", name="查看预览", exact=True).click()
                    page.get_by_role("dialog", name="确认更正", exact=True).get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    expect(page.locator(".diet-event").filter(has_text=name)).to_contain_text(
                        "browser correction"
                    )
                    page.locator(".diet-event").filter(has_text=name).get_by_role(
                        "button", name="删除记录", exact=True
                    ).click()
                    page.get_by_role("dialog", name="确认移入回收站", exact=True).get_by_role(
                        "button", name="确认删除", exact=True
                    ).click()
                    expect(page.locator(".diet-event").filter(has_text=name)).to_have_count(0)
                    page.get_by_label("显示已删除记录", exact=True).check()
                    event = page.locator(".diet-event").filter(has_text=name)
                    expect(event).to_have_count(1)
                    event.get_by_role("button", name="恢复记录", exact=True).click()
                    page.get_by_role("dialog", name="确认恢复", exact=True).get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    expect(page.locator(".diet-event").filter(has_text=name)).not_to_contain_text(
                        "已删除"
                    )
                    page.get_by_role("button", name="营养目标", exact=True).click()
                    targets = page.get_by_role("dialog", name="设置营养目标", exact=True)
                    targets.get_by_role("checkbox", name="热量 (kcal)", exact=True).check()
                    target_value = 1000 + int(uuid4().hex[:3], 16)
                    targets.get_by_label("下限或固定值", exact=True).first.fill(str(target_value))
                    targets.get_by_label("上限", exact=True).first.fill(str(target_value + 100))
                    targets.get_by_role("button", name="查看目标预览", exact=True).click()
                    page.get_by_role("dialog", name="确认目标设置", exact=True).get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    page.get_by_role("button", name="目标管理", exact=True).click()
                    expect(page.locator(".diet-target-row")).to_have_count(1)
                    page.get_by_role("button", name="记录与分析", exact=True).click()
                    page.locator(".diet-status-menu > summary").click()
                    page.get_by_role("button", name="声明已记完整", exact=True).click()
                    page.get_by_role("dialog", name="确认日记录状态", exact=True).get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    expect(page.locator(".diet-day-status")).to_contain_text("已声明完整")
                    page.get_by_role("button", name="每周", exact=True).click()
                    expect(
                        page.locator(".diet-week-details dl > div").filter(has_text="自然周日均")
                    ).to_contain_text("未知")
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    ), "Horizontal overflow"
                    assert not errors, errors
                    page.screenshot(path=str(args.output_dir / f"{width}-week.png"), full_page=True)
                    page.get_by_role("button", name="个人食物", exact=True).click()
                    page.get_by_role("button", name="添加食物", exact=True).click()
                    food_editor = page.get_by_role("dialog", name="添加个人食物", exact=True)
                    food_name = f"personal-{name}"
                    food_editor.get_by_label("食物名称", exact=True).fill(food_name)
                    food_editor.get_by_label("资料口径", exact=True).select_option("per_serving")
                    food_editor.get_by_label("每份克重（可留空）", exact=True).fill("80")
                    food_editor.get_by_label("来源说明或标签原文", exact=True).fill(
                        "Browser test label"
                    )
                    food_editor.get_by_role("checkbox", name="热量 (kcal)", exact=True).check()
                    food_editor.get_by_label("下限或固定值", exact=True).first.fill("100")
                    food_editor.get_by_role("button", name="添加过敏原", exact=True).click()
                    food_editor.get_by_label("过敏原名称", exact=True).fill("milk")
                    food_editor.get_by_label("食品过敏原状态").select_option("known_present")
                    food_editor.get_by_label("过敏原依据引用", exact=True).fill("manual-label")
                    food_editor.get_by_label("过敏原依据说明", exact=True).fill("Contains milk")
                    food_editor.get_by_role("button", name="查看食物预览", exact=True).click()
                    page.get_by_role("dialog", name="确认变更", exact=True).get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    food_row = page.locator(".diet-food-list-item").filter(has_text=food_name)
                    expect(food_row).to_contain_text("milk：含有")
                    food_row.get_by_role("button", name="修改食物", exact=True).click()
                    food_editor = page.get_by_role("dialog", name="修改食物资料", exact=True)
                    food_editor.get_by_label("品牌", exact=True).fill("Browser brand")
                    food_editor.get_by_role("button", name="查看食物预览", exact=True).click()
                    page.get_by_role("dialog", name="确认变更", exact=True).get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    expect(food_row).to_contain_text("Browser brand")
                    food_row.get_by_role("button", name="删除食物", exact=True).click()
                    page.get_by_role("dialog", name="确认变更", exact=True).get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    expect(food_row).to_have_count(0)
                    page.get_by_label("显示已删除", exact=True).check()
                    food_row.get_by_role("button", name="恢复食物", exact=True).click()
                    page.get_by_role("dialog", name="确认变更", exact=True).get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    expect(food_row).not_to_contain_text("已删除")
                    page.screenshot(
                        path=str(args.output_dir / f"{width}-foods.png"), full_page=True
                    )
                    page.get_by_role("button", name="记录与分析", exact=True).click()
                    page.get_by_role("button", name="每日", exact=True).click()
                    page.get_by_role("button", name="记录已吃", exact=True).click()
                    editor = page.get_by_role("dialog", name="记录已吃", exact=True)
                    editor.get_by_label("名称", exact=True).fill(food_name)
                    editor.get_by_role("button", name="匹配食物1资料", exact=True).click()
                    editor.locator(".diet-food-results button").filter(has_text=food_name).click()
                    editor.get_by_role("combobox").first.select_option("snack")
                    editor.get_by_label("份量口径").select_option("servings")
                    editor.get_by_label("本人份数", exact=True).fill("2")
                    editor.get_by_role("button", name="查看预览", exact=True).click()
                    preview = page.get_by_role("dialog", name="确认饮食记录", exact=True)
                    expect(preview).to_contain_text("200 kcal")
                    preview.get_by_role("button", name="确认保存", exact=True).click()
                    expect(page.locator(".diet-day-status")).to_contain_text("尚未声明完整")
                    page.get_by_role("button", name="记录已吃", exact=True).click()
                    page.get_by_role("dialog", name="记录已吃", exact=True).press("Escape")
                    expect(page.get_by_role("dialog")).to_have_count(0)
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )
                    assert not errors, errors
                    results.append(
                        f"{width}x{height}: multi-food draft, confirm, refresh, correction, "
                        "delete/restore, target, day declaration, weekly unknown, "
                        "personal food CRUD, "
                        "allergen evidence, matched servings, preview reload, Escape, no overflow"
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
