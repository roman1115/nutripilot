# 持久多会话执行改造

验收日期：2026-10-30，Asia/Shanghai。

## 范围

按 xerina 的要求，将生成任务从浏览器连接和 API 请求生命周期中分离。保留现有
Python、FastAPI、LangGraph、React、PostgreSQL、Redis 与 MCP，没有增加任务队列服务或新依赖。
营养数据、整菜口径、100 道菜谱和旧 18 道菜的退役规则未在本次改造中变化。

每个部署队列使用一个独立 Worker，默认全局并发 4、每用户 3、同会话 1。
超过并发数排队，每用户最多接收 20 个未结束任务。三个模型 Worker Agent 继续作为图节点运行，
与执行聊天任务的后台进程不是同一计数。

## 实现

1. `chat_threads`、`chat_messages`、`chat_runs`、`chat_events`、`chat_workers`
   保存归属、消息、任务、事件游标和心跳；Alembic `0002_durable_chat` 增量建表。
2. 提交用户消息、创建任务和排队事件在同一事务完成。请求编号按用户唯一，
   相同编号及内容重试复用任务，不同内容拒绝；同会话仅允许一个活动任务。
3. PostgreSQL 会话级 advisory lock 确保同队列仅一个 Worker。进程领取任务后有限并发执行，
   旧执行版本不能继续写检查点、业务数据或完成事件；写入过程持有归属保护。
4. 图采用同步节点检查点，恢复时继续同一任务。截止时间和已预留 Token 持久化，
   默认最多首次执行加一次恢复。无法确认提供方实际用量时保守保留预留，不视为免费失败。
5. 完成回复、会话快照、最终状态和完成事件在同一事务写入。旧同步 API 等待其自己的持久结果，
   不会误读同会话下一轮。评测 API 与评测 CLI 也进入同一调度队列。
6. 浏览器断开只结束订阅；SSE 按事件编号续传，刷新和重开读取一致快照再订阅活动任务。
   会话选择保存在 URL 与标签页存储；草稿按用户及会话隔离，多标签页定期同步新任务。
   餐单页刷新后台确认的写入结果；停止使用具体任务编号，防止误停下一轮。
7. 旧 SQLite 消息导入前通过 SQLite backup 生成权限为 `0600` 的快照，再原子发布备份文件。
   全量消息事务导入，编号、内容或用户冲突拒绝覆盖；原文件保留。历史缺少图状态时仍可查看消息。
8. API 与 Worker 并发初始化共享 Trace 文件时，对 SQLite 锁冲突作有上限重试。
   Docker 配置与本地启动器均包含独立 Worker，健康信息展示执行器是否在线。

## 接口

| 接口 | 行为 |
| --- | --- |
| `POST /api/v1/threads/{id}/runs` | 接收 `message`、`request_id`，持久提交，返回 202 和任务信息 |
| `POST /api/v1/threads/{id}/confirm` | 接收预览编号、决定与请求编号，持久提交确认任务 |
| `GET /api/v1/runs/{id}` | 查询归属当前用户及队列的任务 |
| `GET /api/v1/runs/{id}/events?after=N` | 重播并订阅；兼容 `Last-Event-ID` |
| `POST /api/v1/runs/{id}/cancel` | 只停止指定任务；终态重复停止不改变结果 |
| `GET /api/v1/threads/{id}/snapshot` | 一致返回历史消息、最近结果和当前队列任务 |

旧聊天、确认、流式和线程停止接口继续兼容。新前端使用以上接口。
没有 Worker 时任务仍可入库，健康信息标记 `worker_unavailable`，页面不宣称执行器已就绪。

## 实际验收

- 最终完整 pytest（含实际 PostgreSQL/Redis/Qdrant）296 项通过，1 条既有依赖警告；
  Ruff、mypy（应用及本轮脚本共 102 个源文件）、前端 typecheck/build 和两份 Compose 配置检查通过。
  全脚本 mypy 的既有 `generate_eval_dataset.py:85` 问题不在本次修复范围。
- 本机既有 PostgreSQL 已备份并升级到 `0002_durable_chat`，原数据未清除。
  备份：`.runtime/before-durable-chat-20261030.dump`。
  另创建独立临时数据库，实际执行 `0001 → 0002`，核对 5 张聊天表后删除该临时数据库。
  这不等同于备份恢复演练。
