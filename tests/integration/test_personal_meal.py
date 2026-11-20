from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from nutripilot.api.app import create_app
from nutripilot.application import build_container
from nutripilot.config import Settings
from nutripilot.model_gateway import FakeModelGateway
from nutripilot.tools.errors import ToolUnavailableError

A = "a" * 32
B = "b" * 32


def test_personal_recommendation_context_portion_and_plan(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    local_day = now.astimezone(ZoneInfo("Asia/Shanghai")).date()
    day = local_day.isoformat()
    gateway = FakeModelGateway()
    container = build_container(
        gateway=gateway, settings=Settings(
            _env_file=None, app_mode="test", user_timezone="Asia/Shanghai",
            api_access_tokens={A: 1, B: 2},
            api_rate_limit_per_minute=500,
            diet_deletion_journal_root=str(tmp_path / "deletions"),
        ),
    )
    for service in (
        container.diet, container.diet.intake, container.diet.plans,
        container.diet.queries, container.diet.records,
    ):
        service.clock = lambda: now
    auth = {"Authorization": f"Bearer {A}"}
    other = {"Authorization": f"Bearer {B}"}
    evidence = [{
        "field": "operation", "source_kind": "user_form", "source_ref": "test",
        "original_text": "fixture", "operation": "set", "status": "explicit",
    }]
    with TestClient(create_app(container)) as client:
        def read(path: str) -> dict:
            response = client.get("/api/v1/diet" + path, headers=auth)
            assert response.is_success, response.text
            return response.json()

        def write(path: str, body: dict, *, version=None, key=None) -> dict:
            headers = {**auth, "Idempotency-Key": key or str(uuid4())}
            if version is not None:
                headers["If-Match"] = f'"{version}"'
            response = client.post("/api/v1/diet" + path, json=body, headers=headers)
            assert response.is_success, response.text
            return response.json()

        def confirm(value: dict) -> dict:
            return write(f"/previews/{value['id']}/confirm", {
                "approved": True, "preview_version": value["version"],
                "content_hash": value.get("content_hash") or value["preview_hash"],
            }, version=value["version"])

        def recommend(text: str) -> dict:
            response = client.post("/api/v1/chat", headers=auth, json={
                "thread_id": thread_id, "message": text,
            })
            assert response.is_success, response.text
            return response.json()["final_response"]

        def latest_context() -> dict:
            calls = [call for call in gateway.call_history if call["worker"] == "personal_meal"]
            return calls[-1]["payload"]["tool_results"][0]["data"]

        thread_id = str(uuid4())
        target = write("/previews", {
            "operation": "target_set", "arguments": {
                "action": "set", "effective_date": day,
                "goals": {"calories_kcal": {"lower": "1800", "upper": "2000"}},
                "evidence": evidence,
            },
        })
        target_ref = confirm(target)["result_refs"][0]
        scope = f"start_date={day}&end_date={day}"
        final = recommend("今晚吃什么")
        assert final["personal_meal_options"], final
        assert "个人份量" in final["message"]
        assert any("没有可用的实际摄入" in warning for warning in final["warnings"])
        assert latest_context()["actual"]["state"] == "no_records"
        assert latest_context()["conditional_gap"] == {}
        assert latest_context()["target"]["calories_kcal"]["lower"] == "1800"
        assert read(f"/logs?{scope}")["items"] == []
        assert read(f"/plans?{scope}")["items"] == []
        option = final["personal_meal_options"][0]
        selection = {
            "thread_id": thread_id, "turn_id": option["turn_id"], "option_id": option["id"],
            "shares": ["0.25"] * len(option["items"]),
        }
        denied = client.post(
            "/api/v1/diet/recommendation-options/calculate", headers=other, json=selection,
        )
        assert denied.status_code == 404
        adjusted = write("/recommendation-options/calculate", selection)
        total = adjusted["nutrition"]["nutrients"]["calories_kcal"]["total"]
        original = option["nutrition"]["nutrients"]["calories_kcal"]["total"]
        assert Decimal(total["upper"]) < Decimal(original["upper"])
        plan = write("/recommendation-options/preview", {
            **selection, "meal_type": "dinner",
            "parent": {"kind": "new", "title": "个人份量测试", "start_date": day},
        })
        assert read(f"/plans?{scope}")["items"] == []
        unrelated = write("/previews", {
            "operation": "day_status_set",
            "arguments": {
                "local_date": (local_day - timedelta(days=1)).isoformat(),
                "status": "no_intake", "base_version": 1,
            },
        }, version=1)
        confirm(unrelated)
        receipt = confirm(plan)
        assert confirm(plan)["id"] == receipt["id"]
        meal = plan["content"]["after_meals"][0]
        replaced = recommend("换一组")
        assert replaced["personal_meal_options"], replaced
        assert {
            item["meal_id"] for entry in final["personal_meal_options"] for item in entry["items"]
        }.isdisjoint(
            item["meal_id"]
            for entry in replaced["personal_meal_options"]
            for item in entry["items"]
        )
        assert latest_context()["planned_meals"][0]["recorded_count"] == 0
        assert latest_context()["actual"]["state"] == "no_records"
        actual = write("/drafts", {
            "mode": "structured", "channel": "plan",
            "source": {
                "kind": "plan", "planned_meal_id": meal["id"], "base_version": meal["version"],
                "record_again": False,
            },
            "entry": {
                "subject": "self", "actuality": "actual",
                "temporal": {"local_date": day, "meal_type": "dinner"},
                "items": [{
                    "client_item_key": item["client_item_key"], "food": item["food_ref"],
                    "quantity": item["quantity"],
                } for item in meal["items"]],
            },
            "evidence": evidence,
        })
        saved_actual = confirm(actual)
        after_intake = recommend("今晚吃什么")
        context = latest_context()
        assert context["actual"]["record_count"] == 1
        assert context["actual"]["day_status"] == "open"
        assert context["planned_meals"][0]["recorded_count"] == 1
        assert context["conditional_gap"]["calories_kcal"]["upper"] == str(
            Decimal("2000") - Decimal(total["lower"])
        )
        assert "actual_refs" not in context
        assert "target_ref" not in context
        confirm(write("/previews", {
            "operation": "target_correct", "arguments": {
                "action": "correct", "target_id": target_ref["id"],
                "base_version": target_ref["version"], "effective_date": day,
                "goals": {"calories_kcal": {"lower": "1900", "upper": "2100"}},
                "evidence": evidence,
            },
        }, version=target_ref["version"]))
        assert recommend("今晚吃什么")["personal_meal_options"]
        assert latest_context()["conditional_gap"]["calories_kcal"]["upper"] == str(
            Decimal("2100") - Decimal(total["lower"])
        )
        replacement = after_intake["personal_meal_options"][0]
        parent = plan["content"]["after_plan"]
        existing = {
            "thread_id": thread_id, "turn_id": replacement["turn_id"],
            "option_id": replacement["id"], "shares": ["0.5"] * len(replacement["items"]),
            "meal_type": "dinner",
            "parent": {"kind": "existing", "plan_id": parent["id"],
                       "base_version": parent["version"]},
        }
        conflict = client.post("/api/v1/diet/recommendation-options/preview",
                               json=existing, headers={**auth, "Idempotency-Key": str(uuid4())})
        assert conflict.status_code == 409
        key = str(uuid4())
        replace_body = {**existing, "conflict": "replace"}
        preview = write("/recommendation-options/preview", replace_body, key=key)
        assert preview["recommendation_basis_changed"]
        assert "当前已保存记录" in preview["recommendation_note"]
        assert len(preview["content"]["after_meals"][0]["items"]) == len(replacement["items"])
        confirm(preview)
        replay = write("/recommendation-options/preview", replace_body, key=key)
        assert replay["id"] == preview["id"] and replay["state"] == "committed"
        record = saved_actual["result_refs"][0]
        logged = read(f"/logs/{record['id']}")
        correction = write("/previews", {
            "operation": "log_correct",
            "arguments": {
                "action": "correct", "record_id": record["id"],
                "base_version": record["version"], "evidence": evidence,
                "patch": {"item_edits": [{
                    "action": "update", "item_id": logged["items"][0]["id"],
                    "changes": {"quantity": {"kind": "unknown", "description": "份量未测"}},
                }]},
            },
        }, version=record["version"])
        confirm(correction)
        assert recommend("今晚吃什么")["personal_meal_options"]
        corrected = latest_context()
        assert corrected["actual"]["record_count"] == 1
        assert corrected["actual"]["nutrients"]["calories_kcal"]["total"] is None
        assert "calories_kcal" not in corrected["conditional_gap"]
        recommend("明天晚餐吃什么")
        assert latest_context()["local_date"] == (local_day + timedelta(days=1)).isoformat()
        assert latest_context()["actual"] is None
        before = len([call for call in gateway.call_history if call["worker"] == "personal_meal"])
        recommend("给朋友推荐几道晚餐菜")
        assert len([
            call for call in gateway.call_history if call["worker"] == "personal_meal"
        ]) == before
        thread_id = str(uuid4())
        original = recommend("今晚吃什么")["personal_meal_options"][0]
        half = recommend("把第一份的份量减半")
        assert half["personal_meal_options"], half
        assert [item["meal_id"] for item in half["personal_meal_options"][0]["items"]] == [
            item["meal_id"] for item in original["items"]
        ]
        assert [Decimal(item["quantity"]["share"]) * 2
                for item in half["personal_meal_options"][0]["items"]] == [
            Decimal(item["quantity"]["share"]) for item in original["items"]
        ]
        save = recommend("把第一份加入计划")
        assert save["status"] == "confirmation_required", save
        assert len(read(f"/plans?{scope}")["items"]) == 1
        pending_id = save["diet_context"]["object_ids"][0]
        pending = read(f"/previews/{pending_id}")
        deletion = write("/previews", {
            "operation": "log_delete",
            "arguments": {
                "action": "delete", "record_id": record["id"],
                "base_version": 2, "evidence": evidence,
            },
        }, version=2)
        confirm(deletion)
        purge = write("/purge/previews", {"record_ids": [record["id"]]})
        job = write(f"/purge/previews/{purge['id']}/confirm", {
            "approved": True,
            "preview_version": purge["version"],
            "content_hash": purge["content_hash"],
        }, version=purge["version"])
        deadline = monotonic() + 10
        while (job := read(f"/jobs/{job['id']}"))["state"] in ("queued", "running"):
            assert monotonic() < deadline, job
            sleep(0.1)
        assert job["state"] == "succeeded", job
        stale = client.post(f"/api/v1/diet/previews/{pending_id}/confirm", json={
            "approved": True, "preview_version": pending["version"],
            "content_hash": pending["content_hash"],
        }, headers={**auth, "If-Match": f'"{pending["version"]}"',
                    "Idempotency-Key": str(uuid4())})
        assert stale.status_code == 409
        after_purge = recommend("今晚吃什么")
        assert after_purge["personal_meal_options"], after_purge
        assert latest_context()["actual"]["state"] == "no_records"
        assert latest_context()["conditional_gap"] == {}

        food_preview = write("/previews", {
            "operation": "personal_food_create", "arguments": {
                "action": "create", "data": {
                    "name": "验收用燕麦饮", "food_state": "as_sold", "basis": "per_serving",
                    "source": "manual_label", "source_note": "合成标签测试",
                    "nutrients": {
                        "calories_kcal": {"lower": "100", "upper": "110"},
                        **dict.fromkeys([
                            "protein_g", "fat_g", "carbohydrates_g", "fiber_g", "sodium_mg",
                        ]),
                    },
                },
            },
        })
        food_ref = confirm(food_preview)["result_refs"][0]
        profile = client.put("/api/v1/profile", headers=auth, json={
            "allergens": [], "dislikes": [],
        })
        assert profile.is_success, profile.text
        gateway.queue_response("personal_meal", {
            "options": [[{"candidate_id": -999, "share": "0.5"}]],
        })
        gateway.queue_response("personal_meal", {
            "options": [[{"candidate_id": -1, "share": "0.5"},
                         {"candidate_id": -2, "share": "0.5"}]],
        })
        thread_id = str(uuid4())
        combo = recommend("今晚吃什么？也用上验收用燕麦饮")["personal_meal_options"][0]
        assert [item["food"]["kind"] for item in combo["items"]] == [
            "ingredient", "personal_food",
        ], [
            (call["payload"]["dietary_constraints"], [
                candidate["name"]
                for candidate in call["payload"]["tool_results"][-1]["data"]["personal_portions"]
            ])
            for call in gateway.call_history if call["worker"] == "personal_meal"
        ][-2:]
        assert combo["items"][0]["quantity"] == {
            "kind": "mass", "grams": "100", "food_state": "cooked",
        }
        assert combo["items"][1]["quantity"]["kind"] == "servings"
        combo_selection = {
            "thread_id": thread_id, "turn_id": combo["turn_id"], "option_id": combo["id"],
            "amounts": ["150", "0.5"],
        }
        recalculated = write("/recommendation-options/calculate", combo_selection)
        assert recalculated["nutrition"]["nutrients"]["calories_kcal"]["total"] == {
            "lower": "245", "upper": "250",
        }
        combo_plan = write("/recommendation-options/preview", {
            **combo_selection, "meal_type": "dinner",
            "parent": {"kind": "new", "title": "熟重与份数", "start_date": day},
        })
        assert [
            item["quantity"]["kind"] for item in combo_plan["content"]["after_meals"][0]["items"]
        ] == ["mass", "servings"]
        confirm(write("/previews", {
            "operation": "personal_food_delete", "arguments": {
                "action": "delete", "food_id": food_ref["id"], "base_version": food_ref["version"],
            },
        }, version=food_ref["version"]))
        stale_food = client.post(
            f"/api/v1/diet/previews/{combo_plan['id']}/confirm",
            json={"approved": True, "preview_version": combo_plan["version"],
                  "content_hash": combo_plan["content_hash"]},
            headers={**auth, "If-Match": f'"{combo_plan["version"]}"',
                     "Idempotency-Key": str(uuid4())},
        )
        assert stale_food.status_code == 409, stale_food.text

        registry = container.tool_executor.registry
        tool = registry.require("get_recommendation_context")

        async def unavailable(_arguments, _context):
            raise ToolUnavailableError()

        unavailable.database_idempotency = True
        registry.register(replace(tool, handler=unavailable), replace=True)
        thread_id = str(uuid4())
        degraded = recommend("今晚吃什么")
        assert degraded["personal_meal_options"]
        assert any("未能读取" in warning for warning in degraded["warnings"])
        assert all("差额" not in option["reason"] for option in degraded["personal_meal_options"])
