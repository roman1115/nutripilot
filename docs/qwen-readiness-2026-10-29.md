# Qwen 可用性与验收前置检查

日期：2026-10-29，时间显示为 Asia/Shanghai。
本轮目标为迁移后的真实 Qwen/BGE 端到端验收。**16:12:54 复查已恢复可调用**；
真实推荐冒烟、推荐 14/14、扩展 8/8 及浏览器确认保存通过，
详见 [迁移后真实模型验收](qwen-catalog-validation-2026-10-29.md)。
以下保留恢复前的拒绝记录。

## 实际请求

使用现有 `.env` 中的密钥和 `qwen-plus`，没有输出密钥或更改账号、余额、计费、模型配置。

| 检查 | 时间 | 结果 | 延迟 |
| --- | --- | --- | --- |
| 最小原始请求 | 15:58:17 | HTTP 403，`AllocationQuota.FreeTierOnly` | 253.57 ms |
| 本地真实模式启动 | 16:03:54 | 前置检查阻止启动，`MODEL_QUOTA_EXHAUSTED` | 337.89 ms |
| 最终独立前置检查 | 16:07:50 | 退出码 1，`MODEL_QUOTA_EXHAUSTED` | 316.32 ms |
| xerina 请求复查 | 16:12:54 | 退出码 0，`passed`，输入 111、输出 5 Token | 698.04 ms |

提供方实际响应说明免费额度已耗尽，需要在管理控制台处理额度或“仅使用免费额度”设置。
是否允许付费及如何调整余额由账号所有者决定；代码无法解除该账号限制。
本记录以本次接口响应为依据，不将控制台当前设置或计费状态推断为已核实事实。

这些拒绝没有返回实际 Token 用量，最终报告保存 `usage: null`，不能据此声称实际计费为零。
没有继续提交完整模型批量评测，也没有将模拟或规则降级结果标为真实模型成功。

原始证据位于 Git 忽略目录：

- `.runtime/qwen-validation-20261029/provider-probe.json`：实际 HTTP 状态、提供方错误及返回内容。
- `.runtime/qwen-validation-20261029/preflight.json`：最终前置检查，含脱敏错误分类及延迟。
- `.runtime/real-model-probe.json`：第一次启动检查的历史报告；该版尚以内部默认零记录缺失用量，
  最终实现已改为未知，以最终 `preflight.json` 的口径为准。

## 实现

- 网关区分 `MODEL_QUOTA_EXHAUSTED`、`MODEL_AUTHENTICATION_FAILED`、
  `MODEL_PERMISSION_DENIED` 和其它拒绝；保持非重试错误立即终止，不尝试备用模型。
  不回显原始错误中的潜在凭据。
- `verify_showcase.py --probe-only` 用当前主模型请求一个固定 JSON，使用 512 Token 总预算，
  不重试、不降级；检查结构化结果及正数 Token 证据，结束后关闭客户端。
  检查成功仅说明一次主模型调用可用，不代表完整业务链路通过。
- 本地启动器的 `real` 模式在读取配置、确认本地模型文件存在后检查主模型；
  通过后才执行基础设施配置、迁移及 MCP/BGE 预热。
  本次启动检查失败后没有生成 `real-8006-*.pid`，原 5177 持久化入口仍可用。
- `verify_real_regressions.py` 在提交评测前运行同一检查；失败时退出，不创建批量评测。
  检查通过后还要求 API 为 Qwen/BGE/PostgreSQL/HTTP MCP，
  并逐事件核对模型身份、实际 Token、降级及模型错误。
- 原有健康检查不代表远程模型可用性；本轮未改变其含义。
  完整 Compose 启动路径没有接入新的自动前置检查，应先手动运行检查命令。

## 恢复前的验证

- 新增 12 项测试：提供方拒绝分类/脱敏、非重试与无备用模型切换、
  检查成功/额度不足/缺失用量、客户端关闭、批量执行门槛及模型事件核验。
- `NUTRIPILOT_TEST_EXTERNAL=1 .venv/bin/pytest`：257 项通过，1 条既有依赖警告。
  外部存储检查实际使用本机 PostgreSQL、Redis、Qdrant，对话使用 Fake。
- Ruff 全仓通过；mypy 97 个源文件通过，包含启动与验收脚本；前端 typecheck/build 通过。
- 实际真实启动在前置检查阶段退出；当前运行的持久化演示仍为 Fake，
  不能当作 Qwen/BGE 对话验收。
- 本轮未重新进行浏览器验收、完整容器构建、备份恢复或营养核验。
  前一轮持久化浏览器证据见 [存储验收](persistence-validation-2026-10-29.md)。

## 恢复后的验收命令

额度恢复后实际采用以下命令复查，再启动完整链路：

```sh
.venv/bin/python scripts/verify_showcase.py --probe-only \
  --output .runtime/qwen-validation-20261029/recovered-probe.json
```

必须返回退出码 0、`status: passed` 和非零输入/输出 Token，才能继续：

```sh
USER_TIMEZONE=Asia/Shanghai sh scripts/start_real.sh --background \
  --api-port 8006 --web-port 5178 --mcp-port 8012

.venv/bin/python scripts/verify_showcase.py --base-url http://127.0.0.1:8006 \
  --user-id 910033 --output .runtime/qwen-validation-20261029/showcase.json

.venv/bin/python scripts/verify_real_regressions.py --base-url http://127.0.0.1:8006 \
  --dataset evals/datasets/catalog-recommendations.json --timeout 1800 \
  --output .runtime/qwen-validation-20261029/catalog.json

.venv/bin/python scripts/verify_real_regressions.py --base-url http://127.0.0.1:8006 \
  --dataset evals/datasets/real-regressions.json --timeout 1800 \
  --output .runtime/qwen-validation-20261029/regressions.json

.venv/bin/python scripts/verify_web.py --base-url http://127.0.0.1:5178 --real \
  --executable "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-id 910034 --output-dir .runtime/qwen-validation-20261029/web
```

上述命令为恢复后的验收入口；恢复前没有运行通过。本次复查后已启动真实服务，
推荐冒烟、推荐 14/14、扩展 8/8 与浏览器流程均通过；
失败历史与最终结果以新验收记录和当前状态页为准。
固定场景覆盖点名推荐、条件筛选、整菜营养与范围、过敏约束、未知值、无候选、多轮换菜和餐单预览；
浏览器脚本补充换菜后人工确认与保存的一致性。应使用没有已存餐单的新验收用户。
小样本结果只用于回归；所有失败、Token、延迟及降级必须保留，不宣称整体模型准确率。
