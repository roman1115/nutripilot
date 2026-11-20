"""Verify the personal recommendation workflow on an isolated local demo."""

import argparse
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5195/")
    parser.add_argument("--executable")
    parser.add_argument("--output-dir", type=Path, default=Path(".runtime/personal-meal-web"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=args.executable)
        for width, height in ((1440, 900), (390, 844)):
            page = browser.new_page(viewport={"width": width, "height": height})
            errors: list[str] = []
            page.on("pageerror", lambda error, errors=errors: errors.append(str(error)))
            page.goto(args.base_url)
            page.get_by_role("textbox", name="发送给 NutriPilot 的消息").fill("今晚吃什么")
            page.get_by_role("button", name="发送消息", exact=True).click()
            card = page.locator('[aria-label="个人份量建议"] article').first
            expect(card).to_be_visible(timeout=60_000)
            portion = card.get_by_role("combobox").first
            selected_index = portion.evaluate("(el) => el.selectedIndex")
            portion.select_option(index=1 if selected_index == 0 else 0)
            expect(card).to_contain_text("已按调整后的个人份量重算")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            replace = page.get_by_role("button", name="换一组", exact=True).bounding_box()
            card_box = card.bounding_box()
            assert replace and card_box and replace["y"] >= card_box["y"] + card_box["height"]
            page.screenshot(path=args.output_dir / f"{width}-personal-meal.png")
            card.get_by_role("button", name="加入计划", exact=True).click()
            name = f"推荐验收 {width}-{uuid4().hex[:6]}"
            page.get_by_label("餐单名称").fill(name)
            page.get_by_role("button", name="查看预览", exact=True).click()
            preview = page.get_by_role("dialog", name="确认餐单安排", exact=True)
            expect(preview).to_contain_text(name)
            page.reload()
            expect(preview).to_contain_text(name)
            preview.get_by_role("button", name="确认保存", exact=True).click()
            expect(preview).to_have_count(0)
            page.reload()
            card.get_by_role("link", name="已加入 · 查看周餐单").click()
            expect(page.get_by_role("heading", name=name, exact=True)).to_be_visible()
            page.get_by_role("button", name="标记已吃", exact=True).click()
            editor = page.get_by_role("dialog", name="标记已吃", exact=True)
            editor.get_by_label("份量口径").first.select_option("unknown")
            editor.get_by_label("原始份量描述").first.fill("试用中未称重")
            editor.get_by_role("button", name="查看预览", exact=True).click()
            confirm = page.get_by_role("dialog", name="确认饮食记录", exact=True)
            confirm.get_by_role("button", name="确认保存", exact=True).click()
            recorded = page.get_by_text("已记实际 1 次", exact=True)
            expect(recorded).to_be_visible()
            page.reload()
            expect(recorded).to_be_visible()
            page.screenshot(path=args.output_dir / f"{width}-recorded.png")
            page.goto(args.base_url.rstrip("/") + "/#diet")
            page.get_by_role("button", name="更正记录", exact=True).first.click()
            correction = page.get_by_role("dialog", name="更正饮食记录", exact=True)
            correction.get_by_label("份量口径").first.select_option("unknown")
            description = f"验收更正-{width}"
            correction.get_by_label("原始份量描述").first.fill(description)
            correction.get_by_role("button", name="查看预览", exact=True).click()
            page.get_by_role("dialog", name="确认更正").get_by_role(
                "button", name="确认保存", exact=True,
            ).click()
            expect(page.get_by_text(description, exact=False)).to_be_visible()
            page.goto(args.base_url.rstrip("/") + "/#chat")
            page.get_by_role("textbox", name="发送给 NutriPilot 的消息").fill("今晚吃什么")
            page.get_by_role("button", name="发送消息", exact=True).click()
            groups = page.locator('[aria-label="个人份量建议"]')
            expect(groups).to_have_count(2, timeout=60_000)
            page.screenshot(path=args.output_dir / f"{width}-rerecommended.png")
            assert not errors, errors
            page.close()
            print(f"{width}x{height}: recommendation, preview reload, intake, correction and retry")
        browser.close()


if __name__ == "__main__":
    main()
