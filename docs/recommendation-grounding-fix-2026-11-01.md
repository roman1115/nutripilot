# 本轮推荐事实与工具前置流程修复

验收日期：2026-11-01。本文记录本次修复，不覆盖此前失败报告。

## 范围

- 新轮次只将旧候选存入 `previous_candidate_ids`，用于换菜排除；
  `candidate_ids` 从本轮工具结果重新生成。
- 推荐生成与输出边界都按本轮成功 `search_meals` 的完整记录校正：
  名称、整道菜营养、营养范围、来源、理由和警告不采用模型编造的事实。
  没有成功检索时删除无依据卡片并重建正文；成功空结果保留筛选诊断解释。
- 指定 `food_name` 贯穿工具参数、MCP 合约、服务和检索硬筛选，
  仅使用收录名称或可靠别名匹配，不以语义相似的其他菜替代。
  取消或替换食材条件时清理旧菜名，不改变画像或数据集。
- Planner 提前完成或乱序检索时，补齐成功读取画像、反馈后才检索的必要调用。
  工具边界仍拒绝缺少画像或反馈的调用；画像与反馈排除由程序注入，
  原预算、过敏保护、确认、身份隔离和持久化逻辑保留。
- 普通推荐天气可选；本轮明确天气需求时在检索前补查天气。
  Worker 数量仍为 1，全局并发 4、每用户 3、同会话 1。
- 评测增加逐轮事实、来源、前置顺序、本轮成功检索和最终状态断言。
  空列表必须实际清空，显式 `null` 不等同字段缺失，嵌套任选组不再用 `set` 转换。
  更新数据集版本及核心生成脚本，不覆写历史失败报告。

## 首轮修复验收

先补失败测试，确认旧实现确实失败，再实现修复。包括旧候选、错误菜名、
无成功检索、Planner 提前完成或漏读、未知指定对象、别名、天气和精确槽位。

真实环境：`qwen-plus`、本地 BGE、PostgreSQL、Redis、Qdrant、
HTTP MCP、独立 Worker。加载修复后重新启动服务，没有删除历史或数据卷。

- 40 场景、57 轮首轮回归：**38/40**，57 轮均完成。
- 所有本轮证据、工具前置、菜名、营养范围和来源检查通过；
  独立从持久化状态对照原始配方复核 132 个卡片，无事实或约束不符。
- 229 次主模型调用，输入 530,591、输出 28,406 Token，无降级；
  平均场景 19.80 秒、P95 31.13 秒，多轮场景统计全部轮次。
- 剩余失败：`ingredient-31` 首次“排除鸡翅、想吃鸡胸肉”误判为调整；
  `ingredient-34` 首轮虽检索到可用候选，但模型未选菜导致空答。
  不能将这轮改称全部通过。
- 补充失败测试后修复：无历史上下文的明确新推荐请求纠正为推荐；
  模型未选择有效卡片时由本轮可信候选补齐，依然经过最终安全校验。
- 独立数值条件 10 轮首轮补测全部通过，21 个推荐；
  44 次主模型调用，输入 102,914、输出 5,716 Token，无降级；
  新 HTTP 客户端恢复 18 条消息后续聊，最终 20 条消息。不是服务重启测试。

报告：

- `.runtime/ingredient-dialogue-real-fixed-2026-11-01.json`
- API 评测 ID：`d19cb14e-53e5-4d30-a339-7183d57f35f0`
- 持久化批次：`e384aa1d-9658-4979-847b-2afd093c0d2e`
- `.runtime/ingredient-dialogue-real-fixed-audit-2026-11-01.json`
- `.runtime/numeric-followup-real-fixed-2026-11-01.json`

## 最终验收

补充修复后再次重启真实服务，再跑完整 40 场景与数值条件补测，
输出到不同的 `*-fixed-final-2026-11-01.json` 文件。
最终主批次 **40/40 场景、57 轮全部通过**，工具前置顺序、
本轮检索、名称、营养范围、来源及完成状态断言均通过。
独立对照源配方审计 135 个推荐，无事实或约束不符。
232 次主模型调用，输入 539,159、输出 28,795 Token，
无降级、无无效模型调用；平均场景 19.92 秒、P95 31.11 秒。
API 评测 ID：`05b519dd-ac5e-4ef0-ac92-abe2686fc727`；
持久化批次：`b89d5e19-743f-4f5f-89be-a7a1e03a044a`。
这是固定小样本回归，不代表任意输入的整体准确率。

