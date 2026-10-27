# NutriPilot 当前状态

最后核对：2026-10-27

## 项目现状

NutriPilot 是基于 LangGraph、FastAPI 和 React 的个人饮食决策展示项目，包含多轮对话、餐食检索、用户画像、餐单确认、会话历史、Trace 和离线评测。Fake 离线演示与 Qwen/BGE 展示模式均有实现。

## 数据状态

- `data/ingredients.json` 已导入 USDA SR Legacy 固定版本的 7,793 条食品/食材营养记录；缺失营养字段保留 `null`，其中 36 条有初始中文映射说明。源记录数量不代表中文可用食材覆盖率。
- `data/recipes.json` 已导入 HowToCook 固定提交的 50 道来源菜谱，保留原始食材、用量、步骤、来源、许可证引用和文件哈希。
- 其中 3 道配方计算链路已接通，47 道仍为 `pending`，不进入推荐索引。可计算配方为清蒸南瓜、糖拌西红柿、小米粥；不声称已完成 50 道高质量菜谱验收。
- 原有 `data/meals.json` 的 18 道餐食及编号保留，其营养数据仍为 `unverified_estimate`。加上 3 道计算配方，当前载入器产生 21 条可检索餐食记录。
- 已实现食材 ID、投料克重、配方份数关联及营养汇总。新配方标记为 `calculated_estimate`，不代表成菜检测或专业审核；整锅、投料重量与单人份量明确区分。
- 完整配方接入既有 MCP 检索及营养查询；只读 API 支持两库查询、分页和来源查看，详情见 [数据说明](data/README.md)。
- PostgreSQL/Qdrant 模式沿用同一载入器，将完整配方转为餐食记录保存和建索引。原始两库仍以版本化 JSON 维护，尚无独立食材/配方数据库表或在线编辑审核后台。
- 菜谱完整克重及中文食材匹配覆盖率仍是主要短板，数据基础链路已完成，数据整理尚未完成。

## 最近记录的验证

以下来自仓库中的历史验收文档，不代表本次状态页更新时重新运行：

- `docs/completeness-improvements-2026-10-21.md` 记录当时后端、Ruff、mypy、前端构建及 Fake/真实模型小样本回归通过。
- `docs/reliability-improvements-2026-10-21.md` 记录当时 107 项后端测试及真实模型、浏览器等检查通过。
- `docs/real-model-validation-2026-10-21.md` 是真实 Qwen/BGE 小样本功能验收，不是整体准确率评测。
- 完整 API/Web Docker 镜像构建及公开生产环境验收未包含在上述验收中。

2026-10-27 本轮两库实现后实际运行：

- 后端 160 项测试通过，另有 1 条既有依赖警告。
- Ruff、mypy（92 个源文件）、前端 typecheck/build 通过。
- Fake 核心回归 60/60、扩展回归 8/8；报告位于 Git 忽略的 `.runtime/catalog-evals/`。这是 Fake 回归，不是真实模型效果。
- 固定来源导入脚本可重现并可无变更重复执行；没有触发真实模型调用。
- 实际本地 HTTP 验收通过：两库统计、菜谱详情及“糖拌西红柿的营养”对话查询返回配方估算和来源。认证 HTTP MCP 测试也覆盖了新增配方。
- 本轮没有重新运行 Qwen/BGE、PostgreSQL/Qdrant 服务或 Docker 完整构建。

## 下一步

1. 继续处理 47 道待整理菜谱的食材匹配、可食克重、调味料和份数；优先补能组成日常完整餐食的配方，避免只增加配菜。
2. 根据数据维护需求再迁移独立数据库表；当前不开发无需求的审核后台。烹调损失、成菜重量和跨食材替换计算仍未实现。
3. 增加“心情不好吃什么”等场景的回归样例，检查推荐依据和健康表述。
4. 在需要交付容器时重新执行 Docker 完整构建；只有准备公开部署时，再完成 HTTPS、暴露面和备份恢复验收。

## 文档导航

- 架构与设计基线：[项目设计与实现方案.md](项目设计与实现方案.md)
- 历史链路验收：[docs/local-chain-validation-2026-10-21.md](docs/local-chain-validation-2026-10-21.md)
- 真实模型验收：[docs/real-model-validation-2026-10-21.md](docs/real-model-validation-2026-10-21.md)
- 连续使用与餐单质量：[docs/completeness-improvements-2026-10-21.md](docs/completeness-improvements-2026-10-21.md)
- 可靠性修复：[docs/reliability-improvements-2026-10-21.md](docs/reliability-improvements-2026-10-21.md)
- 前端改版：[docs/frontend-refresh-2026-10-23.md](docs/frontend-refresh-2026-10-23.md)
