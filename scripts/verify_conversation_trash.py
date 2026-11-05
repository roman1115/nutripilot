"""Verify conversation trash interactions against an isolated Fake API."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from playwright.sync_api import expect, sync_playwright
from scripts.verify_web import navigate_view, verify_chat_navigation


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:5178")
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--executable", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(".runtime/conversation-trash-web"))
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    checks: list[str] = []
    errors: list[str] = []
    dialogs: list[str] = []
    with httpx.Client(base_url=args.api_url, trust_env=False, timeout=30) as client:
        assert client.get("/health/ready").json()["model_provider"] == "fake"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, executable_path=str(args.executable) if args.executable else None,
            )
            try:
                for width, height in ((1440, 1000), (390, 844), (320, 740)):
                    user_id = 7_000_000_000 + uuid4().int % 1_000_000_000
                    headers = {"X-User-ID": str(user_id)}
                    thread = uuid4().hex
                    title = f"推荐午餐，回收站验收{width}"
                    created = client.post("/api/v1/chat", headers=headers, json={
                        "user_id": user_id, "thread_id": thread, "message": title,
                    })
                    created.raise_for_status()
                    original = client.get(
                        f"/api/v1/threads/{thread}/snapshot", headers=headers,
                    ).json()
                    context = browser.new_context(viewport={"width": width, "height": height})
                    context.add_init_script(
                        "if (location.protocol.startsWith('http')) {"
                        f"sessionStorage.setItem('nutripilot.userId','{user_id}');"
                        "}"
                    )

                    def forward(route):  # type: ignore[no-untyped-def]
                        url = urlsplit(route.request.url)
                        route.continue_(
                            url=args.api_url + url.path + ("?" + url.query if url.query else ""),
                        )

                    context.route("**/api/v1/**", forward)
                    context.route("**/health/**", forward)
                    page = context.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))

                    def dismiss_dialog(dialog):  # type: ignore[no-untyped-def]
                        dialogs.append(dialog.message)
                        dialog.dismiss()

                    page.on("dialog", dismiss_dialog)
                    page.goto(f"{args.base_url}/?thread={thread}")
                    page.wait_for_load_state("networkidle")
                    expect(page.locator(".message-user")).to_contain_text(title)
                    for selector in (".chat-toolbar", ".composer", ".composer-send"):
                        box = page.locator(selector).bounding_box()
                        assert box and box["x"] >= 0 and box["x"] + box["width"] <= width
                        assert box["y"] + box["height"] <= height
                    trigger = page.get_by_role("button", name="功能导航", exact=True)
                    navigation = page.get_by_role("navigation", name="主导航", exact=True)
                    expect(navigation).not_to_be_visible()
                    assert page.locator(".sidebar, .mobile-bottom-nav").count() == 0
                    workspace_box = page.locator(".workspace").bounding_box()
                    trigger_box = trigger.bounding_box()
                    assert workspace_box and workspace_box["x"] == 0
                    assert trigger_box and trigger_box["x"] > width / 2
                    trigger.focus()
                    page.keyboard.press("Enter")
                    expect(navigation).to_be_visible()
                    expect(navigation.get_by_role(
                        "button", name="对话", exact=True,
                    )).to_have_attribute("aria-current", "page")
                    page.screenshot(path=str(args.output_dir / f"navigation-{width}.png"))
                    page.keyboard.press("Escape")
                    expect(navigation).not_to_be_visible()
                    expect(trigger).to_be_focused()
                    trigger.click()
                    page.get_by_role("textbox", name="发送给 NutriPilot 的消息").click()
                    expect(navigation).not_to_be_visible()
                    for key, label in (
                        ("profile", "饮食偏好"), ("plans", "饮食计划"),
                        ("traces", "执行记录"), ("evaluations", "质量评估"), ("chat", "对话"),
                    ):
                        navigate_view(page, label)
                        expect(page.locator(f".main-content.view-{key}")).to_be_visible()
                        expect(navigation).not_to_be_visible()
                    expect(page.locator(".message-user")).to_contain_text(title)
                    if width < 1100:
                        page.get_by_role("button", name="历史会话", exact=True).click()
                    history = page.get_by_role("region", name="历史会话列表")
                    expect(history).to_be_visible()
                    history_box = history.bounding_box()
                    assert history_box and history_box["x"] == 0
                    if width >= 1100:
                        chat_box = page.locator(".chat-surface").bounding_box()
                        assert chat_box and chat_box["x"] == 260
                    expect(history.get_by_role("button", name=f"删除会话：{title}")).to_be_visible()
                    with page.expect_response(
                        lambda response: response.request.method == "DELETE",
                    ) as deleted:
                        history.get_by_role("button", name=f"删除会话：{title}").click()
                    assert deleted.value.status == 204
                    expect(page.locator(".message-user")).to_have_count(0)
                    assert not dialogs
                    if width < 1100:
                        page.get_by_role("button", name="历史会话", exact=True).click()
                    history.get_by_role("tab", name="回收站", exact=True).click()
                    expect(history.get_by_role("button", name=f"恢复会话：{title}")).to_be_visible()
                    expect(history).to_contain_text("到期删除")
                    page.screenshot(path=str(args.output_dir / f"trash-{width}.png"))
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    if width < 1100:
                        page.get_by_role("button", name="历史会话", exact=True).click()
                    history.get_by_role("tab", name="回收站", exact=True).click()
                    expect(history.get_by_role("button", name=f"恢复会话：{title}")).to_be_visible()
                    history.get_by_role("button", name=f"恢复会话：{title}").click()
                    expect(page.locator(".message-user")).to_contain_text(title)
                    expect(page.locator(".message-assistant")).to_have_count(1)
                    restored = client.get(
                        f"/api/v1/threads/{thread}/snapshot", headers=headers,
                    ).json()
                    assert restored == original
                    assert not client.get("/api/v1/threads/trash", headers=headers).json()["items"]
                    page.reload()
                    page.wait_for_load_state("networkidle")
                    expect(page.locator(".message-user")).to_contain_text(title)
                    if width < 1100:
                        page.get_by_role("button", name="历史会话", exact=True).click()
                    history.get_by_role("tab", name="历史", exact=True).focus()
                    page.keyboard.press("ArrowRight")
                    expect(history.get_by_role("tab", name="回收站")).to_be_focused()
                    expect(history).to_contain_text("回收站为空")
                    page.keyboard.press("Home")
                    expect(history.get_by_role("tab", name="历史")).to_be_focused()
                    expect(history.get_by_role("button", name=f"删除会话：{title}")).to_be_visible()
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= window.innerWidth"
                    )
                    page.screenshot(path=str(args.output_dir / f"restored-{width}.png"))
                    if width < 1100:
                        page.get_by_role("button", name="关闭历史会话", exact=True).click()
                    page.screenshot(path=str(args.output_dir / f"chat-{width}.png"))
                    checks.extend(
                        f"{width}px: {check}" for check in verify_chat_navigation(page)
                    )
                    for viewport in (
                        [(1024, 768), (768, 1024)] if width == 1440
                        else [(844, 390)] if width == 390 else []
                    ):
                        page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
                        expect(page.get_by_role(
                            "textbox", name="发送给 NutriPilot 的消息",
                        )).to_be_in_viewport()
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= window.innerWidth"
                        )
                        trigger.click()
                        expect(navigation).to_be_visible()
                        box = page.locator("#app-navigation").bounding_box()
                        assert box
                        assert box["x"] >= 0 and box["x"] + box["width"] <= viewport[0]
                        assert box["y"] >= 0 and box["y"] + box["height"] <= viewport[1]
                        page.screenshot(
                            path=str(args.output_dir / f"navigation-{viewport[0]}.png"),
                        )
                        page.keyboard.press("Escape")
                        checks.append(f"{viewport[0]}px: menu fits viewport; composer visible")
                    context.close()
                    checks.append(
                        f"{width}px: no confirmation; trash and restore survive refresh; "
                        "snapshot unchanged; keyboard tabs; top-right menu; no horizontal overflow"
                    )
            finally:
                browser.close()
    assert not dialogs, dialogs
    assert not errors, errors
    report = {"status": "passed", "checks": checks, "dialogs": dialogs, "page_errors": errors}
    (args.output_dir / "result.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
