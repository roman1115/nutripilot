"""Suggest reusable mappings without approving recipes or changing the live catalog."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

from scripts.import_food_catalog import import_recipes

from nutripilot.paths import project_root
from nutripilot.retrieval.catalog import Ingredient, load_food_catalog
from nutripilot.retrieval.catalog_mapping import CatalogMappings

QUANTITY = re.compile(
    r"(?<![\d./])(-?\d+(?:\.\d+)?)\s*(?:[-～~—]\s*(\d+(?:\.\d+)?)\s*)?"
    r"(毫升|ml|克|g|个|颗|枚|瓣)(?![a-zA-Z])",
    re.I,
)
UNIT = {"毫升": "ml", "克": "g", "颗": "个", "枚": "个"}


def draft_recipe(
    recipe: dict[str, Any], ingredients: list[Ingredient], mappings: CatalogMappings
) -> dict[str, Any]:
    by_id = {item.id: item for item in ingredients}
    aliases: dict[str, set[str]] = {}
    for item in ingredients:
        if not item.mapping_note:
            continue
        for name in [item.name, *item.aliases]:
            aliases.setdefault(name, set()).add(item.id)
    for name, ingredient_id in mappings.draft_aliases.items():
        if ingredient_id not in by_id:
            raise ValueError(f"Unknown draft ingredient: {ingredient_id}")
        aliases[name] = {ingredient_id}
    rows = []
    proposed = []
    for part in recipe["ingredients"]:
        text = part["quantity_text"]
        # Longest aliases prevent matching 油 inside 酱油 or 鸡蛋 inside 鸡蛋清.
        remaining = text
        matched_ids: set[str] = set()
        for name in sorted(aliases, key=len, reverse=True):
            if name and name in remaining:
                matched_ids.update(aliases[name])
                remaining = remaining.replace(name, " ")
        quantities = list(QUANTITY.finditer(text))
        issues = []
        suggestion = None
        if len(matched_ids) != 1:
            issues.append("食材未唯一匹配，需核对品种、品牌或复合用料")
        if len(quantities) != 1:
            issues.append("用量不唯一或未提供可换算数字，需核对公式及单位")
        if len(matched_ids) == 1 and len(quantities) == 1:
            ingredient_id = next(iter(matched_ids))
            quantity = quantities[0]
            low, high = float(quantity[1]), float(quantity[2] or quantity[1])
            unit = UNIT.get(quantity[3].lower(), quantity[3].lower())
            rules = [
                key for key, rule in mappings.quantity_rules.items()
                if rule.ingredient_id == ingredient_id and rule.unit == unit
            ]
            ingredient = by_id[ingredient_id]
            if re.search(r"可选|腌", text) or (
                ingredient_id == "fdc:171287" and re.search(r"蛋白|蛋清|蛋黄|粉|酱", text)
            ):
                issues.append("可选、腌制或蛋的组成部分需单独核对，不自动采用")
            elif low <= 0 or high < low:
                issues.append("零、负数或逆序范围，需确定是否为可选项")
            elif not rules:
                issues.append("缺少该单位的已记录换算规则")
            elif len({mappings.quantity_rules[key].grams_per_unit for key in rules}) != 1:
                issues.append("该单位存在多种换算比例，需核对具体规则")
            elif ingredient.allergens is None or any(
                value is None for value in ingredient.nutrients_per_100g.model_dump().values()
            ):
                issues.append("营养字段或既有过敏元数据不完整")
            else:
                suggestion = {
                    "rule": rules[0],
                    "amount": low if low == high else {"minimum": low, "maximum": high},
                    "text": text,
                    "note": "自动草稿：必须核对原文、可食部、可选项和实际食用范围后采用。",
                }
                proposed.append(suggestion)
        rows.append({
            "text": text, "ingredient_candidates": sorted(matched_ids),
            "suggestion": suggestion, "issues": issues,
        })
    checks = ["必须交叉核对步骤、可选项、重复投料和腌制液；草稿不得直接发布。"]
    if re.search(r"腌|去骨|去壳|去头|蛋黄|蘸|捞出|倒掉|沥干", recipe["instructions"]):
        checks.append("步骤含可食部或排除项处理，需核对食用量及接触过敏原。")
    for line in recipe["instructions"].splitlines():
        if re.search(r"\d+\s*(?:[-～~]\s*\d+\s*)?(?:ml|毫升|克|g)", line, re.I):
            checks.append("步骤定量投料核对：" + line.strip())
    return {
        "recipe": recipe,
        "rows": rows,
        "mapping": {
            "source_sha256": recipe["source_sha256"],
            "parts": proposed,
            "issues": checks,
            "note": "自动映射草稿，尚未核对，不进入营养计算或推荐。",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--howtocook-repo", type=Path, required=True)
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    selection = json.loads((project_root() / "data/catalog_selection.json").read_text())
    if not 0 <= args.start_index < len(selection["recipes"]):
        parser.error("--start-index must point to a selected source recipe.")
    mappings = CatalogMappings.model_validate_json(
        (project_root() / "data/recipe_mappings.json").read_text()
    )
    catalog = load_food_catalog(project_root() / "data")
    overrides = {**selection["ingredient_mappings"], **mappings.ingredient_mappings}
    ingredients = [
        Ingredient.model_validate({
            **item.model_dump(mode="json"),
            **(
                overrides.get(item.source_record_id, {})
                if item.source_id.startswith("usda-") else {}
            ),
        })
        for item in catalog.ingredients
    ]
    recipes = import_recipes(
        args.howtocook_repo, selection["howtocook_commit"],
        selection["recipes"][args.start_index:], start_id=1001 + args.start_index,
    )
    drafts = [draft_recipe(recipe, ingredients, mappings) for recipe in recipes]
    total = sum(len(item["rows"]) for item in drafts)
    matched = sum(row["suggestion"] is not None for item in drafts for row in item["rows"])
    output = {
        "source_commit": selection["howtocook_commit"],
        "recipe_count": len(drafts), "quantity_rows": total,
        "suggested_rows": matched, "unresolved_rows": total - matched,
        "note": "建议匹配率不是准确率；全部草稿保留待核对状态，步骤核对另计。",
        "drafts": drafts,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(f"Drafted {len(drafts)} recipes; {matched}/{total} quantity rows suggested.")


if __name__ == "__main__":
    main()
