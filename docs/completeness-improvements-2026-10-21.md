# 连续使用与餐单质量完善

日期：2026-10-21，Asia/Shanghai。

## 本轮行为

- 餐单由程序按已验证候选组合。优先避免同日重复和相邻日期同餐次重复，
  再平衡候选使用次数；候选不足时允许重复，但明确显示餐数与独立菜品数。
  这是菜品层面的去重，不等于专业食物类别多样性评估。
- 支持 `daily_calorie_target` 与 `daily_protein_target_g`，与单餐检索条件分离。
  全天三餐按标准份量合计营养值，热量匹配容差为目标值的正负 10%，蛋白质目标
  按最低值检查。该容差是产品匹配规则，不是医疗建议。
- 本轮明确提出的每日目标无法满足时，不生成可确认写入请求；不虚增份量、
  不补造营养。仅画像中的参考目标未满足时，显示偏差警告，允许用户核对后保存。
  只安排某一个餐次时不宣称全天达标。
- 确认面板显示实际菜名、标准份量热量、蛋白质和每日合计；保存的计划携带当时的
  质量报告，不会因日后修改画像而改写历史判定。旧计划缺少报告时不回填虚构报告。
- 用户画像增加每日蛋白质目标，热量和蛋白质目标均可清空。
- 推荐理由只组合真实匹配的口味、菜系和饮食标签，以及本轮数值约束。
  不把标签匹配解释为减脂疗效，不从食品过敏原推断用户过敏。
- MCP 的请求和返回契约统一支持最多 20 条候选，并携带偏好标签。
  补齐含豆奶种子餐食中的“大豆”过敏原标注；仍保持 18 条餐食。
- 增加服务端历史会话列表、内容搜索、分页、会话切换和完整消息恢复。消息、
  推荐卡片和引用一并恢复；用户隔离由服务端身份控制，不依赖浏览器过滤。
  新记录从本版本启用后开始累计，不声称能恢复旧版本未保存的完整消息。
- 部署模式的会话索引和消息与 Trace、报告一起放在 SQLite；图执行状态仍由
  PostgreSQL Checkpoint 管理。历史操作在线程池执行；个人单实例是当前范围。
- 取消也保存 FAILED/CANCELLED 终态及消息，刷新后不再遗留“执行中”假象。
  已完成的写入不会因取消而回滚。同一会话的运行锁覆盖所有用户，避免并发抢占。
- 明确无效的计划日期在模型调用前校验，避免为非法日期消耗重试费用。
- 评测支持多轮追问、每日目标、计划餐数和失败代码断言；持久化完成报告不再
  长期保留额外内存副本。真实回归数据从 4 条扩为 8 条。
- Docker 构建上下文排除 `.env`、`.runtime` 与本地模型/运行文件。

## 备份与清理

SQLite 快照包含 WAL 中已提交的数据，拒绝覆盖现有文件，写完检查完整性：

```bash
.venv/bin/python scripts/maintain_runtime.py backup \
  --source .runtime/real-observability.sqlite3 \
  --destination .runtime/backups/records-2026-10-21.sqlite3
```

恢复只写入新路径，不直接覆盖运行中的数据库：

```bash
.venv/bin/python scripts/maintain_runtime.py restore \
  --source .runtime/backups/records-2026-10-21.sqlite3 \
  --destination .runtime/restored/records.sqlite3
```

检查恢复文件后，停止 API，将 `RUN_STORE_PATH` 指向新文件再启动。
SQLite 只包含 Trace、评测和消息；完整恢复还必须同时备份 PostgreSQL 中的画像、
计划、反馈和 Checkpoint。不要把“SQLite 备份成功”当作整套业务已备份。
Redis 的写幂等键也必须在整套恢复策略中处理，恢复后不要自动重放旧待确认写入。
Qdrant 索引可由餐食数据重建，BGE 模型应另行保留。

30 天以前的已结束 Trace/评测，默认只预览数量：

