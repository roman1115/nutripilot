# CookHero流式输出源码核对

日期：2026-11-16。只读核对，不代表运行效果或性能验收。
通过GitHub API查询时，`Decade-qiu/CookHero`的main为
`ca2f79a3200ee75cd6ff560f354b29d025d87aa0`，提交时间2026-08-21T02:10:17Z。
以下结论绑定该快照，不推广到其它版本。

## 核对结果

1. 普通对话是真实模型增量输出。`LLMOrchestrator.stream`调用模型`astream`，
   将非空content逐块yield；conversation service累积完整回答，同时把每块作为
   `text`事件发送，生成完保存回答，再发`done`。图片直接回答存在整段发送的分支，
   因此不能宣称所有场景都逐块生成。
2. Agent流式入口调用`run_streaming`；带工具的模型流中，文本块即时发出，
   工具调用片段先聚合，再执行工具、发`tool_call`/`tool_result`，
   把结果加回消息后继续模型循环。文本可能在工具调用前出现，不限于最终回答。
3. 后端通过FastAPI `StreamingResponse`发送SSE，设置
   `text/event-stream`、`Cache-Control: no-cache`及`X-Accel-Buffering: no`。
   前端用fetch响应流、TextDecoder和缓冲区解析SSE，不是收到全文后模拟打字。
4. 普通对话前端先创建空助手消息，收到`text`就追加内容；vision、intent、
   thinking、sources、done分别处理。正文使用Markdown渲染，记录等待与生成耗时。
   thinking是实现中的事件名，不能据此认定为模型隐藏推理。
5. 切换会话时前端保留进行中的流，按会话缓存；浏览器断开时后端
   `asyncio.create_task`和内存Queue让处理继续，最终保存回答。
   前端stopGeneration调用AbortController中止读取；所查入口明确让后台任务继续，
   不能把停止显示等同于取消模型、停止计费或进程崩溃后的持久恢复。

## 对NutriPilot的建议，尚未实施

- 当前模型网关`openai_compatible.py`使用完整completion并解析结构化结果；
  `chat_worker.py`发送节点进度；ChatView已有delta/token追加和最终结果替换处理。
  这还不是模型正文实时流式链路，已有前端处理也不等于完整重连去重已验收。
- 参考其真实模型流、正文与工具事件分离、Markdown增量展示；前期意图和工具规划
  继续使用结构化结果，面向用户的正文增加流式生成路径，避免展示未完成JSON。
- 继续复用现有Worker、持久事件序号及任务取消机制；补正文增量、终态完整快照、
  重连去重、部分输出失败和恢复后内容替换语义，不改用仅内存后台任务。
- 正文发布前校验与现有推荐卡片一致性必须一起设计。已发文本无法保证撤回，
  不应把“全部生成后再打字”当作真正降低首段等待；不取消本人隔离、
  数据预览确认、幂等或营养来源要求。
- 工具执行展示简短状态，原始参数/结果不默认公开；这是本项目建议，
  不是CookHero源码已经做了同样隐私处理的结论。

## 源码定位

下列路径均相对于该固定快照；行号来自本次实际读取：

| 文件 | 关键位置 |
| --- | --- |
| `app/conversation/llm_orchestrator.py` | 23–34：模型astream |
| `app/services/conversation_service.py` | 185–197：text增量、保存、done；443–447：图片直接回答 |
| `app/agent/agents/base.py` | 234–385：流式工具循环 |
| `app/agent/service.py` | 234–341：模型块及工具事件转换 |
| `app/api/v1/endpoints/conversation.py` | 140–193：后台队列与SSE |
| `app/api/v1/endpoints/agent.py` | 378–435：后台队列与SSE |
| `frontend/src/services/api/conversation.ts` | 17–90：fetch流与SSE解析 |
| `frontend/src/hooks/useConversation.ts` | 291–299、436–458、612–636、740起：占位、追加、切换、停止 |
| `frontend/src/components/chat/MarkdownRenderer.tsx` | 128–138：Markdown渲染 |

源码仓库：`https://github.com/Decade-qiu/CookHero`。
固定源码地址形式：
`https://github.com/Decade-qiu/CookHero/blob/ca2f79a3200ee75cd6ff560f354b29d025d87aa0/<文件路径>`。

本轮未修改业务代码、未运行CookHero、未调用真实模型，也未执行工程测试。
