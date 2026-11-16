"""Real Qwen conversation semantics through the persistent local API and Worker."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

CASES = [
    ("C01", ["我今天午餐吃了一碗米饭，帮我记下来。", "米饭是150克熟重。"], "revise"),
    ("C02", ["如果我明天午餐吃一碗米饭，会怎样？只是设想，并没有吃。"], "no_actual"),
    ("C03", ["今天午餐爸爸吃了一碗米饭，我没吃，别记成我的。"], "no_actual"),
    (
        "C04",
        ["我今天午餐吃了一碗米饭，帮我记下来。", "撤回刚才的话，我没吃米饭，不要记。"],
        "discard",
    ),
    ("C05", ["我今天午餐吃了100克生重苹果，喝了一杯牛奶，没有奶的重量。帮我记录。"], "two_items"),
    (
        "C06",
        ["我今天吃了一个苹果，重量不知道，帮我记录。", "是午餐，份量保留未知。"],
        "missing_meal",
    ),
    (
        "C07",
        ["我今天午餐吃了半盘青菜，两人分着吃，我吃多少克不知道。帮我记录。"],
        "unknown_quantity",
    ),
    ("C08", ["我今天午餐吃了鸡胸肉100克，没说是生重还是熟重，先按未知状态记。"], "state_unknown"),
    (
        "C09",
        [
            "我今天午餐吃了100克生重苹果和一杯牛奶，奶没称。帮我记。",
            "只把苹果改成120克生重，牛奶不要变。",
            "把这份草稿里的牛奶删掉，只保留苹果。",
        ],
        "edit_remove",
    ),
    (
        "C10",
        [
            "我今天午餐吃了两份不同来源的苹果，第一份50克生重，第二份80克生重，分别记录。",
            "把那个苹果改成100克。",
        ],
        "ambiguity",
    ),
    ("C12", ["我今天午餐吃了苹果，份量未知，帮我记录。", "你好，我们先聊点别的。"], "retained"),
    (
        "C13",
        [
            "我今天午餐吃了150克熟米饭，帮我记。",
            "我今天晚餐另外吃了一个苹果，重量不知道，另外记一餐。",
        ],
        "new_meal",
    ),
    ("C14", ["我今天午餐吃了苹果，份量未知，帮我记录。", "好"], "no_commit"),
    ("C18", ["查询我今天实际记录的营养汇总，只查询，不修改。"], "query"),
    (
        "C21",
        [
            "我今天午餐吃了苹果，明天午餐想吃豆腐，还想从明天设置热量目标1800千卡，先分别核对，不要直接保存。"
        ],
        "no_commit",
    ),
]


def check(kind: str, replies: list[dict[str, Any]], drafts: list[dict[str, Any]]) -> None:
    assert all(reply.get("phase") != "FAILED" for reply in replies), "Conversation degraded."
    if kind == "no_actual":
        assert not any(
            draft["state"] == "ready"
            and draft["content"]["entry"]["subject"] == "self"
            and draft["content"]["entry"]["actuality"] == "actual"
            for draft in drafts
        )
        return
    if kind in ("query", "no_commit"):
        return
    assert drafts, "No authoritative draft was created."
    first, last = drafts[0], drafts[-1]
    items = last["content"]["items"]
    entry = last["content"]["entry"]
    if kind == "revise":
        assert first["id"] == last["id"] and last["version"] > first["version"]
        assert items[0]["quantity"] == {"kind": "mass", "grams": "150", "food_state": "cooked"}
    elif kind == "discard":
        assert last["state"] == "discarded"
    elif kind == "two_items":
        assert len(items) == 2
        apple = next(item for item in items if "苹果" in item["name_snapshot"])
        milk = next(item for item in items if "牛奶" in item["name_snapshot"])
        assert apple["quantity"]["grams"] == "100" and milk["quantity"]["kind"] == "unknown"
    elif kind == "missing_meal":
        assert first["state"] == "needs_input" and first["id"] == last["id"]
        assert entry["temporal"]["meal_type"] == "lunch"
        assert (
            entry["temporal"]["local_date"]
            == first["content"]["entry"]["temporal"]["local_date"]
        )
        assert last["state"] == "ready", "Supplying a meal must retain the existing date."
    elif kind == "unknown_quantity":
        assert all(item["quantity"]["kind"] == "unknown" for item in items)
    elif kind == "state_unknown":
        assert items[0]["quantity"]["food_state"] == "unknown"
    elif kind == "edit_remove":
        assert first["id"] == last["id"] and len(items) == 1
        assert "苹果" in items[0]["name_snapshot"] and items[0]["quantity"]["grams"] == "120"
    elif kind == "ambiguity":
        assert last["version"] == first["version"], "Ambiguous edit changed a draft."
    elif kind == "retained":
        assert last["id"] == first["id"] and last["state"] in ("ready", "needs_input")
    elif kind == "new_meal":
        assert first["id"] != last["id"]
        assert entry["temporal"]["meal_type"] == "dinner" and len(items) == 1
        assert "苹果" in items[0]["name_snapshot"] and items[0]["quantity"]["kind"] == "unknown"


async def lifecycle(client: httpx.AsyncClient) -> dict[str, Any]:
    thread = f"real-lifecycle-{uuid4().hex}"
    row: dict[str, Any] = {"id": "C16-C17-C19-C22-C23", "thread_id": thread, "passed": False}

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    try:
        prepared = await request(
            "POST",
            "/api/v1/chat",
            json={
                "user_id": 2,
                "thread_id": thread,
                "message": "我今天午餐吃了苹果，份量未知，帮我记录。",
            },
        )
        row["prepare"] = prepared
        pending = prepared["pending_action"]["payload"]
        draft_id = pending["preview_id"]
        row["draft_id"] = draft_id
        confirmation = await request(
            "POST",
            "/api/v1/chat",
            json={
                "user_id": 2,
                "thread_id": thread,
                "message": "确认保存",
            },
        )
        assert confirmation["phase"] == "COMPLETED"
        draft = await request("GET", f"/api/v1/diet/drafts/{draft_id}")
        receipt = await request("GET", f"/api/v1/diet/receipts/{draft['receipt_id']}")
        log_id = next(ref["id"] for ref in receipt["result_refs"] if ref["kind"] == "log")
        row["log_id"] = log_id
        replay = await request(
            "POST",
            f"/api/v1/diet/previews/{draft_id}/confirm",
            headers={"Idempotency-Key": uuid4().hex, "If-Match": f'"{pending["preview_version"]}"'},
            json={
                "approved": True,
                "preview_version": pending["preview_version"],
                "content_hash": pending["content_hash"],
            },
        )
        assert replay == receipt
        unauthorized = await client.get(
            f"/api/v1/diet/logs/{log_id}",
            headers={"Authorization": ""},
        )
        assert unauthorized.status_code == 401
        trace = await request("GET", f"/api/v1/traces/{prepared['trace_id']}")
        assert "苹果" not in json.dumps(trace, ensure_ascii=False)
        deletion = await request(
            "POST",
            "/api/v1/chat",
            json={
                "user_id": 2,
                "thread_id": thread,
                "message": f"请删除已保存的这条正式饮食记录，记录ID是{log_id}，先给我删除预览。",
            },
        )
        row["delete_prepare"] = deletion
        assert deletion["requires_confirmation"]
        confirmed = await request(
            "POST",
            "/api/v1/chat",
            json={
                "user_id": 2,
                "thread_id": thread,
                "message": "确认删除",
            },
        )
        assert confirmed["phase"] == "COMPLETED"
        record = await request("GET", f"/api/v1/diet/logs/{log_id}")
        assert record["deleted_at"] is not None
        query = await request(
            "POST",
            "/api/v1/chat",
            json={
                "user_id": 2,
                "thread_id": thread,
                "message": "查询我今天的实际饮食记录，不包括已删除记录。",
            },
        )
        row["query_after_delete"] = query
        assert query["phase"] != "FAILED"
        after = await request("GET", f"/api/v1/diet/logs/{log_id}")
        assert after["deleted_at"] is not None
        row["passed"] = True
    except Exception as exc:
        row["error"] = type(exc).__name__
        row["message"] = str(exc)[:1000]
    print(f"lifecycle: {'pass' if row['passed'] else 'fail'} {row.get('message', '')}", flush=True)
    return row


async def management_lifecycle(client: httpx.AsyncClient) -> dict[str, Any]:
    row: dict[str, Any] = {"id": "plans-targets-deviation", "passed": False, "replies": []}
    thread = f"real-management-{uuid4().hex}"
    row["thread_id"] = thread

    async def request(method: str, path: str, **kwargs: Any) -> Any:
        response = await client.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    async def chat(message: str, tool: str | None = None) -> Any:
        reply = await request(
            "POST", "/api/v1/chat",
            json={"user_id": 2, "thread_id": thread, "message": message},
        )
        row["replies"].append({"input": message, **reply})
        assert reply["phase"] != "FAILED", reply["final_response"]["message"]
        if tool:
            trace = await request("GET", f"/api/v1/traces/{reply['trace_id']}")
            assert any(
                event.get("component") == tool and event.get("event_type") == "tool.completed"
                for event in trace["events"]
            ), f"Expected real execution of {tool}."
        return reply

    try:
        context = await request("GET", "/api/v1/diet/context")
        tomorrow = date.fromisoformat(context["anchor"]["anchor_local_date"]) + timedelta(days=1)
        targets = await request(
            "GET", "/api/v1/diet/targets",
            params={"start_date": str(tomorrow), "end_date": str(tomorrow + timedelta(days=100))},
        )
        occupied = {item["target"]["effective_date"] for item in targets["items"]}
        while str(tomorrow) in occupied:
            tomorrow += timedelta(days=1)
        row["future_date"] = str(tomorrow)
        calorie_lower = "1850"
        if targets["items"]:
            previous = targets["items"][-1]["target"]["goals"]["calories_kcal"]
            if previous is not None and previous["lower"] == calorie_lower:
                calorie_lower = "1900"
        calorie_upper = str(int(calorie_lower) + 200)
        plan = await chat(
            f"请在饮食管理里新建一份名为验收餐单的计划，从{tomorrow}开始，"
            f"只安排{tomorrow}午餐吃一个苹果，份量未知。这是未来计划，尚未食用，先准备餐单预览。",
            "prepare_meal_plan_change",
        )
        pending = plan["pending_action"]["payload"]
        preview = await request("GET", f"/api/v1/diet/previews/{pending['preview_id']}")
        assert preview["content"]["operation"] == "plan_create"
        meals = preview["content"]["after_meals"]
        assert len(meals) == 1 and meals[0]["meal_type"] == "lunch"
        assert meals[0]["local_date"] == str(tomorrow)
        assert all(item["quantity"]["kind"] == "unknown" for item in meals[0]["items"])
        assert (await chat("确认保存"))["phase"] == "COMPLETED"
        target = await chat(
            f"从{tomorrow}起设置每日热量目标{calorie_lower}到{calorie_upper}千卡，"
            "蛋白质目标80到100克。"
            "这是独立的营养目标变更，先准备目标预览。",
            "prepare_target_change",
        )
        preview = await request(
            "GET", f"/api/v1/diet/previews/{target['pending_action']['payload']['preview_id']}"
        )
        goals = preview["content"]["after"]["goals"]
        assert goals["calories_kcal"] == {"lower": calorie_lower, "upper": calorie_upper}
        assert goals["protein_g"] == {"lower": "80", "upper": "100"}
        assert (await chat("确认设置"))["phase"] == "COMPLETED"
        await chat(f"只查询我{tomorrow}的饮食餐单计划。", "get_meal_plans")
        await chat(f"只查询我{tomorrow}生效的每日营养目标。", "get_nutrition_targets")
        deviation = await chat(
            f"只查询我{tomorrow}实际摄入与计划的偏差和计划执行情况，不修改。",
            "get_diet_deviation",
        )
        assert "不可比较" in deviation["final_response"]["message"]
        row["passed"] = True
    except Exception as exc:
        row["error"], row["message"] = type(exc).__name__, str(exc)[:1000]
    print(
        f"management: {'pass' if row['passed'] else 'fail'} {row.get('message', '')}", flush=True
    )
    return row


async def run(args: argparse.Namespace) -> None:
    environment = json.loads(args.environment.read_text())
    tokens = json.loads(environment["API_ACCESS_TOKENS"])
    token = next(key for key, user in tokens.items() if user == 2)
    results: list[dict[str, Any]] = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(2)
    async with httpx.AsyncClient(
        base_url=args.api_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=150,
        trust_env=False,
    ) as client:

        async def request(method: str, path: str, **kwargs: Any) -> Any:
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        context = await request("GET", "/api/v1/diet/context")
        today = context["anchor"]["anchor_local_date"]
        log_path = f"/api/v1/diet/logs?start_date={today}&end_date={today}&limit=50"
        before = await request("GET", log_path)

        async def sample(case: tuple[str, list[str], str]) -> dict[str, Any]:
            case_id, messages, kind = case
            thread = f"real-{case_id}-{uuid4().hex}"
            row: dict[str, Any] = {
                "id": case_id,
                "thread_id": thread,
                "passed": False,
                "replies": [],
                "drafts": [],
            }
            async with semaphore:
                try:
                    active_id = None
                    for message in messages:
                        reply = await request(
                            "POST",
                            "/api/v1/chat",
                            json={
                                "user_id": 2,
                                "thread_id": thread,
                                "message": message,
                            },
                        )
                        row["replies"].append({"input": message, **reply})
                        pending = reply.get("pending_action") or {}
                        active_id = (pending.get("payload") or {}).get("preview_id") or active_id
                        context_ids = (
                            reply.get("final_response", {}).get("diet_context") or {}
                        ).get(
                            "object_ids",
                            [],
                        )
                        if context_ids:
                            active_id = context_ids[0]
                        snapshot = await request("GET", f"/api/v1/threads/{thread}/snapshot")
                        state = snapshot.get("state") or snapshot.get("checkpoint") or {}
                        active_id = (state.get("active_draft_ref") or {}).get("id") or active_id
                        if not active_id:
                            drafts = await request("GET", "/api/v1/diet/drafts?limit=50")
                            match = next(
                                (d for d in drafts["items"] if d["thread_id"] == thread), None
                            )
                            active_id = match["id"] if match else None
                        if active_id:
                            draft = await request("GET", f"/api/v1/diet/previews/{active_id}")
                            if "content" in draft and "entry" in draft["content"]:
                                row["drafts"].append(draft)
                    check(kind, row["replies"], row["drafts"])
                    row["passed"] = True
                except Exception as exc:
                    row["error"] = type(exc).__name__
                    row["message"] = str(exc)[:1000]
                print(
                    f"{case_id}: {'pass' if row['passed'] else 'fail'} {row.get('message', '')}",
                    flush=True,
                )
                results.append(row)
                args.output.write_text(
                    json.dumps(
                        {
                            "model": "qwen-plus",
                            "scope": "Real API/Worker proposals; no automatic approvals.",
                            "passed": len(results) == len(CASES)
                            and all(row["passed"] for row in results),
                            "results": results,
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
            return row

        await asyncio.gather(*(sample(case) for case in CASES))
        after = await request("GET", log_path)
        assert [item["id"] for item in before["items"]] == [item["id"] for item in after["items"]]
        results.append(await lifecycle(client))
        results.append(await management_lifecycle(client))
        args.output.write_text(
            json.dumps(
                {
                    "model": "qwen-plus",
                    "scope": "Real local API/Worker semantic and lifecycle acceptance.",
                    "passed": all(row["passed"] for row in results),
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8025")
    parser.add_argument(
        "--environment", type=Path, default=Path(".runtime/local-diet/environment.json")
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".runtime/diet-acceptance-20261112/real-final/chat-matrix.json"),
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
