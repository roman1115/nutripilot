# 停止生成与会话恢复故障验收

验收日期：2026-10-29，Asia/Shanghai。真实 HTTP 最终检查时间为 17:26:26。

## 现象与原因

xerina 在真实入口 `http://127.0.0.1:5178/#chat` 看到顶部“已连接”，聊天区域却提示“无法连接 Agent 服务：Failed to fetch”。

健康检查可正常返回，但没有检查会话 Checkpointer 的实际读写。原日志显示，请求被取消后，Graph 尝试保存停止状态；AnyIO 的取消作用域再次打断收尾中的 PostgreSQL pipeline。原 Checkpointer 长期共享一条连接，该连接发生 `cannot exit pipeline mode while busy` 后，后续会话读取继续出现 `INSERT 0 1` 无记录和 `PIPELINE_SYNC` 异常，恢复接口返回 500。浏览器接收不到完整的跨域错误响应，显示为网络失败。

原始日志已保留在 Git 忽略的 `.runtime/real-8006-api.error-before-cancel-fix.log`。此前真实模型与顺序浏览器回归没有覆盖这一取消边界，不能用此前通过结果否定本次故障。

## 修复范围

- Graph 的普通调用和流式调用在取消后保护终止状态写入，写入结束后仍向外传播取消。
- ChatService 的聊天、流式响应、餐单确认取消后，保护停止响应保存到历史记录。取消不会撤销已完成的餐单写入。
- PostgreSQL Checkpointer 使用已安装依赖中的异步连接池，取出前检查连接；损坏连接可被丢弃和替换，随应用生命周期关闭。
- 页面恢复失败后允许下次连接检查重试；恢复成功后清除旧错误。
- 续聊验收额外发现模型已识别“糖拌西红柿”，但工具参数仍选了其它候选编号。工具执行边界现以本轮 `food_name` 查询营养，移除冲突编号；精确、模糊和未知菜名继续经过既有名称解析，不放宽匹配条件。

未新增依赖，未修改凭据、计费或模型配置，未清空会话或数据库。真实 API 已重启加载最终实现；Web 5178、API 8006、MCP 8012 可用，原 Fake 持久化入口 5177/8005 保留。

## 自动化回归

新增 8 项用例：

1. AnyIO 持续取消作用域下，普通聊天、流式聊天、餐单确认均保存停止状态和历史、释放会话占用，并允许继续对话；确认流程保留已成功写入的餐单。
2. PostgreSQL 连接池归还已关闭连接后，可以重新读取 Checkpoint，退出上下文后连接池关闭。
3. 真实 PostgreSQL 连续三次流式取消，期间并发读取状态和消息，之后仍可恢复并完成下一轮。
4. 模型错误选择其它菜品编号时，精确菜名返回正确营养，模糊和未知菜名仍进入澄清。

取消保护的三个用例在进程内禁用保护时全部失败，恢复保护后通过；没有为复现回滚工作区。菜名回归同样在修复前暴露错误选择。

最终实际执行：

```sh
NUTRIPILOT_TEST_EXTERNAL=1 .venv/bin/pytest
.venv/bin/ruff check .
MYPYPATH=src:. .venv/bin/mypy --explicit-package-bases src apps \
  scripts/demo_launcher.py scripts/verify_persistence.py \
  scripts/verify_showcase.py scripts/verify_real_regressions.py \
  scripts/import_food_catalog.py scripts/draft_food_catalog.py
npm --prefix web run typecheck
npm --prefix web run build
```

- 完整后端：280 项通过，1 条既有依赖警告，包含实际 PostgreSQL/Redis/Qdrant。
- Ruff 全仓通过。
- 应用及上述相关脚本的 mypy 共 99 个源文件通过。
- 前端 typecheck/build 通过。
- 另尝试将全部 `scripts/` 纳入 mypy：`scripts/generate_eval_dataset.py:85` 存在既有 `slots` 推断为 `object` 的类型错误，本次未改动该生成脚本，不能表述为全仓全部脚本类型检查通过。

## 真实 HTTP 与浏览器

- 截图中的原会话恢复成功，不再返回数据库 500。
- 在浏览器短暂注入会话恢复请求失败，确认出现网络错误；移除拦截后点击“重新检查服务”，同一会话恢复且旧错误消失。故障注入已完全解除。
- 真实 Qwen 生成期间点击停止，随即刷新，恢复到“已停止本轮请求”及对应历史记录。
- 另通过真实 HTTP 在收到 Intent 进度后直接关闭 SSE 连接；随后接口返回 `FAILED` 和停止响应，Trace 包含 `execution.cancelled`，停止消息已保存。
- 对原会话、浏览器验收会话和断开验收会话的状态/消息接口执行 30 次并发读取，全部 HTTP 200，且跨域响应头符合 Web 5178。
- 在直接断开的同一会话继续查询“糖拌西红柿有多少热量”，最终返回 `COMPLETED`，整道菜约 113.4 千卡；4 次模型调用均为 `qwen-plus`，输入 4,831、输出 298 Token，无备用模型或规则降级。这是本次续聊样本用量，不是整轮排错费用。
- 浏览器再次查询得到相同的正确菜名与营养，刷新后消息仍可恢复，最终浏览器错误/警告日志为空；新 API 日志没有 pipeline 或未处理异常。

本地证据：

- `.runtime/cancel-recovery-20261029.json`
- `.runtime/cancel-recovery-20261029.png`
- 直接断开 Trace：`a427a794-4c8c-471b-8e6c-fbe6e4cffdbc`
- HTTP 续聊 Trace：`28da163a-20d7-4997-a188-7e44059f50fd`
- 浏览器修正后续聊 Trace：`d32a013b-5866-4768-8ac9-dd051451f0f8`

## 限制

本轮针对取消、恢复和错误菜名选择验收，没有重新执行此前推荐 14 项与扩展 8 项的整套真实模型评测，也没有进行生产负载测试、完整应用容器构建或备份恢复演练。历史错误回答保留在验收会话中，没有修改已有消息。营养仍为整道菜的配方估算。
