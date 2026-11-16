"""Dedicated demo browser acceptance for private exports and explicit record purge."""

from __future__ import annotations

import argparse
import json
import zipfile
from functools import partial
from pathlib import Path
from uuid import uuid4

from playwright.sync_api import Error, Page, expect, sync_playwright


def record_error(errors: list[str], error: Error) -> None:
    errors.append(str(error))


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
    parser.add_argument("--base-url", default="http://127.0.0.1:5188")
    parser.add_argument("--output-dir", type=Path, default=Path(".runtime/diet-privacy-web"))
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
                name = f"privacy-fixture-{width}-{uuid4().hex[:8]}"
                try:
                    page.goto(args.base_url + "/#diet")
                    page.wait_for_load_state("networkidle")
                    page.get_by_role("button", name="记录已吃", exact=True).click()
                    editor = page.get_by_role("dialog", name="记录已吃", exact=True)
                    editor.get_by_role("combobox").first.select_option("lunch")
                    editor.get_by_label("名称", exact=True).fill(name)
                    editor.get_by_role("button", name="查看预览", exact=True).click()
                    page.get_by_role("dialog", name="确认饮食记录", exact=True).get_by_role(
                        "button", name="确认保存", exact=True
                    ).click()
                    expect(page.get_by_role("dialog", name="确认饮食记录")).not_to_be_visible()
                    load_all_records(page)
                    record = page.locator(".diet-event").filter(has_text=name)
                    expect(record).to_have_count(1)
                    record.get_by_role("button", name="删除记录", exact=True).click()
                    page.get_by_role("dialog", name="确认移入回收站", exact=True).get_by_role(
                        "button", name="确认删除", exact=True
                    ).click()
                    expect(record).to_have_count(0)
                    page.get_by_role("button", name="数据管理", exact=True).click()
                    management = page.get_by_role("region", name="数据管理", exact=True)
                    trash = management.locator(".diet-trash-row").filter(has_text=name)
                    expect(management).to_have_attribute("aria-busy", "false")
                    more_trash = management.get_by_role("button", name="更多回收站记录", exact=True)
                    while not trash.count() and more_trash.count():
                        expect(management).not_to_contain_text("此范围没有已删除记录。")
                        with page.expect_response(
                            lambda response: (
                                "/diet/logs?" in response.url and "cursor=" in response.url
                            )
                        ):
                            more_trash.click()
                        page.wait_for_load_state("networkidle")
                        expect(
                            management.get_by_role("button", name="刷新数据管理")
                        ).to_be_enabled()
                    expect(trash).to_be_visible()
                    with page.expect_response(
                        lambda response: (
                            response.url.endswith("/diet/exports")
                            and response.request.method == "POST"
                        )
                    ) as submitted:
                        page.get_by_role("button", name="创建导出", exact=True).click()
                    task_id = submitted.value.json()["id"]
                    download_button = management.locator(f'[data-job-id="{task_id}"]').get_by_role(
                        "button", name="下载ZIP", exact=True
                    )
                    expect(download_button).to_be_visible(timeout=15000)
                    with page.expect_download() as event:
                        download_button.click()
                    destination = args.output_dir / f"{width}-diet.zip"
                    event.value.save_as(destination)
                    with zipfile.ZipFile(destination) as archive:
                        manifest = json.loads(archive.read("manifest.json"))
                        assert manifest["complete"]
                        assert name in archive.read("sources.json").decode()
                    trash.get_by_role("checkbox").check()
                    management.get_by_role("button", name="核对永久清除（1）", exact=True).click()
                    confirmation = page.get_by_role("dialog", name="确认永久清除", exact=True)
                    expect(
                        confirmation.get_by_role("button", name="永久清除", exact=True)
                    ).to_be_disabled()
                    confirmation.get_by_label("我了解此操作不可恢复", exact=True).check()
                    confirmation.get_by_role("button", name="永久清除", exact=True).click()
                    expect(confirmation).not_to_be_visible()
                    expect(trash).to_have_count(0)
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    expect(page.get_by_role("region", name="数据管理", exact=True)).to_be_visible()
                    expect(page.locator(".diet-trash-row").filter(has_text=name)).to_have_count(0)
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                    assert not errors, errors
                    page.screenshot(
                        path=str(args.output_dir / f"{width}-privacy.png"), full_page=True
                    )
                    results.append(
                        f"{width}x{height}: export ZIP, explicit purge, refresh, no overflow/errors"
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
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
