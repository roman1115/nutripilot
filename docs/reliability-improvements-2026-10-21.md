# 可靠性修复与使用说明

日期：2026-10-21，Asia/Shanghai。

## 已修复

- 推荐摘要、推荐理由、警告与后续动作不再直接使用模型自由生成的个人事实。
  餐食营养、标准份量和过敏原来自餐食工具记录；用户过敏原单独来自画像工具。
  未经检索验证的餐食不会进入推荐。营养查询同样采用记录中的数值与过敏原。
- 支持维护餐食别名，已补“鸡胸肉藜麦能量碗”。近似或部分菜名需要选择候选，
  不会直接套用另一道菜的营养数值。查不到或出现不可重试错误时结束工具循环。
  用户可以在原会话继续补充完整菜名。
- 意图槽位校验类型、范围和允许的字段，并将这些约束提供给真实模型。
  可选饮食限制不再导致计划被追问阻断。明确日期和一天/一周范围由程序解析；
  不存在的日期要求澄清；写入前检查计划日期与用户指定日期一致。
- 接通请求总时限、累计 Token 预算和模型输出长度限制。模型重试反馈具体 Schema
  字段错误，失败尝试的已知 Token 用量也计入统计。规划阶段压缩检索上下文，
  保留菜名、餐次、营养及安全元数据。
- 停止按钮调用服务端取消接口；工具不再吞掉任务取消。同一会话不能同时执行
  多个请求或确认；写操作幂等键包含 turn_id，避免不同请求复用模型 call_id。
- 实际部署及启动脚本使用 SQLite 持久化 Trace 和完整评测报告。重启前未结束的
  评测标记为 interrupted，不自动重新付费运行。业务数据仍由 PostgreSQL 保存，
  对话由原有 PostgreSQL Checkpoint 保存。
- 可配置访问令牌与用户映射，身份由服务端确定。保护用户资源、Trace、评测归属，
  限制请求频率、单批评测数量及并发评测。production 模式要求配置令牌。
  浏览器提供令牌连接界面，令牌只存当前浏览器会话，不注入前端构建产物。
- 种子餐食检查重复编号、重复别名、食材、过敏原显式标注、来源和份量；
  营养数值拒绝 NaN/Infinity。保留现有 18 条餐食，没有编造新营养记录。
- Qdrant 客户端锁定 1.14.x，与现有服务端 1.14.1 对齐。CI 加入后端类型检查
  和可重复的浏览器回归任务。

## 真实模式启动

依赖：Python 3.12、Node.js、运行中的 Docker、`.env` 中有效的 `MODEL_API_KEY`，
以及已下载的本地 BGE 模型。首次准备：

```bash
uv sync --frozen --extra dev --extra ml --extra e2e
npm --prefix web ci
```

macOS/Linux：

```bash
sh scripts/start_real.sh --api-port 8001 --web-port 5174
```

Windows：

```powershell
.\scripts\start_real.ps1 --api-port 8001 --web-port 5174
```

默认使用本项目验收时保存的模型目录：

- `.runtime/models/bge-m3`
- `.runtime/models-parallel/bge-reranker-v2-m3`

也可以传 `--embedding-model /path/to/bge-m3 --rerank-model /path/to/reranker`。
支持 `model.safetensors` 或 `pytorch_model.bin`；启动时不自动下载模型。

启动流程：检查依赖、模型和端口，启动本项目 PostgreSQL/Redis/Qdrant，
执行 Alembic 迁移，启动 MCP 并预热真实 BGE，最后启动 API/Web。
已有基础设施时可加 `--no-infrastructure`。默认前台运行，Ctrl+C 停止 API/MCP/Web，
数据库容器和数据卷保留。`--background` 用于后台运行；对应 PID 文件保存在
`.runtime/real-<API端口>-*.pid`。不同 API 端口使用独立日志和 PID 文件。

`.env` 中可设置：

```dotenv
AGENT_DEADLINE_SECONDS=90
AGENT_TOKEN_BUDGET=24000
RUN_STORE_PATH=.runtime/real-observability.sqlite3
API_RATE_LIMIT_PER_MINUTE=60
```