最终代码的数值补测 10/10 轮通过，
47 次主模型调用，输入 109,137、输出 5,806 Token，无降级。
独立源配方审计 21 个推荐无名称、营养、来源或约束不符，
客户端恢复 18 条消息后可续聊，最终 20 条消息。

另对实际应用重启前已完成的真实会话进行续聊：该会话保存于
`2026-11-01T14:27:45Z`，最终服务重启探测为 `2026-11-01T14:57:41Z`。
重启后恢复原 20 条消息与完整槽位，再次换一批后为 22 条消息；
原 20 条内容不变，过敏/忌口、已取消的预算与时间约束保持，换菜排除上一批。
追加轮 4 次主模型调用，输入 10,364、输出 601 Token，无降级。
独立复核该会话全部 11 轮、24 个推荐无事实或约束不符。
这项是真实应用进程重启后的快照恢复与续聊；在途 SIGKILL 恢复由外部集成测试覆盖。

当前全量自动测试含外部 PostgreSQL、Redis、Qdrant 共 410 项通过、1 条依赖警告，
包括 API/Worker SIGKILL 后恢复、写入后检查点前崩溃的幂等恢复、
确认状态/反馈/身份隔离跨重建保持。Ruff、mypy 99 个源文件、
前端 typecheck/build 通过。

浏览器首轮使用独立门控 Fake Worker、真实 PostgreSQL/SSE，
1440 和 390 像素下 10 项交互检查通过：切页、刷新、关页再开、三个并发一个排队、
单独停止、断网期间完成后重连、多标签自动同步，页面脚本错误为 0。
首尝试因 Playwright 对应浏览器版本未安装而中断；改用现有 Google Chrome 后通过，
未下载新浏览器。最终代码再次通过相同的 10 项检查。

真实 Qwen 浏览器验收另包含：

- 6 轮推荐/做法/指代/食材追问，通过含糊“这个”的澄清、第二道解析、
  原文做法、来源折叠、刷新恢复、1440/390/320 像素无横向溢出；
  17 次模型调用，输入 35,129、输出 1,626 Token。
- 9 轮食材连续对话通过：添加、替换、任选组、换一批、清空、过敏保留、
  未知价格拒绝推荐及刷新恢复；37 次模型调用，
  输入 87,427、输出 4,152 Token，无降级。两组浏览器脚本错误均为 0。

四套 Fake 数据集使用本轮更严格评测重跑：
食材 40/40、菜品 14/14、扩展 8/8；核心 **58/60**，
`core-057/058` 的“用户”“使用”被已有 Fake 解析器中的“用”误认成食材请求，
错误进入推荐澄清。该问题存在于本次未修改的 Fake 分类路径；
不放宽期望、不将该结果改称全通过，暂不扩大本次生产流程修复范围。
报告保存在 `.runtime/fake-fact-guard-final-2026-11-01/`。

最终代码证据：

- `.runtime/ingredient-dialogue-real-fixed-final-2026-11-01.json`
- `.runtime/ingredient-dialogue-real-fixed-final-audit-2026-11-01.json`
- `.runtime/numeric-followup-real-fixed-final-2026-11-01.json`
- `.runtime/numeric-followup-real-fixed-final-audit-2026-11-01.json`
- `.runtime/durable-web-fixed-final-2026-11-01/result.json`
- `.runtime/recipe-chat-fixed-final-2026-11-01/result.json`
- `.runtime/ingredient-browser-fixed-final-2026-11-01/result.json`
- `.runtime/real-restart-followup-fixed-final-2026-11-01.json`
- `.runtime/real-restart-followup-fixed-final-audit-2026-11-01.json`

未覆盖完整应用镜像构建、生产负载或任意表达的模型准确率；
营养仍为整道菜配方计算估算，不是成菜检测。
本次不增加菜谱数据、依赖或 Worker，不修改前端业务代码；
真实服务 Web 5178/API 8006/MCP 8012 及独立 Worker 已加载最终修复并保持运行。

## 复跑

```sh
NUTRIPILOT_TEST_EXTERNAL=1 .venv/bin/pytest -o addopts='' -q --disable-warnings
.venv/bin/ruff check src apps tests scripts
.venv/bin/mypy src apps
npm --prefix web run build
.venv/bin/python scripts/verify_real_regressions.py \
  --base-url http://127.0.0.1:8006 \
  --dataset evals/datasets/ingredient-dialogue.json \
  --output .runtime/ingredient-dialogue-real-new-run.json --timeout 2400
```

真实模型复跑会产生费用、任务记录和新会话；使用新的输出路径保留历史证据。
