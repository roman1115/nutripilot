# 旧演示餐食退役与菜品餐单验收

验收日期：2026-10-29。本文记录本次实际运行的检查，不替代历史验收，也不代表营养实测或真实对话模型准确率。

后续已完成真实 PostgreSQL/Redis/Qdrant 迁移与重启恢复验收，见
[真实持久化验收](persistence-validation-2026-10-29.md)；本文末尾的未验事项保留为本批次历史快照。

## 已实现

- 新推荐、MCP 营养查询及新餐单统一使用 100 道来源菜谱。100 道均可计算，44 道存在营养范围；原有食材数值、菜谱编号、来源和角色未因本次迁移改变。
- `data/meals.json` 的 18 条记录在载入时标为 `is_active=false`。在用列表只返回 100 条；按编号查询仍可返回旧记录，供历史餐单、反馈显示原名称和营养。营养工具额外拒绝退役编号及名称，新餐单写入也拒绝退役菜品。
- PostgreSQL 的 API 和外部 MCP 两个启动入口均上载全部 118 条记录，将旧记录更新为退役状态，不删除历史数据。历史批量编号查询不再依赖在用列表。旧餐单的相同幂等键重放仍返回原记录。
- 外部 MCP 初始化只为 100 道在用菜谱生成向量，上载前显式删除旧 18 条向量；不删除集合，也不清理无关记录。检索仍通过在用候选集合限制向量结果。
- 餐单按日期、餐次安排菜品，每个餐次一道菜；一日三餐为 3 项，一周三餐为 21 项。三餐分别检索，避免遗漏早餐。保留餐次、日期、候选集、过敏/忌口、身份、人工确认及幂等校验。
- 确认前可换一批菜品，保留日期和餐单范围，排除上一份安排中的菜品；旧确认编号不能批准新安排。若确认时菜品已经退役，阻止写入并说明需要重新生成。
- 六项营养按所选整道菜累计上下限，未知值不当作零；不推断人数、个人份量或每日摄入，不再执行个人营养达标判断。画像目标仍可记录，但不用于整菜筛选或达标判断，工具边界会剔除模型自行添加的数值限制。
- 预览及已保存页面保留营养范围和菜谱来源。旧餐单明确标注历史份量；计划列表优先显示最新记录。修复提示框文字挤成竖排的问题。
- 修复餐单成功写入恰好达到工具步数上限时被误报失败的问题。成功保存后直接展示结果，不增加工具调用额度。

## 实际验证

| 检查 | 结果 |
| --- | --- |
| 完整 pytest | 235 项通过，1 条既有依赖警告 |
| Ruff | `src`、`apps`、`tests` 及相关脚本通过 |
| mypy | 95 个源文件通过，含导入器及草稿工具 |
| 前端 | typecheck、生产 build 通过 |
| Fake 核心 | 新版 `nutripilot-core-v3-2026-10-29`，60/60 |
| Fake 扩展 | `real-regressions-v3-2026-10-29`，8/8 |
| Fake 推荐 | 14/14 |
| 真实 BGE + Fake 对话 | 16/16，含三餐安排及换菜 |
| Chrome | 桌面及手机预览、换菜、确认保存、刷新恢复、来源与范围展示通过 |

后端验证包含：18 条退役记录的历史查询与反馈、用户隔离、旧幂等键重放、新写入拒绝退役编号、旧确认失效、确认前后营养范围一致、三餐候选覆盖、过敏约束、未知标签不放宽、个人目标不能误用为整菜条件。

PostgreSQL 使用测试替身验证启动上载参数、历史查询路径、新写入门槛及旧记录重放；**没有连接真实 PostgreSQL 服务**。Qdrant 删除与重复删除在实际客户端的内存模式中通过，外部 MCP 初始化的上载/清理参数另有测试；**没有连接真实 Qdrant 服务**。

浏览器验证了 1440×1000 桌面、390×844 与 320×740 手机的餐单预览及已保存页面。首页另验 1024×768、768×1024；无横向溢出、JavaScript 错误，已人工检查截图。确认后列表中的菜名与换菜预览一致，页面显示整菜范围及来源。

原始证据位于 Git 忽略目录：

- `.runtime/catalog-retirement-evals/`：`core`、`regressions`、`recommendations`、`bge-fake` 的 JSON/Markdown 报告。
- `.runtime/catalog-retirement-web/`：浏览器检查结果、桌面及手机截图。

## 重跑

```sh
.venv/bin/pytest
.venv/bin/ruff check src apps tests scripts/import_food_catalog.py scripts/draft_food_catalog.py scripts/generate_eval_dataset.py scripts/verify_web.py
MYPYPATH=src:. .venv/bin/mypy --explicit-package-bases src apps scripts/import_food_catalog.py scripts/draft_food_catalog.py
npm --prefix web run typecheck
npm --prefix web run build
APP_MODE=demo MODEL_PROVIDER=fake .venv/bin/python -m nutripilot.evaluation.cli --dataset evals/datasets/core.json --output-dir .runtime/catalog-retirement-evals --label core
.venv/bin/python scripts/verify_web.py --base-url http://127.0.0.1:5176 --executable "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --output-dir .runtime/catalog-retirement-web
```

## 限制与部署核验

- Docker daemon 本次不可用，未运行真实 PostgreSQL、Redis、Qdrant 或完整 Docker 构建。部署迁移代码及测试已完成，真实服务中的旧记录标记和向量清理尚未实际验收。
- 本次未重新请求 Qwen。此前同日验收遇到提供方额度限制，见 [推荐链路验收](catalog-recommendations-2026-10-29.md)。真实 BGE 通过不能代替真实 Qwen 效果验证。
- 营养仍为整道菜投料估算，未经成菜检测；单道菜不代表完整一餐。价格、准备时间和部分饮食标签未知，不能自动补成已知。当前没有已标注的“素食”菜谱，严格素食餐单会说明候选不足；预算或时间上限同样可能导致无候选。
- 本地试用为 Fake/内存模式，画像、餐单、反馈及图检查点随 API 进程结束而丢失；SQLite 保留对话消息与执行记录不等于业务数据已持久化。本次未改变该既有演示模式边界。
- 真实环境核验时，在已有数据库及索引上启动新版 API/MCP，检查在用菜谱为 100、旧 18 条仍可按编号读取且标为退役、旧向量已移除；再核验历史计划/反馈及新餐单确认保存。验收前保留原有数据库备份，执行后记录结果。