- 两份旧 SQLite 备份的 `integrity_check` 均通过；逐条核对其中 20 条与 179 条历史消息，
  PostgreSQL 中的消息编号、用户归属和完整内容一致。
- 后端覆盖用户隔离、请求幂等、同会话互斥、并发限额、排队、指定任务取消、事件重播、
  截止时间、Token 预算、确认失效、检查点恢复、历史导入和评测共享队列。
- `test_durable_processes.py` 使用真正独立的 API 与 Worker 进程及真实 PostgreSQL/Redis：
  生成中 SIGKILL API 再启动，Worker 继续；SIGKILL Worker 再启动，从检查点恢复，
  已完成 Intent 不重复。餐单写入成功但工具节点尚未保存检查点时 SIGKILL Worker，
  重启后只保留一张餐单和一个完成事件。
- `verify_durable_web.py` 使用隔离的可暂停 Fake Worker、真实 PostgreSQL 与真实 SSE，
  在 Chrome 1440×1000、390×844 各验证三会话并行与第四个排队、站内切页、
  刷新、关页重开、只停止某个会话、离线期间完成后重连、多标签页自动同步。
  无 JavaScript 错误和页面横向溢出。报告 `.runtime/durable-web/result.json`。
- 原完整浏览器业务回归通过：推荐、营养查询、历史恢复与搜索、换菜、人工确认、
  保存餐单及五种宽度检查。报告 `.runtime/durable-full-web/result.json`。
  `verify_web.py --navigation-only` 另覆盖桌面/手机切页和主动停止；此项使用模拟任务与流，
  与上述真实任务验收区分。
- 真实入口 Web 5178 / API 8006 / MCP 8012 已启动，运行独立 Worker。
  主模型探测输入 111、输出 5 Token；两条真实营养查询经刷新和关页重开后均完成，
  每条保留两条消息。8 次模型调用全部 `qwen-plus`，输入合计 9,632、输出 596 Token，
  无模型降级。实际使用 BGE、PostgreSQL、HTTP MCP；无浏览器错误。
  报告 `.runtime/durable-real-web/result.json`。这是小样本功能验收，不是整体效果或性能指标。

## 重跑

基础 PostgreSQL、Redis、Qdrant 运行后：

```bash
NUTRIPILOT_TEST_EXTERNAL=1 .venv/bin/pytest
.venv/bin/ruff check src apps tests scripts migrations
.venv/bin/mypy src apps scripts/demo_launcher.py scripts/verify_web.py scripts/verify_durable_web.py
npm --prefix web run build
```

Fake 持久化入口 5177 已启动时：

```bash
.venv/bin/python scripts/verify_durable_web.py \
  --base-url http://127.0.0.1:5177 \
  --executable "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
```

该脚本在 8007 启动并清理自有 API/Worker 测试进程，真实调用本地存储，
使用独立用户、队列和 Trace 文件，不调用付费模型。8007 需空闲。
浏览器流量转发到测试 API；生成任务、取消、持久化和事件均由该 API/Worker 实际处理。

## 限制

- 恢复单位为图节点；崩溃时在途模型调用可能重复计费，未完成节点可能重跑。
  数据库唯一键与业务幂等保留，但不能保证外部模型请求“恰好调用一次”。
- 本地后台启动不提供操作系统级守护。Worker 重启后才恢复，停机期间不生成；
  Docker 的 `unless-stopped` 提供进程退出重启。单 Worker 不支持横向扩容。
- 当前总执行时限从首次领取算起，恢复不延长，排队时间不计。反复故障达到恢复次数、
  时限或预算时明确失败，不能保证任意长中断后继续。
- 离线 demo 保持内存语义；跨服务重启恢复必须使用 persistent/real。
  Trace、评测报告仍为共享 SQLite，不在本次迁移为 PostgreSQL。
  评测批次本身不是持久工作流，API 重启后批次会标记中断，已提交的聊天任务仍继续。
- 没有执行完整应用镜像构建、公开部署、Windows 运行、生产负载、数据库断电或备份恢复演练；
  未重新运行整套 Qwen/BGE 效果数据集。事件和任务暂不自动清理，长期部署需安排保留策略。
