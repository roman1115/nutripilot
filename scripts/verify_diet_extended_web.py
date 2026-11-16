"""Fake-only extended browser flows: private label review, split, timezone and conflicts."""

import argparse
import io
import json
from functools import partial
from pathlib import Path
from uuid import uuid4

from PIL import Image
from playwright.sync_api import Error, Page, expect, sync_playwright


def record_error(errors: list[str], error: Error) -> None:
    errors.append(str(error))


def load_all_drafts(page: Page) -> None:
    expect(page.get_by_role("button", name="刷新", exact=True)).to_be_enabled()
    drawer = page.locator(".diet-drafts")
    if drawer.count() and drawer.get_attribute("open") is None:
        drawer.locator(":scope > summary").click()
    more = page.get_by_role("button", name="更多草稿", exact=True)
    buttons = page.locator(".diet-draft-strip button")
    while more.count():
        count = buttons.count()
        more.click()
        expect(buttons).not_to_have_count(count)


def load_all_records(page: Page) -> None:
    expect(page.get_by_role("button", name="刷新", exact=True)).to_be_enabled()
    more = page.get_by_role("button", name="加载更多", exact=True)
    records = page.locator(".diet-event")
    while more.count():
        count = records.count()
        more.click()
        expect(records).not_to_have_count(count)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5190")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    output = io.BytesIO()
    Image.new("RGB", (300, 180), "white").save(output, format="PNG")
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(args.executable))
        try:
            for width, height in ((1440, 900), (390, 844)):
                context = browser.new_context(viewport={"width": width, "height": height})
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", partial(record_error, errors))
                try:
                    page.goto(args.base_url + "/#diet")
                    page.wait_for_load_state("networkidle")
                    load_all_drafts(page)
                    previous_rice = (
                        page.locator(".diet-draft-strip button").filter(has_text="米饭").count()
                    )
                    page.get_by_role("button", name="文字与照片记餐", exact=True).click()
                    parse = page.get_by_role("dialog", name="文字与照片记餐")
                    parse.get_by_role("button", name="照片", exact=True).click()
                    parse.get_by_label("图片用途", exact=True).select_option("label_image")
                    parse.get_by_label("食物照片", exact=True).set_input_files(
                        {
                            "name": "synthetic.png",
                            "mimeType": "image/png",
                            "buffer": output.getvalue(),
                        }
                    )
                    parse.get_by_role("checkbox").check()
                    parse.get_by_role("button", name="准备解析草稿", exact=True).click()
                    editor = page.get_by_role("dialog", name="编辑草稿", exact=True)
                    expect(editor.get_by_role("button", name="核对标签资料")).to_be_visible(
                        timeout=15000
                    )
                    image = editor.locator("img")
                    expect(image).to_be_visible()
                    assert image.evaluate("(image) => image.complete && image.naturalWidth === 300")
                    update = editor.locator(".diet-draft-update")
                    expect(update).to_be_visible()
                    text_bounds = update.locator("p").bounding_box()
                    assert (
                        text_bounds and text_bounds["width"] >= 200 and text_bounds["height"] < 160
                    )
                    update.screenshot(path=str(args.output_dir / f"{width}-draft-update.png"))
                    image.scroll_into_view_if_needed()
                    page.screenshot(path=str(args.output_dir / f"{width}-private-image.png"))
                    editor.get_by_role("button", name="核对标签资料").click()
                    food = page.get_by_role("dialog", name="添加个人食物")
                    expect(food.get_by_role("button", name="查看食物预览")).to_be_disabled()
                    name = f"label-browser-{width}-{uuid4().hex[:6]}"
                    food.get_by_label("食物名称", exact=True).fill(name)
                    food.get_by_label("已逐项核对原图、单位和标签口径", exact=True).check()
                    page.screenshot(
                        path=str(args.output_dir / f"{width}-label.png"), full_page=True
                    )
                    food.get_by_role("button", name="查看食物预览", exact=True).click()
                    preview = page.get_by_role("dialog").filter(has_text="确认变更")
                    expect(preview).to_contain_text("100")
                    preview.get_by_role("button", name="确认保存", exact=True).click()
                    page.get_by_role("button", name="个人食物", exact=True).click()
                    row = page.locator(".diet-food-list-item").filter(has_text=name)
                    expect(row).to_have_count(1)
                    row.get_by_role("button", name="删除食物", exact=True).click()
                    page.get_by_role("dialog").get_by_role("button", name="确认保存").click()
                    page.get_by_role("button", name="数据管理", exact=True).click()
                    management = page.get_by_role("region", name="数据管理")
                    trash = management.locator(".diet-trash-row").filter(has_text=name)
                    expect(trash).to_be_visible()
                    trash.get_by_role("checkbox").check()
                    management.get_by_role("button", name="核对永久清除（1）", exact=True).click()
                    purge = page.get_by_role("dialog", name="确认永久清除")
                    expect(purge).to_contain_text("独立记录")
                    purge.get_by_label("我了解此操作不可恢复", exact=True).check()
                    purge.get_by_role("button", name="永久清除", exact=True).click()
                    expect(trash).to_have_count(0)

                    page.get_by_role("button", name="记录与分析", exact=True).click()
                    page.get_by_role("button", name="文字与照片记餐", exact=True).click()
                    parse = page.get_by_role("dialog", name="文字与照片记餐")
                    parse.get_by_label("饮食原话").fill("我今天午餐和晚餐分别吃了半碗米饭")
                    parse.get_by_role("button", name="准备解析草稿").click()
                    editor = page.get_by_role("dialog", name="编辑草稿", exact=True)
                    split = editor.get_by_role("button", name="拆分为独立草稿（2）", exact=True)
                    expect(split).to_be_visible(timeout=15000)
                    split.click()
                    expect(editor).not_to_be_visible()
                    page.wait_for_load_state("networkidle")
                    strip = page.get_by_role("region", name="草稿箱")
                    load_all_drafts(page)
                    expect(strip.get_by_role("button").filter(has_text="米饭")).to_have_count(
                        previous_rice + 2
                    )
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    load_all_drafts(page)
                    expect(strip.get_by_role("button").filter(has_text="米饭")).to_have_count(
                        previous_rice + 2
                    )
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    assert not errors, errors
                    page.screenshot(
                        path=str(args.output_dir / f"{width}-split.png"), full_page=True
                    )
                    page.get_by_role("button", name="记录已吃", exact=True).click()
                    manual = page.get_by_role("dialog", name="记录已吃", exact=True)
                    manual.get_by_label("餐次", exact=True).select_option("lunch")
                    record_name = f"timezone-{width}-{uuid4().hex[:6]}"
                    manual.get_by_label("名称", exact=True).fill(record_name)
                    manual.get_by_role("button", name="查看预览", exact=True).click()
                    page.get_by_role("dialog", name="确认饮食记录").get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    expect(page.get_by_role("dialog", name="确认饮食记录")).not_to_be_visible()
                    load_all_records(page)
                    record = page.locator(".diet-event").filter(has_text=record_name)
                    record.get_by_role("button", name="更正记录").click()
                    correction = page.get_by_role("dialog", name="更正饮食记录")
                    correction.get_by_label("此记录的食用时区（IANA）", exact=True).fill(
                        "America/Denver"
                    )
                    correction.get_by_role("button", name="查看预览", exact=True).click()
                    preview = page.get_by_role("dialog", name="确认更正", exact=True)
                    expect(preview).to_contain_text("UTC → America/Denver")
                    preview.get_by_role("button", name="确认保存", exact=True).click()
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    load_all_records(page)
                    record.get_by_role("button", name="更正记录").click()
                    correction = page.get_by_role("dialog", name="更正饮食记录")
                    expect(correction.get_by_label("此记录的食用时区（IANA）")).to_have_value(
                        "America/Denver"
                    )
                    correction.get_by_label("本餐备注", exact=True).fill("stale tab edit")
                    second = context.new_page()
                    second.goto(args.base_url + "/#diet")
                    second.wait_for_load_state("networkidle")
                    load_all_records(second)
                    second.locator(".diet-event").filter(has_text=record_name).get_by_role(
                        "button", name="更正记录"
                    ).click()
                    fresh = second.get_by_role("dialog", name="更正饮食记录")
                    fresh.get_by_label("本餐备注", exact=True).fill("fresh tab edit")
                    fresh.get_by_role("button", name="查看预览", exact=True).click()
                    second.get_by_role("dialog", name="确认更正").get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    correction.get_by_role("button", name="查看预览", exact=True).click()
                    expect(correction.get_by_role("alert")).to_be_visible()
                    expect(correction.get_by_label("本餐备注", exact=True)).to_have_value(
                        "stale tab edit"
                    )
                    page.screenshot(
                        path=str(args.output_dir / f"{width}-conflict.png"), full_page=True
                    )
                    second.close()
                    correction.get_by_role("button", name="关闭编辑", exact=True).click()
                    page.get_by_role("button", name="记录已吃", exact=True).click()
                    offline = page.get_by_role("dialog", name="记录已吃", exact=True)
                    offline.get_by_label("餐次", exact=True).select_option("lunch")
                    offline_name = f"offline-{width}-{uuid4().hex[:6]}"
                    offline.get_by_label("名称", exact=True).fill(offline_name)
                    context.set_offline(True)
                    offline.get_by_role("button", name="查看预览", exact=True).click()
                    expect(offline.get_by_role("alert")).to_be_visible()
                    expect(offline.get_by_label("名称", exact=True)).to_have_value(offline_name)
                    expect(page.get_by_role("dialog", name="确认饮食记录")).to_have_count(0)
                    page.screenshot(path=str(args.output_dir / f"{width}-offline.png"))
                    context.set_offline(False)
                    offline.get_by_role("button", name="查看预览", exact=True).click()
                    recovered = page.get_by_role("dialog", name="确认饮食记录")
                    expect(recovered).to_contain_text(offline_name)
                    recovered.get_by_role("button", name="确认保存", exact=True).click()
                    expect(recovered).not_to_be_visible()
                    load_all_records(page)
                    expect(page.locator(".diet-event").filter(has_text=offline_name)).to_have_count(
                        1
                    )
                    estimate_records = page.locator(".diet-event").filter(
                        has_text="照片中的家常炒菜"
                    )
                    before_photos = estimate_records.count()
                    page.get_by_role("button", name="文字与照片记餐", exact=True).click()
                    parse = page.get_by_role("dialog", name="文字与照片记餐")
                    parse.get_by_role("button", name="照片", exact=True).click()
                    parse.get_by_label("食物照片", exact=True).set_input_files(
                        {
                            "name": "synthetic.png",
                            "mimeType": "image/png",
                            "buffer": output.getvalue(),
                        }
                    )
                    parse.get_by_role("checkbox").check()
                    parse.get_by_role("button", name="准备解析草稿", exact=True).click()
                    editor = page.get_by_role("dialog", name="编辑草稿", exact=True)
                    expect(editor.get_by_role("button", name="载入解析结果")).to_be_visible(
                        timeout=15000
                    )
                    editor.get_by_role("button", name="载入解析结果").click()
                    expect(editor.get_by_label("下限 (g)", exact=True)).to_have_value("150")
                    expect(editor.get_by_label("上限 (g)", exact=True)).to_have_value("250")
                    estimate_notice = editor.get_by_text("模型估算，非实测数据", exact=True)
                    expect(estimate_notice).to_be_visible()
                    estimate_notice.scroll_into_view_if_needed()
                    page.screenshot(
                        path=str(args.output_dir / f"{width}-image-estimate-editor.png")
                    )
                    editor.get_by_role("button", name="查看预览", exact=True).click()
                    preview = page.get_by_role("dialog", name="确认饮食记录", exact=True)
                    expect(preview).to_contain_text("150–400 kcal")
                    expect(preview).to_contain_text("粗略估算")
                    page.screenshot(
                        path=str(args.output_dir / f"{width}-image-estimate-preview.png")
                    )
                    preview.get_by_role("button", name="确认保存", exact=True).click()
                    expect(preview).not_to_be_visible()
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    load_all_records(page)
                    expect(estimate_records).to_have_count(before_photos + 1)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    assert not errors, errors
                    results.append(
                        f"{width}x{height}: private image visible, label review/confirm, "
                        "food purge impact, split refresh, timezone diff, stale-tab rejection, "
                        "offline input retained and recovered without duplicate record"
                        ", photo estimate saved without catalog matching or measured portions"
                    )
                except BaseException:
                    page.screenshot(
                        path=str(args.output_dir / f"{width}-failure.png"), full_page=True
                    )
                    raise
                finally:
                    context.close()
        finally:
            browser.close()
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
