# 会话回收站验收

验收日期：2026-11-04。

## 行为

- 点击历史会话删除按钮，直接移入回收站，不显示确认弹窗。
- 删除当前会话后进入新会话；回收站可搜索、分页，显示到期时间。
- 恢复后打开原会话，保留消息、推荐记录、待确认状态及图上下文，可以继续追问。
- 保留期从移入时间起计七天，即168小时，不受时区和夏令时影响。
  重复删除不刷新时间；恢复后再次删除重新计时。
- 到期后禁止恢复，并从可恢复列表隐藏。Worker 启动时及每分钟清理最多100个过期会话，
  包含 checkpoint、会话摘要、消息、任务和任务事件。
- 清理先持久化不可恢复标记，再删除 checkpoint 与会话记录。
  checkpoint 或数据库失败会保留待清理状态，下次重试，避免恢复出不完整上下文。
  Worker 未运行时物理删除延后，恢复接口仍执行七天期限校验。
- 仅允许操作本人会话。回收站会话不能通过旧 URL、消息/快照、任务/事件接口读取，
  也不能用新请求或旧幂等请求直接续聊；必须先恢复。
- 排队、执行、停止中的会话仍禁止移入回收站，避免任务写回与删除竞争。

## 存储与迁移

新增迁移 `0003_conversation_trash`，增加：

- `chat_thread_trash`：删除时间及不可恢复清理状态，与原会话关联。
- `chat_history_imports`：已导入及已彻底删除的会话编号标记，不保存消息正文。
  原 SQLite 源库和备份不自动覆盖或清除；标记防止删除内容重启后被再次导入，
  并阻止过期会话编号重新使用，避免旧页面或并发清理误操作新会话。

新增表也纳入 `ChatTaskStore.initialize`，保持内存/SQLite 演示和 PostgreSQL 的行为一致。
没有新增依赖、独立服务或定时任务平台，未改模型回答与菜谱数据。
本地 PostgreSQL 已从 `0002_durable_chat` 升级到 `0003_conversation_trash`。

## 本轮验证

- 定向历史、耐久任务、运行时及导入测试：31通过、1外部服务测试跳过。
- 随后启用外部服务，真实回收站测试及耐久任务测试：15通过，无跳过。
- PostgreSQL 实测包含：移入回收站、应用重建后列表仍在、恢复快照完全一致、
  按原候选继续查询第一道菜做法、过期不可恢复、消息/任务/事件/图状态实际清除。
- 时间边界使用受控时间及过期时间戳测试：七天前可恢复，七天到期不可恢复；
  重复删除不延期，恢复后再次删除重新计时。
- 覆盖用户隔离、搜索通配符转义、分页、存储重建、旧幂等请求阻止、
  checkpoint 故障重试、Worker 启动清理及旧源库不复活历史。
- Ruff 相关文件通过；mypy 应用及浏览器脚本共93文件通过；
  前端 `npm run typecheck`、`npm run build` 通过。
- 启用实际 PostgreSQL/Redis/Qdrant 与独立进程恢复的最终完整后端回归528通过，
  1条既有依赖警告，无跳过。覆盖Worker收尾与移入回收站竞争时仍能处理后续任务，
  以及过期会话编号不能被旧请求重新创建。

浏览器执行命令：

```sh
.venv/bin/python /Users/jiang/.codex/skills/webapp-testing/scripts/with_server.py \
  --server 'APP_MODE=demo MODEL_PROVIDER=fake EMBEDDING_PROVIDER=hashing RERANK_PROVIDER=lexical MCP_ENABLED=false RUN_STORE_PATH=.runtime/conversation-trash-web/traces.sqlite3 WEB_ORIGIN=http://127.0.0.1:5178 .venv/bin/python -m uvicorn apps.api.main:app --host 127.0.0.1 --port 8019' \
  --port 8019 -- \
  .venv/bin/python scripts/verify_conversation_trash.py \
  --api-url http://127.0.0.1:8019 \
  --executable '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
```

桌面1440、手机390/320均验证免确认删除、回收站刷新、恢复后原消息与快照不变、
刷新后恢复仍有效、键盘切换分类和无横向溢出；无确认对话框、页面 JavaScript 错误。
已目视查看1440和320回收站截图。
报告与截图位于 `.runtime/conversation-trash-web/`。
API使用隔离 Fake 模型，没有调用付费模型，也不代表回答质量通过。

## 运行状态与限制

- 原 Web5178、API8006 和独立 Worker 已重载，继续使用原配置、队列、数据库和 MCP。
- 最终重载前队列无活动任务；重载前后552个会话、2278条消息数量与最新会话抽查快照一致，
  API/Worker就绪，回收站接口及Web返回200。
- 物理删除由 Worker 分批执行；停止运行、存储错误或积压会推迟实际清理时间，
  不会延长用户可恢复期限。
- 未进行真实等待七天、生产负载、备份恢复演练或模型回答质量复测。
