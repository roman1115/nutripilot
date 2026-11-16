"""Real-photo browser acceptance at desktop and mobile widths, using the local token file."""

import argparse
import json
from functools import partial
from pathlib import Path

from playwright.sync_api import Error, expect, sync_playwright


def record_error(errors: list[str], error: Error) -> None:
    errors.append(str(error))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5191")
    parser.add_argument(
        "--environment", type=Path, default=Path(".runtime/local-diet/environment.json")
    )
    parser.add_argument(
        "--photo", type=Path, default=Path(".runtime/diet-acceptance-20261112/photos/I06.jpg")
    )
    parser.add_argument(
        "--output", type=Path, default=Path(".runtime/diet-acceptance-20261112/real-final/browser")
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    tokens = json.loads(json.loads(args.environment.read_text())["API_ACCESS_TOKENS"])
    token = next(key for key, user_id in tokens.items() if user_id == 1)
    results = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        )
        try:
            for width, height in ((1440, 900), (390, 844)):
                context = browser.new_context(viewport={"width": width, "height": height})
                context.add_init_script(
                    'sessionStorage.setItem("nutripilot.accessToken",' + json.dumps(token) + ");"
                    'sessionStorage.setItem("nutripilot.userId","1");',
                )
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", partial(record_error, errors))
                try:
                    page.goto(args.base_url + "/#diet")
                    page.wait_for_load_state("networkidle")
                    page.get_by_role("button", name="文字与照片记餐", exact=True).click()
                    form = page.get_by_role("dialog", name="文字与照片记餐", exact=True)
                    form.get_by_role("button", name="照片", exact=True).click()
                    form.get_by_label("照片补充说明", exact=True).fill(
                        "这是我今天午餐吃的，请粗略估算。"
                    )
                    form.get_by_label("食物照片", exact=True).set_input_files(args.photo)
                    form.get_by_role("checkbox").check()
                    form.get_by_role("button", name="准备解析草稿", exact=True).click()
                    editor = page.get_by_role("dialog", name="编辑草稿", exact=True)
                    expect(editor.get_by_role("button", name="载入解析结果")).to_be_visible(
                        timeout=240000
                    )
                    expect(editor.get_by_role("button", name="载入解析结果")).to_be_enabled(
                        timeout=240000
                    )
                    editor.get_by_role("button", name="载入解析结果").click()
                    expect(editor.get_by_label("名称", exact=True).first).not_to_have_value("")
                    expect(
                        editor.get_by_text("模型估算，非实测数据", exact=True).first
                    ).to_be_visible()
                    editor.get_by_label("主体").select_option("self")
                    editor.get_by_label("实际发生情况").select_option("actual")
                    editor.get_by_label("餐次", exact=True).select_option("lunch")
                    lower = editor.get_by_label("下限 (g)", exact=True)
                    if lower.count():
                        lower.first.fill("120")
                        editor.get_by_label("上限 (g)", exact=True).first.fill("180")
                    editor.screenshot(path=str(args.output / f"{width}-editor.png"))
                    editor.get_by_role("button", name="查看预览", exact=True).click()
                    preview = page.get_by_role("dialog", name="确认饮食记录", exact=True)
                    expect(preview).to_contain_text("粗略估算")
                    preview.screenshot(path=str(args.output / f"{width}-preview.png"))
                    preview.get_by_role("button", name="确认保存", exact=True).click()
                    expect(preview).not_to_be_visible()
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    expect(page.locator(".diet-event").first).to_be_visible()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    assert not errors, errors
                    page.screenshot(path=str(args.output / f"{width}-saved.png"), full_page=True)
                    results.append({"width": width, "height": height, "passed": True})
                    print(f"{width}: passed", flush=True)
                except BaseException:
                    page.screenshot(path=str(args.output / f"{width}-failure.png"), full_page=True)
                    raise
                finally:
                    context.close()
        finally:
            browser.close()
    (args.output / "report.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
