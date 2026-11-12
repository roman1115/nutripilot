# 本地链路验收记录

验收日期：2026-10-21（Asia/Shanghai）。

## 验证范围

本轮仅处理本地运行与验证，不扩展业务功能。环境为 macOS ARM64、
Python 3.12.15、Node.js 24.18.0。前后端依赖分别按 `uv.lock` 和
`web/package-lock.json` 安装。

验证分为两种模式：

| 模式 | 模型与检索 | 存储与工具通信 |
| --- | --- | --- |
| 本地演示 | FakeModelGateway、Hashing Embedder、Lexical Reranker | 内存存储、进程内 MCP |
| 外部基础设施验收 | FakeModelGateway、Hashing Embedder、Lexical Reranker | PostgreSQL、Redis、Qdrant、独立 HTTP MCP |

外部模式使用 Compose 启动 PostgreSQL、Redis、Qdrant，API 与 MCP
使用本机虚拟环境中的 Python 启动。未验证 API/Web 容器镜像构建，
也未接入 Qwen 或下载 BGE 模型，不能将本次结果表述为真实模型效果验收。

## 实际结果

| 检查 | 结果 |
| --- | --- |
| 后端 pytest | 72 项通过，整体语句覆盖率 83% |
| Ruff | 通过 |
| mypy | 85 个源码文件检查通过 |
| 前端 TypeScript 检查 | 通过 |
| 前端生产构建 | 通过 |
| Fake 确定性评测 | `nutripilot-core-v2`，60/60 通过 |
| Compose 配置检查 | 通过 |
| Alembic 迁移 | 首次执行及重复执行均通过 |
| Mac 启动器 | 默认端口与自定义端口均启动成功 |
| 端口占用保护 | 拒绝重复启动，已运行服务不受影响 |
| 自定义前端 API 地址与 CORS | 通过 |
| Ctrl+C 停止 | 前后端进程均退出，端口释放 |

## 浏览器链路

使用 Chromium 实际操作页面，验证：

1. 页面连接 API，显示实际运行模式。
2. 发送推荐请求，收到 SSE 进度、餐食推荐、营养信息及引用。
3. 从对话打开 Trace 页面。
4. 提交一周晚餐计划，明确确认后保存，计划页显示 7 条餐食明细。
5. 从计划页提交餐食反馈。
6. 修改画像并确认保存，刷新后读取到修改；随后恢复原姓名。
7. 从评测页发起评测，最终显示 60 项通过、0 项失败。
8. 390 × 844 移动视口可导航、输入，无页面横向溢出。

浏览器未出现 JavaScript 页面异常或非预期 HTTP 失败。
新会话尚无服务端状态时的查询 404 属于前端已处理的空状态。

## 外部基础设施链路

实际验证：

1. API 使用 PostgreSQL Checkpoint 和 Redis 初始化。
2. 用户画像写入 PostgreSQL。
3. 推荐经过独立 Streamable HTTP MCP 与 Qdrant 检索，未触发降级。
4. 一周晚餐计划进入待确认状态。
5. 停止并重启 API、MCP 后，恢复相同待确认操作。
6. 确认后只新增一份计划；再次确认返回 409，未重复写入。
7. 使用相同幂等键提交反馈，返回同一反馈记录。
8. 再次重启 API，画像、计划和已完成会话均可读取。
9. HTTP MCP 拒绝未携带内部 Token 的请求，返回 401。
10. Qdrant 集合包含全部 18 条种子餐食向量。

外部验证使用专用用户 `424242`，数据保留于本项目的 Docker 数据卷中。

## 本轮变更

- `scripts/demo_launcher.py` 支持 Mac/Linux Python 路径与进程组。
- 新增 `scripts/start_demo.sh`，前台运行并在 Ctrl+C 时清理服务。
- 支持自定义端口，并同步前端 API 地址和 CORS。
- 启动前检查端口占用；启动失败时回收已创建的子进程。
- README 补充 Mac/Linux 安装、启动和停止说明。

## 验证边界与后续商议项

- Qwen 真实模型、BGE Embedding/Reranker 尚未验收。
- 完整 API/Web Docker 镜像构建尚未验收。
- Qdrant 服务端为 1.14.1，锁定的客户端为 1.18.0；测试调用成功，
  但客户端发出了版本兼容性警告，版本对齐留待后续处理。
- 演示模式数据仍为内存数据，停止服务即清空。
- 本次未新增认证、扩大数据集或改动产品界面。

## 本机证据

以下文件为本机生成内容，不随 Git 提交：

- `evals/reports/local-chain-20261021.json`
- `evals/reports/local-chain-20261021.md`
- `.runtime/browser-checks.json`
- `.runtime/external-checks.json`
- `.runtime/browser-recommendation.png`
- `.runtime/browser-plan.png`
- `.runtime/browser-evaluation.png`
- `.runtime/browser-mobile.png`
- `.runtime/launcher-check.log`
- `.runtime/external-api.log`
- `.runtime/external-mcp.log`
