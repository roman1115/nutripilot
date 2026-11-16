"""Frozen HowToCook photos: provenance preparation and authenticated real API acceptance."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import mimetypes
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from PIL import Image, ImageDraw, ImageOps


def blob(repository: Path, commit: str, path: str) -> bytes:
    from urllib.parse import quote

    # The local source is a partial clone; missing blobs must not trigger unbounded git fetches.
    result = subprocess.run(
        ["git", "-C", str(repository), "ls-tree", commit, "--", path],
        check=True,
        capture_output=True,
    ).stdout.decode()
    assert result, f"No source path at frozen commit: {path}"
    expected = result.split()[2]
    cache = repository.parent / "howtocook-blobs" / expected
    if cache.exists():
        content = cache.read_bytes()
    else:
        for attempt in range(3):
            try:
                response = httpx.get(
                    f"https://raw.githubusercontent.com/Anduin2017/HowToCook/{commit}/{quote(path)}",
                    timeout=20,
                    follow_redirects=True,
                )
                response.raise_for_status()
                content = response.content
                break
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt == 2:
                    raise
    assert hashlib.sha1(f"blob {len(content)}\0".encode() + content).hexdigest() == expected
    if not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(content)
    return content


def prepare(dataset_path: Path, repository: Path, output: Path) -> None:
    frozen = dataset_path.read_bytes()
    dataset = json.loads(frozen)
    assert len(dataset["cases"]) == len({case["id"] for case in dataset["cases"]}) == 30
    output.mkdir(parents=True, exist_ok=True)
    commit = dataset["source_commit"]
    license_text = blob(repository, commit, "LICENSE").decode()
    assert "public domain" in license_text.lower()
    paths = blob(repository, commit, "README.md")
    assert paths
    tree = (
        subprocess.run(
            ["git", "-C", str(repository), "ls-tree", "-rz", "--name-only", commit],
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .split("\0")
    )
    sheet = Image.new("RGB", (1000, 6 * 180), "white")
    draw = ImageDraw.Draw(sheet)
    manifest: dict[str, Any] = {
        "dataset_sha256": hashlib.sha256(frozen).hexdigest(),
        "source_commit": commit,
        "license_sha256": hashlib.sha256(license_text.encode()).hexdigest(),
        "cases": [],
        "rights_review": (
            "Repository license retained; recipe credits do not establish image ownership. "
            "Watermarked I07 and commercial package I30 have unverified third-party rights. "
            "Local acceptance only; do not bundle source photos or recipe texts for publication."
        ),
    }
    for index, case in enumerate(dataset["cases"]):
        content = blob(repository, commit, case["path"])
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(content))).convert("RGB")
        original_size = image.size
        image.thumbnail((1600, 1600))
        filename = f"{case['id']}.jpg"
        image.save(output / filename, format="JPEG", quality=90)
        markdown_paths = [
            path
            for path in tree
            if path.startswith(str(Path(case["path"]).parent) + "/") and path.endswith(".md")
        ]
        notes = {}
        for path in markdown_paths:
            text = blob(repository, commit, path).decode()
            notes[path] = text
            (output / f"{case['id']}-source.md").write_text(text)
        thumb = image.copy()
        thumb.thumbnail((190, 150))
        x, y = (index % 5) * 200, (index // 5) * 180
        sheet.paste(thumb, (x, y + 20))
        draw.text((x + 5, y + 3), case["id"], fill="black")
        manifest["cases"].append(
            {
                **case,
                "file": filename,
                "original_size": original_size,
                "submitted_size": image.size,
                "original_sha256": hashlib.sha256(content).hexdigest(),
                "submitted_sha256": hashlib.sha256((output / filename).read_bytes()).hexdigest(),
                "source_markdown": list(notes),
                "transform": "EXIF transpose; RGB; longest edge <=1600; JPEG quality 90",
            }
        )
        print(f"{case['id']}: prepared", flush=True)
    sheet.save(output / "contact-sheet.jpg")
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


def main_food_hit(case: dict[str, Any], names: list[str]) -> bool:
    return any(alias in name for alias in case.get("aliases", []) for name in names)


async def run(args: argparse.Namespace) -> None:
    manifest = json.loads((args.assets / "manifest.json").read_text())
    assert manifest["dataset_sha256"] == hashlib.sha256(args.dataset.read_bytes()).hexdigest()
    environment = json.loads(args.environment.read_text())
    tokens = json.loads(environment["API_ACCESS_TOKENS"])
    token = next(key for key, user in tokens.items() if user == 1)
    other = next(key for key, user in tokens.items() if user == 2)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        base_url=args.api_url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
        trust_env=False,
    ) as client:

        async def request(method: str, path: str, **kwargs: Any) -> Any:
            response = await client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()

        selected = [
            case for case in manifest["cases"] if not args.case_ids or case["id"] in args.case_ids
        ]
        assert selected
        for case in selected:
            started = time.monotonic()
            row: dict[str, Any] = {"id": case["id"], "kind": case["kind"], "passed": False}
            try:
                file = args.assets / case["file"]
                assert hashlib.sha256(file.read_bytes()).hexdigest() == case["submitted_sha256"]
                headers = {"Idempotency-Key": uuid4().hex}
                media = await request(
                    "POST",
                    "/api/v1/diet/media",
                    headers=headers,
                    data={"kind": case["kind"]},
                    files={"files": ("meal.jpg", file.read_bytes(), mimetypes.guess_type(file)[0])},
                )
                media_id = media["items"][0]["id"]
                forbidden = await client.get(
                    f"/api/v1/diet/media/{media_id}/content",
                    headers={"Authorization": f"Bearer {other}"},
                )
                assert forbidden.status_code == 404
                draft = await request(
                    "POST",
                    "/api/v1/diet/drafts",
                    headers={"Idempotency-Key": uuid4().hex},
                    json={
                        "mode": "image",
                        "media_ids": [media_id],
                        "caption": "这是我今天午餐吃的，请估算记餐。"
                        if case["kind"] == "intake_image"
                        else "请读取可见的包装标签。",
                        "send_images_authorized": True,
                    },
                )
                row["draft_id"] = draft["id"]
                job = await request(
                    "POST",
                    f"/api/v1/diet/drafts/{draft['id']}/parse",
                    headers={"Idempotency-Key": uuid4().hex, "If-Match": f'"{draft["version"]}"'},
                    json={"kind": "image", "media_ids": [media_id], "send_images_authorized": True},
                )
                deadline = time.monotonic() + 240
                while job["state"] in ("queued", "running") and time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
                    job = await request("GET", f"/api/v1/diet/jobs/{job['id']}")
                row["job"] = job
                assert job["state"] == "succeeded", job.get("error_code")
                draft = await request("GET", f"/api/v1/diet/drafts/{draft['id']}")
                row["draft"] = draft
                assert draft["receipt_id"] is None, "Parsing must never write actual intake."
                if case["kind"] == "label_image":
                    assert draft["label_model_name"] == "qwen3-vl-plus"
                    assert draft["label_suggestion"] is not None
                    row["label_requires_review"] = True
                else:
                    estimation = draft.get("image_estimation")
                    assert estimation and estimation["model_name"] == "qwen3-vl-plus"
                    names = [item["name_snapshot"] for item in draft["content"]["items"]]
                    row["names"] = names
                    row["main_food_hit"] = main_food_hit(case, names)
                    row["model_ready_without_edits"] = draft["state"] == "ready"
                    if draft["state"] == "needs_input" and draft["content"]["items"]:
                        # The test caption declares self/actual/lunch; the test user reviews it.
                        draft = await request(
                            "PATCH",
                            f"/api/v1/diet/drafts/{draft['id']}",
                            headers={
                                "Idempotency-Key": uuid4().hex,
                                "If-Match": f'"{draft["version"]}"',
                            },
                            json={
                                "patch": {"temporal": {
                                    "meal_type": "lunch",
                                    "local_time": None,
                                    "utc_offset_minutes": None,
                                }},
                                "user_edit": {"subject": "self", "actuality": "actual"},
                                "evidence": [
                                    {
                                        "field": "actuality",
                                        "source_kind": "user_form",
                                        "source_ref": "acceptance-review",
                                        "original_text": "这是我今天午餐吃的",
                                        "operation": "set",
                                        "status": "explicit",
                                    }
                                ],
                            },
                        )
                        row["reviewed_by_test_user"] = True
                    assert draft["state"] == "ready", draft.get("missing_fields")
                    body = {
                        "approved": True,
                        "preview_version": draft["version"],
                        "content_hash": draft["preview_hash"],
                    }
                    path = f"/api/v1/diet/previews/{draft['id']}/confirm"
                    headers = {"Idempotency-Key": uuid4().hex, "If-Match": f'"{draft["version"]}"'}
                    receipt = await request("POST", path, headers=headers, json=body)
                    replay = await request("POST", path, headers=headers, json=body)
                    assert receipt == replay
                    refs = receipt["result_refs"]
                    log_id = next(ref["id"] for ref in refs if ref["kind"] == "log")
                    log = await request("GET", f"/api/v1/diet/logs/{log_id}")
                    assert log["channel"] == "image" and log["items"]
                    row["log_id"] = log_id
                    row["receipt"] = receipt
                    row["saved_items"] = log["items"]
                row["passed"] = True
            except Exception as exc:
                row["error"] = type(exc).__name__
                row["message"] = str(exc)[:500]
                if isinstance(exc, httpx.HTTPStatusError):
                    row["response"] = exc.response.text[:1000]
            row["seconds"] = round(time.monotonic() - started, 3)
            results.append(row)
            report = {
                "dataset_sha256": manifest["dataset_sha256"],
                "source_commit": manifest["source_commit"],
                "model": "qwen3-vl-plus",
                "results": results,
                "passed": len(results) == len(selected) and all(r["passed"] for r in results),
                "scope": "Coarse primary-food references; no measured portions or nutrition truth.",
                "supplier_cost": "unknown",
            }
            args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(
                f"{case['id']}: {'pass' if row['passed'] else 'fail'} {row.get('message', '')}",
                flush=True,
            )
    print(
        json.dumps(
            {
                "executed": len(results),
                "passed": sum(row["passed"] for row in results),
                "main_food_hits": sum(row.get("main_food_hit", False) for row in results),
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--dataset", type=Path, default=Path("data/eval_diet_images_20261112.json"))
    parser.add_argument("--repository", type=Path, default=Path(".runtime/data-sources/HowToCook"))
    parser.add_argument(
        "--assets", type=Path, default=Path(".runtime/diet-acceptance-20261112/photos")
    )
    parser.add_argument(
        "--environment", type=Path, default=Path(".runtime/local-diet/environment.json")
    )
    parser.add_argument("--api-url", default="http://127.0.0.1:8021")
    parser.add_argument("--case-ids", nargs="*")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".runtime/diet-acceptance-20261112/real-final/images.json"),
    )
    args = parser.parse_args()
    if args.prepare:
        prepare(args.dataset, args.repository, args.assets)
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
