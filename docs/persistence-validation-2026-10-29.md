# 真实存储迁移与重启验收

验收日期：2026-10-29。范围为真实 PostgreSQL、Redis、Qdrant 及应用进程的迁移和重启恢复。
对话使用 Fake，不代表真实 Qwen 效果。

## 变更

- 启动器新增 `--mode persistent`，复用现有外部服务配置、迁移和 MCP 启动逻辑。
  Fake/Hashing/Lexical 搭配 PostgreSQL、Redis、Qdrant、HTTP MCP，无需模型密钥或 ML 权重。
  `demo` 继续使用内存；`real` 继续使用 Qwen/BGE，没有新增依赖。
- 普通和 BGE 模式使用不同索引集合，防止 256 维 Hashing 与 1024 维 BGE 向量混用。
- `verify_persistence.py` 将画像纳入重启前后快照，并要求至少存在一份已保存餐单，
  检查实际用户与 `--user-id` 一致，避免空餐单或错误身份得到通过结果。
- 新增 6 项启动器/核验单元测试和 4 项可选的真实服务集成测试。
  外部测试只连接本机服务，使用合成用户及独立临时向量集合；向量测试结束删除自己的测试集合，
  不删除业务集合。合成用户的验收记录保留在本地数据库中。

## 真实迁移

本机 Docker Server 29.6.1；使用仓库既有 Compose 的 PostgreSQL 16、Redis 7、
Qdrant 1.14.1 镜像与原有数据卷。API、Web、MCP 使用本地进程，没有构建整套应用容器。

| 核验 | 结果 |
| --- | --- |
| 迁移前数据库 | 18 条旧餐食、3 份餐单、2 条反馈、3 份画像 |
| 迁移后菜品 | 100 条在用、18 条退役 |
| 原餐单、反馈、画像 | 原记录逐条比较一致，没有被启动种子覆盖 |
| 旧菜名、原六项营养、份量 | 全部保留 |
| 历史来源说明 | 原“人工审核”字样更新为未核验演示估算，补入当前元数据字段 |
| `meal_search` | 18 条迁为 100 条；旧编号 1–18 向量为 0 |
| `meal_search_bge_m3` | 18 条迁为 100 条；旧编号 1–18 向量为 0 |
| 无关向量 | 独立实际服务测试中保留，重复初始化不增加点数 |
| 历史访问 | 旧编号仍可查询；旧幂等键可重放；新餐单拒绝退役编号 |

BGE 迁移使用本地 BGE-M3、BGE Reranker 的 CPU 推理，实际查询“西葫芦炒鸡蛋”首条为该菜；
没有调用 Qwen。每个模式启动时仅迁移自身集合；本次两个集合均已分别初始化和验收。

## 实际重启

通过 HTTP 创建合成用户 910031 的画像、已确认餐单、反馈，以及另一段待确认餐单对话。
记录用户可见快照后，停止本次启动的 API/MCP/Web，执行
`docker compose restart postgres redis qdrant`，再启动相同持久化模式与同一运行目录。

| 核验 | 结果 |
| --- | --- |
| 用户可见快照 | 2 段会话、消息、1 份已保存餐单、待确认状态、画像全部一致 |
| 重启前后 SHA-256 | `046d943559a214567cfa2f77f67c91d985e55dc8f99205eac6e1d0d7f0f6992b` |
| Redis AOF 验收键 | 值保留，TTL 仍为正 |
| Qdrant 两个集合 | 重启后均为 100 条，旧编号仍为 0 |
| 待确认餐单继续确认 | 原确认编号可用，保存后共 2 份餐单 |
| 再次确认同一操作 | HTTP 409，仍为 2 份餐单 |
| 反馈重放 | 相同幂等键仍返回原记录 |

另用两个不同 Bearer 身份的真实 PostgreSQL Checkpoint 集成测试验证：他人会话与消息返回 404，
他人餐单列表为空，伪造 `X-User-ID` 不改变认证身份。该测试在同一 Python 进程中重建应用三次，
分别验证确认前恢复、确认保存和再次恢复。实际 HTTP 重启检查使用本地无令牌的演示入口；
这不表示已验收公开访问部署。

## 质量检查