Token 预算使用服务商实际返回的累计用量，并以保守估算预留下一次输入与输出。
估算不是服务商精确 tokenizer，也不代表账单绝对上限。取消可以停止服务端后续
调用并关闭在途请求，不能撤销服务商已经执行或计费的推理。

## 访问控制

需要隔离用户时，在 `.env` 配置 `API_ACCESS_TOKENS`，其值是 JSON 对象：
每个键为至少 32 字符的随机访问令牌，值为所属用户的正整数 ID。
使用 Python `secrets.token_urlsafe(32)` 生成令牌，不要把实际令牌提交到 Git。

启动后在页面输入该令牌；身份由服务端映射，不能通过 `X-User-ID` 修改。
API 脚本读取环境变量 `NUTRIPILOT_ACCESS_TOKEN`，通过 Bearer Header 访问。
Compose 默认只向本机回环地址开放端口。

该方案适合当前个人单机部署。SQLite、内存限流与运行任务锁按单 API 实例设计；
多进程、多副本部署仍需要共享的任务队列、限流与记录存储。公开部署还需要
HTTPS、真实秘密配置、备份与运维配置，不能将本轮验收当作完整生产验收。

## 回归入口

```bash
uv run --extra dev pytest
MYPYPATH=src uv run --extra dev mypy --explicit-package-bases src/nutripilot apps
uv run --extra dev ruff check .
npm --prefix web run build
```

Fake 的 60 条固定回归：

```bash
MODEL_PROVIDER=fake APP_MODE=demo uv run nutripilot-eval \
  --dataset evals/datasets/core.json --label fake-core
```

真实服务启动后，下列脚本实际调用模型，会产生调用费用：

```bash
.venv/bin/python scripts/verify_showcase.py \
  --base-url http://127.0.0.1:8001 \
  --output .runtime/real-showcase.json
.venv/bin/python scripts/verify_real_regressions.py \
  --base-url http://127.0.0.1:8001 \
  --output .runtime/real-regressions.json
.venv/bin/python scripts/verify_cancellation.py \
  --base-url http://127.0.0.1:8001
.venv/bin/python scripts/verify_web.py \
  --base-url http://127.0.0.1:5174 --real
```

浏览器依赖通过 `uv run --extra e2e playwright install chromium` 安装；
已有浏览器时可传 `--executable`。浏览器脚本支持环境变量
`NUTRIPILOT_ACCESS_TOKEN`，会通过连接界面完成鉴权。自动验收使用专用测试用户；
启用鉴权时，请为测试用户分配对应令牌，并按脚本的 `--user-id` 或数据集配置匹配。

## 本轮实际验证

- 后端 107 项测试通过，本轮测试覆盖率 85%；Ruff、mypy（89 个源文件）
  和前端生产构建通过。
- 原有 Fake 数据集 60/60 通过。
- 严格真实 Qwen/BGE 推荐验收通过，三个 Worker 均有真实模型与 Token 证据，
  没有降级。该次 5 次调用共 8,525 输入、1,110 输出 Token。
- 新增 4 条真实回归最终 4/4 通过，均无降级：推荐解释、营养别名、未知菜名退出、
  指定 2026-10-22 开始的一周晚餐待确认计划。合计 22,479 输入、2,783 输出 Token；
  四个样例平均约 17.60 秒，最长约 34.62 秒。未以这些小样本宣称整体准确率。
- 实际 HTTP 流取消通过，Trace 记录 execution.cancelled，之后未调用工具。
- 重启真实 API 后，原 Trace 和评测报告仍可读取。
- 浏览器在真实模式下通过推荐解释、Trace、别名查询、刷新恢复和移动端检查；
  另在令牌鉴权模式下通过相同检查，无页面脚本错误。
- 完整 API/Web Docker 镜像构建与生产环境验收未包含在本轮。

原始证据保存在 Git 忽略的 `.runtime/fixes-*.json` 和 `.runtime/fixes-web*/`。
首次真实回归曾出现周计划槽位失败，该问题修复后重跑通过，前后证据均保留。