```bash
.venv/bin/python scripts/maintain_runtime.py prune \
  --source .runtime/real-observability.sqlite3 --days 30
```

备份并停止 API 后，加 `--apply` 才会删除。不会清理会话消息、待确认 Trace 或
运行中评测，也不默认删除历史计划。备份含个人信息，不要提交 Git 或上传公共存储。
当前没有自动定时清理或完整灾难恢复编排。

## 回归入口

```bash
.venv/bin/python -m pytest --cov=nutripilot
.venv/bin/ruff check .
MYPYPATH=src .venv/bin/mypy --explicit-package-bases src/nutripilot apps
npm --prefix web run build
MODEL_PROVIDER=fake APP_MODE=demo .venv/bin/python -m nutripilot.evaluation.cli \
  --dataset evals/datasets/core.json --label completeness-fake-core-final
```

真实服务启动后，以下脚本会产生模型调用费用：

```bash
.venv/bin/python scripts/verify_real_regressions.py \
  --output .runtime/completeness-real-regressions-final.json
.venv/bin/python scripts/verify_web.py --base-url http://127.0.0.1:5174 --real
.venv/bin/python scripts/verify_cancellation.py
```

浏览器脚本使用独立验收用户，覆盖消息恢复、历史搜索切换、餐单预览、确认保存和
移动布局。可用 `--executable` 指定已安装的 Chromium/Chrome。
重启前后分别运行 `scripts/verify_persistence.py --capture` 和不带 `--capture`
的同一命令，可比较会话、完整消息、最终状态和已保存计划。

## 仍有边界

- 18 条餐食仍是演示种子数据，缺少独立营养来源核验和统一克重；没有伪造数据扩容。
- 本轮未增加完整食品对比、计划编辑换餐、账户注册或多副本部署。
- 本地 Docker 镜像构建曾在拉取基础镜像阶段因 Docker Hub 连接超时失败，
  不得把本地 Python/Web 验收等同完整容器交付验收。
- GitHub Actions 只有配置和本地对应检查；未推送就没有远端 CI 执行证据。
- 公开部署仍需 HTTPS、生产密钥、基础设施暴露面检查和完整备份恢复演练。

## 实际验收

- 后端完整回归 132 项通过，覆盖率 86%（另有 1 条测试警告）；Ruff、mypy
  （91 个源文件）和前端生产构建均通过。
- Fake 核心数据集 60/60，扩展回归 8/8。
- 真实 Qwen/BGE 扩展回归最终 8/8，零降级。非法日期走程序校验、零模型调用；
  其余七个场景都有真实模型证据。输入 49,810、输出 3,803 Token，
  平均约 12.74 秒，最长约 30.64 秒；这是小样本，不是线上总体准确率或负载基准。
- Fake 与真实浏览器各通过 8 类检查，无页面脚本错误；已检查桌面与手机截图。
- 最后补充同一会话从“推荐午餐”切换到“一日三餐”的真实 Qwen/BGE 验收：
  生成 3 餐并等待确认，两个回合共 9 次模型调用、零降级；没有保存测试计划。
- 实际 HTTP 取消验证终态及消息记录；不会继续调用后续工具。
- 真实服务重启前后，两个验收用户合计 19 个会话的列表、消息和最终状态哈希一致，
  包含 4 个待确认状态；一个已保存计划及完整质量报告也一致，评测报告仍可读取。
- 对实际 SQLite 记录库备份并恢复到新路径，完整性检查通过；恢复文件包含
  20 个会话、48 条消息、4 份评测报告。实际清理只执行 dry-run，没有删除用户记录。

原始证据位于 Git 忽略的 `.runtime/completeness-*.json`、
`.runtime/completeness-web-*/`、`.runtime/backups/` 和 `.runtime/restored/`。
首次真实扩展回归中非法日期曾触发降级，原证据保留；前置校验后重跑为零降级。
