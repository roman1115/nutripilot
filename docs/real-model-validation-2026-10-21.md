# Qwen 与 BGE 真实模型验收

验收日期：2026-10-21（Asia/Shanghai）。

## 结论与范围

真实 `qwen-plus`、`BAAI/bge-m3`、`BAAI/bge-reranker-v2-m3`
均已执行推理。Qwen、BGE、PostgreSQL、Redis、Qdrant 和独立
Streamable HTTP MCP 的联合链路通过本机验收。

本次是小样本功能验收，不是 60 条真实模型准确率评测。上一次 60/60
仍只属于 Fake 确定性回归，不能与本次结果混用。

本轮使用本机 Python 启动 API/MCP，使用 Docker 启动数据库、缓存和
向量库；完整 API/Web 容器镜像构建仍不在本次验收范围内。

## 环境与模型

- macOS ARM64，24 GiB 内存。
- Python 3.12.15，PyTorch 2.13.0，sentence-transformers 5.6.1。
- BGE 使用 CPU 推理，测试进程使用 4 个计算线程。
- Qwen 使用 `.env` 中的凭证访问 DashScope 兼容接口。
- `.env` 已确认被 Git 忽略；验收报告不包含 API Key。

Hugging Face 大文件 CDN 的 TLS 连接失败，因此改从 ModelScope 同名
BAAI 镜像下载模型。所需文件逐一检查大小和 SHA-256，两个主权重的
SHA-256 另与 Hugging Face 对应模型的元数据核对一致：

| 模型 | 主权重大小（字节） | SHA-256 |
| --- | ---: | --- |
| BGE-M3 | 2271145830 | `b5e0ce3470abf5ef3831aa1bd5553b486803e83251590ab7ff35a117cf6aad38` |
| BGE Reranker v2-M3 | 2271071852 | `d9e3e081faff1eefb84019509b2f5558fd74c1a05a2c7db22f74174fcedb5286` |

本机已验证模型目录：

- `.runtime/models/bge-m3`
- `.runtime/models-parallel/bge-reranker-v2-m3`

这些模型文件保留在本地，不随 Git 提交。

## Qwen 单独验证

先使用真实 Qwen 与离线检索隔离验证模型侧，三个样例均无模型降级：

| 样例 | 结果 | 本次耗时 |
| --- | --- | ---: |
| 清淡、高蛋白、600 千卡以内的午餐推荐 | 完成，返回餐食、营养与引用 | 28.90 秒 |
| 查询“鸡胸藜麦能量碗”的营养 | 完成，营养工具成功返回权威记录 | 14.64 秒 |
| 从 2026-10-22 起安排 7 天晚餐 | 生成 7 条明细，进入人工确认状态 | 24.49 秒 |

这三次成功样例共记录 23808 个输入与输出 Token。该数值不包含
首次探索请求和后续联合验收的调用，不代表本轮全部调用账单。

## BGE 单独验证

- Embedding 输出形状为 `3 × 1024`，元素均为有限数，向量范数约为 1。
- 相关句子相似度为 0.8444，无关句子为 0.4245。
- Reranker 对 18 条餐食产生有限且有差异的分数；精确菜名
  “鸡胸藜麦能量碗”排名第一，分数约 0.9972。
- 混合检索正常完成，午餐、600 千卡上限、30 克蛋白质下限、
  花生及香菇排除条件均保留。
- 同一进程的单次冷启动 Embedding 耗时约 7.08 秒，冷启动重排约
  4.26 秒，热状态混合检索约 0.61 秒。这些是本机、小数据集的
  单次观测，不是性能基准。

## 联合链路验证

临时 API 使用 `127.0.0.1:8001`，独立 MCP 使用 `127.0.0.1:8010`。
实际验证：

1. API 使用真实 Qwen，MCP 使用真实 BGE Embedding 与 Reranker。
2. 经带内部 Token 的 HTTP MCP 完成 BGE 冷启动检索，约 9 秒。
3. Qdrant 的 `meal_search_bge_m3` 集合包含 18 条 1024 维向量。
4. 推荐经过三个真实 Qwen Worker，没有模型降级。
5. 生成 7 天晚餐计划，等待人工确认。
6. 重启 API 与 MCP，恢复原待确认操作。
7. 确认后仅新增一份计划；重复确认返回 409，未重复写入。
8. 同一幂等键提交餐食反馈，返回相同反馈记录。
9. 再次重启 API，画像、计划和已完成会话均可恢复。
10. MCP 对未携带内部 Token 的请求返回 401。

仓库内的严格验收脚本另完成一次真实推荐：

- 5 次 `qwen-plus` 调用，覆盖 Intent、Planner、Recommendation。
- 输入 Token 9417，输出 Token 1088。
- 没有 `tool.failed` 或模型降级事件。
- 推荐为鸡胸藜麦能量碗、虾仁西兰花糙米饭、清蒸鲈鱼配时蔬。
- 使用专用测试用户 `910021`；联合恢复测试使用 `424242`。

## 本轮代码与回归

增强 `scripts/verify_showcase.py`：

- 支持 `--base-url`、`--user-id`、`--output`。
- 默认使用专用测试用户，避免修改日常用户 `1` 的画像。
- 必须存在三个 Worker 的目标 Qwen 模型调用及非零 Token 证据。
- 工具失败、模型不符、降级或不完整响应会导致验收失败。

新增 7 项验收脚本测试，整体后端回归为 79 项通过。
Ruff、mypy 及变更文件格式检查均通过。

真实模式服务启动后，可重新执行：

```bash
.venv/bin/python scripts/verify_showcase.py \
  --base-url http://127.0.0.1:8001 \
  --output .runtime/real-showcase-verification.json
```

该命令实际调用模型并修改专用测试用户的画像。

## 观察到的效果问题

以下问题已记录，本轮没有进一步扩展功能或改动 Prompt：

- 简短的计划请求可能要求补充饮食限制；条件齐全的请求能生成计划。
- “鸡胸肉藜麦能量碗”与种子库名称“鸡胸藜麦能量碗”不完全相同，
  现有营养名称匹配未识别该变体；准确名称查询通过。
- 一次推荐将候选自身的甲壳类、鱼类过敏原误称为用户已标记的
  过敏原，而用户画像实际只标记花生。这属于模型解释质量问题，
  应在后续优化中约束个人事实表述。
- Qdrant 客户端 1.18.0 与服务端 1.14.1 发出版本兼容性警告；
  本次建库、写入、检索均成功，版本对齐尚未处理。

## 运行状态与本机证据

验收结束后释放临时真实模式 API/MCP 端口，并停止本次使用的
基础设施容器，数据卷与模型文件保留。上轮本地演示仍使用
`127.0.0.1:5173`，运行模式为 Fake/内存。

本机生成的原始证据位于 Git 忽略目录：

- `.runtime/qwen-checks.json`
- `.runtime/qwen-initial-checks.json`
- `.runtime/bge-models.json`
- `.runtime/bge-direct-checks.json`
- `.runtime/real-stack-checks.json`
- `.runtime/real-recommendation-trace.json`
- `.runtime/real-showcase-verification.json`
- `.runtime/real-stack-validation.log`
