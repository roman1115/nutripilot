"""Rebuild the checked-in catalog from pinned, locally downloaded official sources."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from nutripilot.paths import project_root
from nutripilot.retrieval.catalog import FoodCatalog

USDA_URL = "https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_sr_legacy_food_csv_2018-04.zip"
NUTRIENTS = {
    "1008": ("calories_kcal", "KCAL"),
    "1003": ("protein_g", "G"),
    "1005": ("carbohydrates_g", "G"),
    "1004": ("fat_g", "G"),
    "1079": ("fiber_g", "G"),
    "1093": ("sodium_mg", "MG"),
}


def _rows(archive: zipfile.ZipFile, filename: str) -> csv.DictReader[str]:
    matches = [name for name in archive.namelist() if name.endswith("/" + filename)]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one {filename} in USDA archive.")
    return csv.DictReader(io.StringIO(archive.read(matches[0]).decode("utf-8-sig")))


def import_ingredients(
    archive_path: Path, expected_sha256: str, mappings: dict[str, Any]
) -> list[dict[str, Any]]:
    if hashlib.sha256(archive_path.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("USDA archive checksum differs from the pinned source.")
    with zipfile.ZipFile(archive_path) as archive:
        definitions = {row["id"]: row for row in _rows(archive, "nutrient.csv")}
        for nutrient_id, (_field, unit) in NUTRIENTS.items():
            if definitions[nutrient_id]["unit_name"].upper() != unit:
                raise ValueError(f"Unexpected unit for nutrient {nutrient_id}.")
        foods = list(_rows(archive, "food.csv"))
        values: dict[str, dict[str, float]] = {}
        for row in _rows(archive, "food_nutrient.csv"):
            if row["nutrient_id"] not in NUTRIENTS or not row["amount"]:
                continue
            field = NUTRIENTS[row["nutrient_id"]][0]
            target = values.setdefault(row["fdc_id"], {})
            if field in target:
                raise ValueError(f"Duplicate nutrient {field} for food {row['fdc_id']}.")
            target[field] = float(row["amount"])
    ids = {row["fdc_id"] for row in foods}
    if not set(mappings).issubset(ids):
        raise ValueError("An ingredient mapping references an unknown FDC ID.")
    return [
        {
            "id": f"fdc:{row['fdc_id']}",
            "name": mappings.get(row["fdc_id"], {}).get("name", row["description"]),
            "aliases": mappings.get(row["fdc_id"], {}).get("aliases", []),
            "source_id": "usda-sr-legacy-2018-04",
            "source_record_id": row["fdc_id"],
            "source_description": row["description"],
            "source_uri": f"https://fdc.nal.usda.gov/food-details/{row['fdc_id']}/nutrients",
            "nutrients_per_100g": {
                field: values.get(row["fdc_id"], {}).get(field)
                for field, _unit in NUTRIENTS.values()
            },
            "allergens": mappings.get(row["fdc_id"], {}).get("allergens"),
            "mapping_note": mappings.get(row["fdc_id"], {}).get("mapping_note"),
        }
        for row in sorted(foods, key=lambda item: int(item["fdc_id"]))
    ]


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^## {heading}\s*\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    if not match or not match[1].strip():
        raise ValueError(f"Missing recipe section: {heading}.")
    return match[1].strip()


def import_recipes(directory: Path, commit: str, paths: list[str]) -> list[dict[str, Any]]:
    recipes = []
    for index, path in enumerate(paths, start=1001):
        if not path.startswith("dishes/") or not path.endswith(".md") or ".." in Path(path).parts:
            raise ValueError("Recipe paths must stay inside the source dishes directory.")
        raw = subprocess.run(
            ["git", "-C", str(directory), "show", f"{commit}:{path}"],
            check=True,
            capture_output=True,
        ).stdout
        text = raw.decode("utf-8")
        quantities = _section(text, "计算")
        recipes.append(
            {
                "id": index,
                "name": Path(path).stem,
                "source_id": "howtocook",
                "source_uri": (
                    f"https://github.com/Anduin2017/HowToCook/blob/{commit}/{quote(path)}"
                ),
                "source_path": path,
                "source_sha256": hashlib.sha256(raw).hexdigest(),
                "raw_ingredients": _section(text, "必备原料和工具"),
                "raw_quantities": quantities,
                "instructions": _section(text, "操作"),
                "ingredients": [
                    {"quantity_text": line}
                    for line in (
                        re.findall(r"^\s*[-*]\s+(.+)$", quantities, re.MULTILINE) or [quantities]
                    )
                ],
                "status": "pending",
                "review_issues": [
                    "尚未逐项完成食材映射、可食克重、调味用量及份量核对；不进入推荐索引。"
                ],
            }
        )
    return recipes


def build_catalog(usda: Path, howtocook: Path, selection: dict[str, Any]) -> FoodCatalog:
    commit = selection["howtocook_commit"]
    license_bytes = subprocess.run(
        ["git", "-C", str(howtocook), "show", f"{commit}:LICENSE"],
        check=True,
        capture_output=True,
    ).stdout
    if b"public domain" not in license_bytes or b"unlicense.org" not in license_bytes:
        raise ValueError("The pinned HowToCook license must be reviewed before import.")
    recipes = import_recipes(howtocook, commit, selection["recipes"])
    # Only these three source recipes have a complete initial mass-based mapping.
    ready: dict[str, dict[str, Any]] = {
        "清蒸南瓜": {
            "ingredients": [
                {"quantity_text": "南瓜 300 g", "ingredient_id": "fdc:168448", "grams": 300},
                {
                    "quantity_text": "饮用水 1000 ml（用于蒸锅）",
                    "consumed": False,
                    "note": "蒸锅用水不作为食用投料，也不作为成菜重量。",
                },
            ],
            "servings": 1,
            "serving_label": "整份配方（原文供1至2人分食）",
            "meal_types": ["snack"],
            "meal_role": "dish",
            "preparation_note": "以去籽后可食生南瓜300克作为投料；没有测量蒸熟后的重量。",
        },
        "糖拌西红柿": {
            "ingredients": [
                {
                    "quantity_text": "西红柿2个（原文明确共200g）",
                    "ingredient_id": "fdc:170457",
                    "grams": 200,
                },
                {"quantity_text": "白砂糖20g", "ingredient_id": "fdc:169655", "grams": 20},
            ],
            "servings": 2,
            "serving_label": "半份配方（原文2人份中的1人份）",
            "meal_types": ["snack"],
            "meal_role": "snack",
            "preparation_note": "采用原文明确的200克番茄和20克白砂糖，不采用原文预估卡路里。",
        },
        "小米粥": {
            "ingredients": [
                {"quantity_text": "小米100克", "ingredient_id": "fdc:169702", "grams": 100},
                {
                    "quantity_text": "水2000克",
                    "ingredient_id": "fdc:173647",
                    "grams": 2000,
                },
            ],
            "servings": 1,
            "serving_label": "整锅配方（未确定单人份量）",
            "meal_types": ["breakfast"],
            "meal_role": "staple",
            "preparation_note": "按整锅计算，不把2100克投料当作成粥重量或单人份量。",
        },
    }
    for recipe in recipes:
        if recipe["name"] in ready:
            recipe.update(ready[recipe["name"]])
            recipe.update(status="calculation_ready", review_issues=[])
    return FoodCatalog.model_validate(
        {
            "sources": [
                {
                    "id": "usda-sr-legacy-2018-04",
                    "name": "USDA FoodData Central SR Legacy",
                    "uri": USDA_URL,
                    "license": "CC0-1.0",
                    "version": "2018-04",
                    "sha256": selection["usda_sha256"],
                    "checksum_scope": "archive",
                },
                {
                    "id": "howtocook",
                    "name": "Anduin2017/HowToCook",
                    "uri": f"https://github.com/Anduin2017/HowToCook/tree/{commit}",
                    "license": "Unlicense",
                    "version": commit,
                    "sha256": hashlib.sha256(license_bytes).hexdigest(),
                    "checksum_scope": "license",
                },
            ],
            "ingredients": import_ingredients(
                usda, selection["usda_sha256"], selection["ingredient_mappings"]
            ),
            "recipes": recipes,
        }
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--usda-archive", type=Path, required=True)
    parser.add_argument("--howtocook-repo", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=project_root() / "data")
    args = parser.parse_args()
    with (project_root() / "data" / "catalog_selection.json").open(encoding="utf-8") as handle:
        selection = json.load(handle)
    catalog = build_catalog(args.usda_archive, args.howtocook_repo, selection)
    args.output.mkdir(parents=True, exist_ok=True)
    outputs = {}
    for key in ("sources", "ingredients", "recipes"):
        items = getattr(catalog, key)
        content = "[\n" + ",\n".join(item.model_dump_json() for item in items) + "\n]\n"
        path = args.output / f"{key}.json"
        if path.exists() and path.read_text(encoding="utf-8") != content:
            raise ValueError("Refusing to overwrite changed catalog files; use a new --output.")
        outputs[path] = content
    for path, content in outputs.items():
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    print(
        f"Imported {len(catalog.ingredients)} ingredients, {len(catalog.recipes)} recipes; "
        f"{len(catalog.calculated_meals())} calculation-ready."
    )


if __name__ == "__main__":
    main()