- `NUTRIPILOT_TEST_EXTERNAL=1 .venv/bin/pytest`：245 项通过，1 条既有依赖警告。
- 默认 `.venv/bin/pytest`：241 项通过、4 项外部服务测试跳过，1 条既有依赖警告。
- Ruff：`src apps tests scripts migrations` 全部通过。
- mypy：95 个源文件通过，包含本次启动器和重启核验脚本。
- 前端 typecheck 与生产 build 通过。
- Chrome 1440×1000、390×844、320×740 验证餐单预览、换菜、确认保存与来源/营养范围；
  首页另验 1024×768、768×1024。推荐、营养查询、Trace、刷新恢复、历史搜索通过，
  无 JavaScript 错误或横向溢出。已查看桌面及全新手机视口截图。
- 快速连续切换视口后的部分 Chrome 全页截图存在拼接伪影；
  另在全新手机视口核对只有一个应用根节点、三行菜品，并单独截图顶部和底部。

## 启动与重跑

从项目根目录前台启动，端口被占用时使用其它空闲端口：

```sh
USER_TIMEZONE=Asia/Shanghai sh scripts/start_demo.sh --mode persistent \
  --api-port 8005 --web-port 5177 --mcp-port 8011
```

Web：`http://127.0.0.1:5177`。API：`http://127.0.0.1:8005`。按 `Ctrl+C` 停止本次前台应用，
保留基础容器、数据库卷和 `.runtime/persistent-observability.sqlite3`。
启动脚本同样支持 `--background`；本次后台 PID/日志前缀为 `.runtime/persistent-8005-`。
原 8003/5175、8004/5176 的内存演示进程未由本次操作停止或转换。

```sh
docker compose up -d --wait postgres redis qdrant
NUTRIPILOT_TEST_EXTERNAL=1 .venv/bin/pytest
.venv/bin/ruff check src apps tests scripts migrations
MYPYPATH=src:. .venv/bin/mypy --explicit-package-bases src apps scripts/demo_launcher.py scripts/verify_persistence.py
npm --prefix web run typecheck
npm --prefix web run build
.venv/bin/python scripts/verify_web.py --base-url http://127.0.0.1:5177 \
  --executable "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-id 910032 --output-dir .runtime/persistent-web
```

浏览器脚本的 `--user-id` 应使用没有已存餐单的新验收用户；910032 已在本次使用。
外部集成测试默认跳过，设置 `NUTRIPILOT_TEST_EXTERNAL=1` 才会写入本机验收数据。

对已创建画像、会话和餐单的用户，重启快照核验方式：

```sh
.venv/bin/python scripts/verify_persistence.py --base-url http://127.0.0.1:8005 \
  --user-id 910031 --record .runtime/persistence-new.json --capture
# 停止应用、重启基础服务，再使用相同配置和运行目录启动。
.venv/bin/python scripts/verify_persistence.py --base-url http://127.0.0.1:8005 \
  --user-id 910031 --record .runtime/persistence-new.json
```

## 备份、证据与边界

Git 忽略目录 `.runtime/storage-backup-20261029/` 保存迁移前 PostgreSQL custom dump、
原业务记录快照、两个 Qdrant 集合快照及 SHA-256 清单。PostgreSQL 备份已用 `pg_restore --list`
读取目录；Qdrant 下载内容与服务返回的校验和一致。**没有执行整库恢复演练**。

同目录另有 `migration-check.json`、`bge-migration.json`、`restart-snapshot.json`、
`infrastructure-restart.json`、`post-restart-confirmation.json` 等证据；
浏览器结果与截图在 `.runtime/persistent-web/`。
初次捕获的 `restart-snapshot.json` 是继续确认第二份餐单之前的历史快照，
后续操作改变了餐单和会话，不能再用它直接比较当前数据；重新验收应另行捕获。

本次只处理持久化与迁移，没有重验真实 Qwen、完整 API/Web Docker 构建、公开部署或跨机器恢复。
默认 `demo` 仍为内存模式；当前持久化入口为 Fake 对话，不能表述为真实模型展示。
运行目录中的 SQLite 负责会话消息、Trace 和报告，业务数据与图状态在 PostgreSQL；
迁移或备份时必须同时保留两者。
