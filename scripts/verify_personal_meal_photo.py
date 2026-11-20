"""Confirm one sourced photo estimate and exercise the next-meal flow on a local test account."""

import argparse
from pathlib import Path
from time import monotonic, sleep
from uuid import uuid4

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8029")
    parser.add_argument("--photo", required=True, type=Path)
    args = parser.parse_args()
    prefix = "/api/v1/diet"
    with httpx.Client(base_url=args.api_url, timeout=100) as client:
        def request(method: str, path: str, *, version: int | None = None, **kwargs):
            headers = {"Idempotency-Key": uuid4().hex}
            if version is not None:
                headers["If-Match"] = f'"{version}"'
            response = client.request(method, path, headers=headers, **kwargs)
            assert response.is_success, (response.status_code, response.text[:800])
            return response.json()

        asset = request(
            "POST", f"{prefix}/media", data={"kind": "intake_image"},
            files={"files": ("rice.jpg", args.photo.read_bytes(), "image/jpeg")},
        )["items"][0]
        photo = {"media_ids": [asset["id"]], "send_images_authorized": True}
        draft = request("POST", f"{prefix}/drafts", json={
            "mode": "image", "caption": "这是我今天午餐吃的米饭，请用照片粗略估算。", **photo,
        })
        draft_path = f"{prefix}/drafts/{draft['id']}"
        job = request(
            "POST", f"{draft_path}/parse", version=draft["version"],
            json={"kind": "image", **photo},
        )
        deadline = monotonic() + 100
        while job["state"] in ("queued", "running") and monotonic() < deadline:
            sleep(0.5)
            job = request("GET", f"{prefix}/jobs/{job['id']}")
        assert job["state"] == "succeeded", (job["state"], job["error_code"])
        draft = request("GET", draft_path)
        if draft["state"] == "needs_input" and draft["content"]["items"]:
            draft = request("PATCH", draft_path, version=draft["version"], json={
                "patch": {"temporal": {
                    "meal_type": "lunch", "local_time": None, "utc_offset_minutes": None,
                }},
                "user_edit": {"subject": "self", "actuality": "actual"},
                "evidence": [{
                    "field": "actuality", "source_kind": "user_form", "source_ref": "test-review",
                    "original_text": "这是我今天午餐吃的米饭", "operation": "set",
                    "status": "explicit",
                }],
            })
        assert draft["state"] == "ready", draft.get("missing_fields")
        receipt = request(
            "POST", f"{prefix}/previews/{draft['id']}/confirm", version=draft["version"],
            json={"approved": True, "preview_version": draft["version"],
                  "content_hash": draft["preview_hash"]},
        )
        record = request("GET", f"{prefix}/logs/{receipt['result_refs'][0]['id']}")
        bases = [item["nutrition_snapshot"]["calories_kcal"]["basis"] for item in record["items"]]
        assert "model_estimate" in bases, bases
        owner = request("GET", "/api/v1/profile")["user_id"]
        result = request("POST", "/api/v1/chat", json={
            "user_id": owner, "thread_id": uuid4().hex,
            "message": "结合今天吃的，今晚吃什么？最好有熟米饭和一道菜。",
        })["final_response"]
        assert result["personal_meal_options"], result
        assert any("照片模型估算" in warning for warning in result["warnings"]), result
        assert any(
            {"recipe", "ingredient"} <= {item["food"]["kind"] for item in option["items"]}
            for option in result["personal_meal_options"]
        ), result
        print({
            "vision": draft["image_estimation"]["model_name"], "bases": bases,
            "recommendation": result["status"],
            "options": [
                [(item["name"], item["quantity"]["kind"]) for item in option["items"]]
                for option in result["personal_meal_options"]
            ],
        })


if __name__ == "__main__":
    main()
