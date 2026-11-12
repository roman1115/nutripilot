from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from scripts.draft_food_catalog import draft_recipe
from scripts.import_food_catalog import import_recipes

from nutripilot.retrieval.catalog import load_food_catalog
from nutripilot.retrieval.catalog_mapping import CatalogMappings

DATA = Path(__file__).resolve().parents[2] / "data"


def test_import_preserves_source_calories_separately_and_extracts_known_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = """# 测试
预估卡路里：476 大卡
## 必备原料和工具
- 鸡蛋
- 水果刀
## 计算
+ 鸡蛋 2 个
## 操作
蒸熟。
"""
    monkeypatch.setattr(
        "scripts.import_food_catalog.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=source.encode()),
    )
    item = import_recipes(Path("/unused"), "fixed", ["dishes/test.md"], start_id=1051)[0]
    assert item["id"] == 1051
    assert item["source_calories_kcal"] == 476
    assert item["source_calories_text"] == "476 大卡"
    assert item["tools"] == ["水果刀"]
    assert item["ingredients"] == [{"quantity_text": "鸡蛋 2 个"}]
    assert "nutrition" not in item
    assert item["status"] == "pending"
    with pytest.raises(ValueError, match="Duplicate"):
        import_recipes(Path("/unused"), "fixed", ["dishes/test.md"] * 2)
    with pytest.raises(ValueError, match="inside"):
        import_recipes(Path("/unused"), "fixed", ["dishes/../private.md"])


def test_draft_preserves_ranges_avoids_substring_oil_match_and_stays_pending() -> None:
    catalog = load_food_catalog(DATA)
    mappings = CatalogMappings.model_validate_json((DATA / "recipe_mappings.json").read_text())
    recipe = catalog.recipes[0].model_dump(mode="json")
    recipe["instructions"] = "腌制后去蛋黄。锅中另加入10ml食用油。"
    recipe["ingredients"] = [
        {"quantity_text": "食盐 1～2g"},
        {"quantity_text": "酱油 5ml"},
        {"quantity_text": "鸡蛋清 1个"},
        {"quantity_text": "香油 2ml（可选）"},
        {"quantity_text": "未知复合酱 5g"},
    ]
    draft = draft_recipe(recipe, catalog.ingredients, mappings)
    salt, soy, white, optional, unknown = draft["rows"]
    assert salt["suggestion"]["amount"] == {"minimum": 1, "maximum": 2}
    rule = mappings.quantity_rules[soy["suggestion"]["rule"]]
    assert rule.ingredient_id == "fdc:174277"
    assert all(row["suggestion"] is None for row in (white, optional, unknown))
    assert draft["mapping"]["issues"]
    assert any("步骤定量投料" in issue for issue in draft["mapping"]["issues"])
    assert any("可食部" in issue for issue in draft["mapping"]["issues"])


def test_draft_rejects_missing_nutrients_and_ambiguous_aliases() -> None:
    catalog = load_food_catalog(DATA)
    mappings = CatalogMappings.model_validate_json((DATA / "recipe_mappings.json").read_text())
    recipe: dict[str, Any] = catalog.recipes[0].model_dump(mode="json")
    recipe["ingredients"] = [{"quantity_text": "测试食材 5g"}]
    tomato = next(item for item in catalog.ingredients if item.id == "fdc:170457")
    tomato.nutrients_per_100g.sodium_mg = None
    mappings.draft_aliases = {"测试食材": tomato.id}
    draft = draft_recipe(recipe, [tomato], mappings)
    assert draft["rows"][0]["suggestion"] is None
    assert "营养字段" in draft["rows"][0]["issues"][0]
    mappings.draft_aliases.clear()
    tomato.aliases = ["测试食材"]
    salt = next(item for item in catalog.ingredients if item.id == "fdc:173468")
    salt.aliases = ["测试食材"]
    draft = draft_recipe(recipe, [tomato, salt], mappings)
    assert len(draft["rows"][0]["ingredient_candidates"]) == 2
    assert draft["rows"][0]["suggestion"] is None


def test_second_batch_keeps_source_recipe_ids_unique_and_original_prefix() -> None:
    catalog = load_food_catalog(DATA)
    selected = json.loads((DATA / "catalog_selection.json").read_text())["recipes"]
    assert len(selected) == len(set(selected)) == 100
    assert [item.id for item in catalog.recipes] == list(range(1001, 1101))
    assert catalog.recipes[0].name == "西红柿炒鸡蛋"
    assert catalog.recipes[49].name == "茄子炖土豆"
    assert catalog.recipes[50].name == "太阳蛋"
    assert catalog.recipes[-1].name == "奶油蘑菇汤"
    assert all(recipe.source_calories_kcal is not None for recipe in catalog.recipes)


@pytest.mark.parametrize("text", ["盐 -2g", "盐 2～1g", "盐 0g", "盐 1/2g"])
def test_draft_does_not_turn_invalid_or_fraction_quantities_into_positive_amounts(
    text: str,
) -> None:
    catalog = load_food_catalog(DATA)
    mappings = CatalogMappings.model_validate_json((DATA / "recipe_mappings.json").read_text())
    recipe = catalog.recipes[0].model_dump(mode="json")
    recipe["ingredients"] = [{"quantity_text": text}]
    draft = draft_recipe(recipe, catalog.ingredients, mappings)
    assert draft["rows"][0]["suggestion"] is None


def test_draft_rejects_conflicting_conversion_factors() -> None:
    catalog = load_food_catalog(DATA)
    mappings = CatalogMappings.model_validate_json((DATA / "recipe_mappings.json").read_text())
    mappings.quantity_rules["different_egg"] = mappings.quantity_rules["egg"].model_copy(
        update={"grams_per_unit": 60}
    )
    recipe = catalog.recipes[0].model_dump(mode="json")
    recipe["ingredients"] = [{"quantity_text": "鸡蛋 1个"}]
    draft = draft_recipe(recipe, catalog.ingredients, mappings)
    assert draft["rows"][0]["suggestion"] is None
    assert "多种换算" in draft["rows"][0]["issues"][0]


@pytest.mark.parametrize("calories", ["", "预估卡路里：100～200 大卡\n", "预估卡路里：500 kJ\n"])
def test_import_keeps_missing_or_ambiguous_source_calories_unknown(
    calories: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = f"# 测试\n{calories}## 必备原料和工具\n- 鸡蛋\n## 计算\n- 鸡蛋1个\n## 操作\n蒸熟。"
    monkeypatch.setattr(
        "scripts.import_food_catalog.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout=source.encode()),
    )
    item = import_recipes(Path("/unused"), "fixed", ["dishes/test.md"])[0]
    assert item["source_calories_kcal"] is None
    assert item["source_calories_text"] == (calories.split("：")[1].strip() if calories else None)
