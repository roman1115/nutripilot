# 菜谱与食材营养数据

## 当前内容

| 文件 | 内容 |
| --- | --- |
| `ingredients.json` | USDA SR Legacy 的 7,793 条食品/食材记录，每 100 克的能量、蛋白质、碳水、脂肪、纤维和钠；未知值保留 `null` |
| `recipes.json` | 50 道 HowToCook 来源菜谱，保存食材原文、用量原文、操作步骤及文件校验和 |
| `sources.json` | 来源、许可证、固定版本、下载地址与校验和范围 |
| `catalog_selection.json` | 固定的菜谱文件清单、源版本和初始中文食材映射 |
| `meals.json` | 保留的 18 条旧演示餐食；编号与现有计划、反馈继续兼容 |

7,793 是源数据库的食品条目数，不是 7,793 种已核对的中文基础食材。当前仅 36 个条目具有初始中文映射说明。不能以名称相似自动判断生熟状态、食材品种、品牌或可食部一致。

50 道菜中有 3 道达到 `calculation_ready`：清蒸南瓜、糖拌西红柿、小米粥。其余 47 道为 `pending`，可以查看原文，但不会加入推荐索引或显示虚构营养结果。`calculation_ready` 只表示计算所需元数据齐全，不表示营养师审核或成菜检测通过。

## 来源

- USDA FoodData Central SR Legacy，版本 `2018-04`；直接使用官方 CSV 下载包，不要求用户配置 API Key。数据许可证为 CC0-1.0，保留 FDC 编号、英文原始名称和来源链接。许可依据：`https://fdc.nal.usda.gov/api-guide.html` 的 Licensing 说明。本版本是固定历史数据，不声称使用最新食品数据。
- HowToCook，提交 `308cc8b7dab1da84b20e7fafdf836a715437f582`。上游仓库标注 Unlicense，许可证副本在 `licenses/HowToCook-Unlicense.txt`。保留逐文件链接和 SHA-256；本轮没有导入图片、其他文件或整站爬虫数据。未来复用新来源仍需检查各文件的额外来源说明。

## 计算与发布

- 每道配方引用 `ingredient_id`，按食用投料克重计算：`各食材每100克营养 × 投料克重 / 100`，最后除以明确的配方份数。
- 不将“个、瓣、适量、毫升”无依据地转换成克；不能把未知营养字段当作零。
- 可食部、调味料用量、食材映射、过敏原元数据及份数不完整时，不得切换为 `calculation_ready`。未知过敏原为 `null`，与经过核对后标注的空列表不同。
- 非食用用水等需要明确排除原因；不会把蒸锅用水计算成菜品重量。
- 输出状态为 `calculated_estimate`。未校正烹调损失、吸水、蒸发或油盐残留；显示的是投料重量，不能标为成菜重量或营养检测值。
- 小米粥原文没有单人份量，因此保留为“整锅配方”；清蒸南瓜按整份配方显示。单道菜、主食、汤和加餐不能由现有餐单规划器直接充当完整一餐。
- 新配方未知的价格与总准备时间保留为 `null`；指定价格或时间上限的检索不会把未知值作为零而通过。

启动时，两库由同一载入器校验。完整配方转换为既有 `Meal` 契约，因此离线模式可直接检索，外部模式沿用 PostgreSQL 餐食保存和 Qdrant 建索引流程。源食材、源菜谱暂以版本化 JSON 保存，尚未迁移成可在线编辑的独立数据库表。

## 查询

以下是已有访问控制保护的只读 API，可在 `/docs` 调试：

- `GET /api/v1/catalog`：数据量、可计算/待处理数量及来源。
- `GET /api/v1/ingredients?q=西红柿&limit=30&offset=0`：食材名称与别名查询、分页。
- `GET /api/v1/ingredients/fdc:170457`：单条营养记录与来源。
- `GET /api/v1/recipes?status=pending`：查看需要继续整理的菜谱。
- `GET /api/v1/recipes/1004`：原始配方、来源和可复算营养。

已接入现有 MCP `search_meals` 与 `get_nutrition`；可以在对话中查询“糖拌西红柿的营养”。尚未提供在线审核后台、配方编辑或新的 Agent 工具。

## 重现导入

先把上述 USDA ZIP 下载到本地，并获取含固定提交的 HowToCook Git 仓库。下载包的 SHA-256 必须与 `catalog_selection.json` 一致：

```bash
.venv/bin/python scripts/import_food_catalog.py \
  --usda-archive .runtime/data-sources/usda-sr-legacy-2018-04.zip \
  --howtocook-repo .runtime/data-sources/HowToCook \
  --output .runtime/catalog-rebuilt
```

脚本校验下载包哈希、营养单位、重复记录、引用和计算门槛后再生成文件。相同内容可重跑；发现目标文件与新结果不同则拒绝覆盖，应先导出到新目录并审查差异，防止覆盖已整理的配方。
