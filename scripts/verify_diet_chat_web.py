"""Browser check of reviewed chat confirmation, using Fake or an authenticated real stack."""

import argparse
import json
from functools import partial
from pathlib import Path

from playwright.sync_api import APIRequestContext, Error, expect, sync_playwright


def record_error(errors: list[str], error: Error) -> None:
    errors.append(str(error))


def record_count(client: APIRequestContext, url: str) -> int:
    response = client.get(url)
    assert response.ok, response.status
    return int(response.json()["items"][0]["actual"]["record_count"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5190")
    parser.add_argument("--api-url", default="http://127.0.0.1:8019")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--environment", type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: list[str] = []
    token = None
    if args.environment:
        tokens = json.loads(json.loads(args.environment.read_text())["API_ACCESS_TOKENS"])
        token = next(key for key, user_id in tokens.items() if user_id == 1)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(args.executable))
        api = playwright.request.new_context(
            extra_http_headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        try:
            for width, height in ((1440, 900), (390, 844)):
                context = browser.new_context(viewport={"width": width, "height": height})
                if token:
                    context.add_init_script(
                        'sessionStorage.setItem("nutripilot.accessToken",'
                        + json.dumps(token) + ");"
                        'sessionStorage.setItem("nutripilot.userId","1");',
                    )
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", partial(record_error, errors))
                anchor = api.get(args.api_url + "/api/v1/diet/context")
                assert anchor.ok, anchor.status
                today = anchor.json()["anchor"]["anchor_local_date"]
                url = args.api_url + (
                    f"/api/v1/diet/summary?start_date={today}&end_date={today}&granularity=day"
                )

                count_records = partial(record_count, api, url)
                timeout = 150000 if token else 15000

                try:
                    page.goto(args.base_url + "/#chat")
                    page.wait_for_load_state("networkidle")
                    composer = page.get_by_role("textbox", name="发送给 NutriPilot 的消息")
                    for method in ("button", "natural"):
                        before = count_records()
                        page.get_by_role("button", name="新对话", exact=True).click()
                        composer.fill("帮我记：我今天午餐吃了苹果，重量不知道")
                        page.get_by_role("button", name="发送消息", exact=True).click()
                        confirm = page.get_by_role("button", name="确认执行", exact=True)
                        expect(confirm).to_be_enabled(timeout=timeout)
                        panel = page.locator(".confirmation-panel")
                        expect(panel).to_contain_text("苹果")
                        expect(panel).to_contain_text("份量未知")
                        assert count_records() == before
                        if method == "button":
                            page.reload()
                            page.wait_for_load_state("networkidle")
                            expect(confirm).to_be_enabled(timeout=timeout)
                            page.screenshot(path=str(args.output_dir / f"{width}-preview.png"))
                            confirm.click()
                        else:
                            page.get_by_role("button", name="关闭执行过程", exact=True).click()
                            composer.fill("确认保存")
                            page.get_by_role("button", name="发送消息", exact=True).click()
                        expect(panel).not_to_be_visible(timeout=timeout)
                        expect(
                            page.get_by_text("已保存，本次确认不会重复写入。", exact=True)
                        ).to_be_visible()
                        assert count_records() == before + 1
                        page.reload()
                        page.wait_for_load_state("networkidle")
                        expect(composer).to_be_enabled()
                        assert count_records() == before + 1
                    assert not errors, errors
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    page.screenshot(path=str(args.output_dir / f"{width}-saved.png"))
                    results.append(
                        f"{width}x{height}: preview reload, no automatic intake, "
                        "button/natural approval, saved reload without duplicate"
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
            api.dispose()
    (args.output_dir / "report.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
