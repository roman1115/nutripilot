import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import {
  Activity,
  Bot,
  BrainCircuit,
  Check,
  CheckCircle2,
  Circle,
  CircleAlert,
  ExternalLink,
  LoaderCircle,
  Plus,
  Quote,
  RotateCcw,
  Send,
  ShieldCheck,
  Sparkles,
  Square,
  UserRound,
  Utensils,
  Wrench,
  X,
} from "lucide-react";
import { EmptyState, ErrorNotice } from "../../components/Common";
import { ApiError, confirmChat, getThread, getTrace, streamChat } from "../../lib/api";
import { asArray, asRecord, firstNumber, firstString, formatDuration } from "../../lib/format";
import type {
  ChatMessage,
  ChatStreamEvent,
  Citation,
  ConnectionState,
  MealRecommendation,
  PendingConfirmation,
  ToolActivity,
  TraceRun,
} from "../../types";

const promptSuggestions = [
  "晚餐想吃清淡一点，预算 40 元以内",
  "帮我制定一个高蛋白的一日饮食计划",
  "鸡胸肉和豆腐的营养差异是什么？",
];

const componentLabels: Record<string, string> = {
  supervisor: "Supervisor 编排",
  intent_agent: "IntentAgent 理解需求",
  intentagent: "IntentAgent 理解需求",
  clarify: "补全必要信息",
  clarify_node: "补全必要信息",
  planner_agent: "PlannerAgent 制定计划",
  planneragent: "PlannerAgent 制定计划",
  recommendation_agent: "RecommendationAgent 生成建议",
  recommendationagent: "RecommendationAgent 生成建议",
  risk_guard: "RiskGuard 安全检查",
  output_guard: "OutputGuard 输出检查",
  tool_executor: "执行工具",
  search_meals: "检索候选餐食",
  get_nutrition: "查询营养数据",
  get_user_profile: "读取用户画像",
  get_weather_context: "读取天气场景",
  create_meal_plan: "写入饮食计划",
  record_feedback: "记录用户反馈",
  query_dietary_guidelines: "查询饮食指南",
};

function makeId(prefix: string): string {
  return `${prefix}-${crypto.randomUUID()}`;
}

function getInitialThreadId(): string {
  const stored = localStorage.getItem("nutripilot.threadId");
  if (stored) return stored;
  const created = crypto.randomUUID();
  localStorage.setItem("nutripilot.threadId", created);
  return created;
}

function normalizeName(value: string): string {
  const key = value.trim().toLowerCase().replace(/[\s-]+/g, "_");
  return componentLabels[key] || value.replace(/_/g, " ");
}

function activityKind(name: string): ToolActivity["kind"] {
  const lower = name.toLowerCase();
  if (lower.includes("guard") || lower.includes("risk")) return "guard";
  if (lower.includes("agent") || lower.includes("planner") || lower.includes("intent")) return "agent";
  if (lower.includes("tool") || lower.includes("search") || lower.includes("get_") || lower.includes("query_")) return "tool";
  return "system";
}

function normalizeRecommendations(value: unknown): MealRecommendation[] {
  const recommendations: MealRecommendation[] = [];
  for (const item of asArray(value)) {
    const source = asRecord(item);
    const nutrition = asRecord(source.nutrition);
    const name = firstString(source.name, source.title, source.meal_name);
    if (!name) continue;
    recommendations.push({
      mealId: (source.meal_id as string | number | undefined) ?? (source.id as string | number | undefined),
      name,
      reason: firstString(source.reason, source.recommendation_reason, source.description),
      calories: firstNumber(source.calories, source.calories_kcal, nutrition.calories, nutrition.calories_kcal),
      protein: firstNumber(source.protein, source.protein_g, nutrition.protein),
      carbs: firstNumber(source.carbs, source.carbohydrates, source.carbs_g, nutrition.carbs),
      fat: firstNumber(source.fat, source.fat_g, nutrition.fat),
      tags: asArray(source.tags).filter((tag): tag is string => typeof tag === "string"),
    });
  }
  return recommendations;
}

function normalizeCitations(value: unknown): Citation[] {
  const citations: Citation[] = [];
  for (const item of asArray(value)) {
    const source = asRecord(item);
    const title = firstString(source.title, source.name, source.source_title, source.source);
    if (!title) continue;
    citations.push({
      id: (source.id as string | number | undefined) ?? (source.source_id as string | number | undefined),
      title,
      source: firstString(source.source, source.source_type, source.publisher),
      excerpt: firstString(source.excerpt, source.content, source.text),
      score: firstNumber(source.score, source.relevance_score),
      url: firstString(source.url, source.uri),
    });
  }
  return citations;
}

function normalizeStringList(value: unknown): string[] {
  return asArray(value).filter((item): item is string => typeof item === "string" && Boolean(item.trim()));
}

function activitiesFromTrace(trace: TraceRun): ToolActivity[] {
  return trace.events.slice(-16).map((event) => ({
    id: event.id,
    name: event.component,
    label: normalizeName(event.component),
    status: event.status.toLowerCase().includes("fail") || event.status.toLowerCase().includes("error") ? "error" : "success",
    detail: event.error || event.type.replace(/_/g, " "),
    durationMs: event.durationMs,
    kind: activityKind(event.component),
  }));
}

function eventPayload(event: ChatStreamEvent): Record<string, unknown> {
  const outer = event.data;
  const nested = asRecord(outer.data ?? outer.payload);
  return Object.keys(nested).length ? { ...outer, ...nested } : outer;
}

function extractResponseContent(source: Record<string, unknown>): string | undefined {
  const finalResponse = asRecord(source.final_response);
  const response = asRecord(source.response);
  return firstString(
    source.delta,
    source.token,
    source.content,
    source.answer,
    typeof source.message === "string" ? source.message : undefined,
    finalResponse.content,
    finalResponse.answer,
    finalResponse.message,
    response.content,
    response.answer,
    response.message,
  );
}

function activityIcon(activity: ToolActivity) {
  if (activity.status === "running") return <LoaderCircle className="spin" size={16} />;
  if (activity.status === "success") return <CheckCircle2 size={16} />;
  if (activity.status === "error") return <CircleAlert size={16} />;
  return <Circle size={16} />;
}

interface ChatViewProps {
  connection: ConnectionState;
  onTraceDiscovered: (traceId: string) => void;
  onOpenTrace: (traceId: string) => void;
}

export function ChatView({ connection, onTraceDiscovered, onOpenTrace }: ChatViewProps) {
  const [threadId, setThreadId] = useState(getInitialThreadId);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [activities, setActivities] = useState<ToolActivity[]>([]);
  const [draft, setDraft] = useState("");
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingConfirmation, setPendingConfirmation] = useState<PendingConfirmation | null>(null);
  const [confirmationBusy, setConfirmationBusy] = useState(false);
  const [activePhase, setActivePhase] = useState("等待请求");
  const abortRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    if (connection !== "online" || messages.length || isSending) return;
    let cancelled = false;
    void getThread(threadId)
      .then(async (result) => {
        if (cancelled) return;
        const finalResponse = asRecord(result.final_response);
        const pendingAction = asRecord(result.pending_action);
        const traceId = firstString(result.trace_id);
        const content = extractResponseContent(result);
        const restored: ChatMessage[] = [];
        if (content || result.requires_confirmation === true) {
          restored.push({
            id: makeId("restored"),
            role: "assistant",
            content:
              content ||
              "已恢复上次会话中等待确认的操作，请在执行面板中核对后继续。",
            createdAt: new Date().toISOString(),
            recommendations: normalizeRecommendations(finalResponse.recommendations),
            citations: normalizeCitations(finalResponse.citations),
            warnings: normalizeStringList(finalResponse.warnings),
            suggestedActions:
              result.requires_confirmation === true
                ? []
                : normalizeStringList(finalResponse.suggested_actions),
            traceId,
          });
        }
        if (restored.length) setMessages(restored);
        if (result.requires_confirmation === true && Object.keys(pendingAction).length) {
          setPendingConfirmation({
            id: firstString(pendingAction.id),
            title: firstString(pendingAction.title) || "确认数据写入",
            description:
              firstString(pendingAction.description) ||
              "Agent 准备执行一项会修改数据的操作，请核对后确认。",
            operation: firstString(pendingAction.operation),
            payload: asRecord(pendingAction.payload),
            idempotencyKey:
              firstString(pendingAction.idempotency_key, pendingAction.id) ||
              crypto.randomUUID(),
          });
          setActivePhase("已恢复 · 等待人工确认");
        } else if (restored.length) {
          setActivePhase("已恢复上次会话");
        }
        if (traceId) {
          onTraceDiscovered(traceId);
          try {
            const trace = await getTrace(traceId);
            if (!cancelled) setActivities(activitiesFromTrace(trace));
          } catch {
            // Restored business state remains usable when Trace storage is unavailable.
          }
        }
      })
      .catch((caught) => {
        if (!(caught instanceof ApiError && caught.status === 404) && !cancelled) {
          setError(caught instanceof Error ? caught.message : "无法恢复上次会话");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [connection, isSending, messages.length, onTraceDiscovered, threadId]);

  const updateAssistant = (
    assistantId: string,
    update: (message: ChatMessage) => ChatMessage,
  ) => {
    setMessages((current) => current.map((message) => (message.id === assistantId ? update(message) : message)));
  };

  const upsertActivity = (next: ToolActivity) => {
    setActivities((current) => {
      const existingIndex = current.findIndex(
        (item) => item.id === next.id || (item.name === next.name && item.status === "running"),
      );
      if (existingIndex < 0) return [...current, next].slice(-16);
      const copy = [...current];
      copy[existingIndex] = { ...copy[existingIndex], ...next };
      return copy;
    });
  };

  const ingestEvent = (event: ChatStreamEvent, assistantId: string) => {
    const source = eventPayload(event);
    const type = event.type.toLowerCase().replace(/[\s-]+/g, "_");
    const metadata = asRecord(source.metadata ?? source.meta);
    const traceId = firstString(source.trace_id, metadata.trace_id);
    if (traceId) {
      onTraceDiscovered(traceId);
      updateAssistant(assistantId, (message) => ({ ...message, traceId }));
    }
    const returnedThreadId = firstString(source.thread_id, metadata.thread_id);
    if (returnedThreadId && returnedThreadId !== threadId) {
      setThreadId(returnedThreadId);
      localStorage.setItem("nutripilot.threadId", returnedThreadId);
    }

    const component = firstString(
      source.tool_name,
      source.agent,
      source.node,
      source.component,
      source.phase,
      source.name,
    );
    const isStart = type.includes("start") || type.includes("running") || type === "progress";
    const isEnd = type.includes("end") || type.includes("result") || type.includes("complete") || type.includes("finish");
    const isFailure = type.includes("error") || type.includes("fail") || source.status === "error" || source.status === "failed";
    const isActivity =
      Boolean(component) &&
      (type.includes("tool") || type.includes("agent") || type.includes("node") || type.includes("guard") || type === "progress");

    if (type === "status" && component) {
      setActivePhase(normalizeName(component));
      upsertActivity({
        id: `status-${component}`,
        name: component,
        label: normalizeName(component),
        status: "running",
        kind: "system",
      });
    }

    if (isActivity && component) {
      const name = component;
      const status: ToolActivity["status"] = isFailure ? "error" : isEnd ? "success" : isStart ? "running" : "waiting";
      const label = normalizeName(name);
      setActivePhase(status === "running" ? label : status === "error" ? `${label}异常` : label);
      upsertActivity({
        id: firstString(source.event_id, source.call_id, source.tool_call_id) || `${name}-${status}`,
        name,
        label,
        status,
        detail: firstString(source.detail, source.message, source.error_message),
        durationMs: firstNumber(source.duration_ms, source.latency_ms),
        kind: activityKind(name),
      });
    }

    const content = extractResponseContent(source);
    const isDelta = type.includes("delta") || type.includes("token");
    const isFinal = ["final", "response", "completed", "complete", "done", "final_response"].includes(type) || Boolean(source.final_response);
    if (content && (isDelta || isFinal || type === "message")) {
      updateAssistant(assistantId, (message) => ({
        ...message,
        content: isDelta ? `${message.content}${content}` : content,
        isStreaming: !isFinal,
      }));
    }

    const finalResponse = asRecord(source.final_response);
    const recommendations = normalizeRecommendations(
      source.recommendations ?? source.meals ?? finalResponse.recommendations ?? finalResponse.meals,
    );
    const citations = normalizeCitations(
      source.citations ?? source.sources ?? finalResponse.citations ?? finalResponse.sources,
    );
    const warnings = normalizeStringList(source.warnings ?? finalResponse.warnings);
    const suggestedActions = source.requires_confirmation === true
      ? []
      : normalizeStringList(source.suggested_actions ?? finalResponse.suggested_actions);
    if (recommendations.length || citations.length || warnings.length || suggestedActions.length) {
      updateAssistant(assistantId, (message) => ({
        ...message,
        recommendations: recommendations.length ? recommendations : message.recommendations,
        citations: citations.length ? citations : message.citations,
        warnings: warnings.length ? warnings : message.warnings,
        suggestedActions: suggestedActions.length ? suggestedActions : message.suggestedActions,
      }));
    }

    if (type.includes("confirmation") || type === "interrupt" || source.requires_confirmation === true) {
      const confirmation = asRecord(source.confirmation ?? source.pending_action ?? source.action);
      setPendingConfirmation({
        id: firstString(source.confirmation_id, confirmation.id),
        title: firstString(confirmation.title, source.title) || "确认写入操作",
        description:
          firstString(confirmation.description, source.description, source.message) ||
          "Agent 准备执行一项会修改数据的操作，请核对后确认。",
        operation: firstString(confirmation.operation, source.operation, source.tool_name),
        payload: asRecord(confirmation.payload ?? source.arguments ?? source.payload),
        idempotencyKey: firstString(source.idempotency_key, confirmation.idempotency_key) || crypto.randomUUID(),
      });
      setActivePhase("等待人工确认");
    }

    if (isFailure && !isActivity) {
      setError(firstString(source.message, source.detail, source.error, source.error_message) || "Agent 执行失败");
    }
    if (isFinal || type === "done") setActivePhase("本轮已完成");
  };

  const sendMessage = async (content: string) => {
    const trimmed = content.trim();
    if (!trimmed || isSending) return;
    const userMessage: ChatMessage = {
      id: makeId("user"),
      role: "user",
      content: trimmed,
      createdAt: new Date().toISOString(),
    };
    const assistantId = makeId("assistant");
    const assistantMessage: ChatMessage = {
      id: assistantId,
      role: "assistant",
      content: "",
      createdAt: new Date().toISOString(),
      isStreaming: true,
    };
    setMessages((current) => [...current, userMessage, assistantMessage]);
    setDraft("");
    setActivities([]);
    setError(null);
    setPendingConfirmation(null);
    setIsSending(true);
    setActivePhase("Supervisor 接收请求");
    abortRef.current = new AbortController();
    let confirmationRequested = false;
    let completedTraceId: string | undefined;

    try {
      await streamChat(
        { threadId, message: trimmed },
        (event) => {
          const type = event.type.toLowerCase();
          const payload = eventPayload(event);
          const metadata = asRecord(payload.metadata ?? payload.meta);
          completedTraceId = firstString(payload.trace_id, metadata.trace_id) || completedTraceId;
          if (type.includes("confirmation") || type === "interrupt" || payload.requires_confirmation === true) {
            confirmationRequested = true;
          }
          ingestEvent(event, assistantId);
        },
        abortRef.current.signal,
      );
      updateAssistant(assistantId, (message) => ({
        ...message,
        content:
          message.content ||
          (confirmationRequested
            ? "我已经准备好待执行的操作，请在执行面板中核对并确认。"
            : "请求已经结束，但服务没有返回可展示的文本。"),
        isStreaming: false,
      }));
      if (completedTraceId) {
        try {
          const trace = await getTrace(completedTraceId);
          const tracedActivities = activitiesFromTrace(trace);
          if (tracedActivities.length) setActivities(tracedActivities);
        } catch {
          // The conversation result remains usable when observability storage is unavailable.
        }
      }
      setActivePhase((phase) => (confirmationRequested || phase === "等待人工确认" ? "等待人工确认" : "本轮已完成"));
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === "AbortError") {
        updateAssistant(assistantId, (message) => ({
          ...message,
          content: message.content || "已停止本轮请求。",
          isStreaming: false,
        }));
        setActivePhase("已停止");
      } else {
        const message = caught instanceof ApiError || caught instanceof Error ? caught.message : "对话请求失败";
        setError(message);
        updateAssistant(assistantId, (current) => ({
          ...current,
          content: current.content || "本轮未能获得结果，请检查服务后重试。",
          isStreaming: false,
        }));
        setActivePhase("请求失败");
      }
    } finally {
      setIsSending(false);
      abortRef.current = null;
    }
  };

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    void sendMessage(draft);
  };

  const handleComposerKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void sendMessage(draft);
    }
  };

  const handleConfirmation = async (approved: boolean) => {
    if (!pendingConfirmation) return;
    setConfirmationBusy(true);
    setError(null);
    try {
      const result = await confirmChat(
        threadId,
        approved,
        pendingConfirmation.id,
        pendingConfirmation.idempotencyKey,
      );
      setMessages((current) => [
        ...current,
        {
          id: makeId("system"),
          role: "system",
          content: approved ? "已确认操作，Agent 正在继续执行。" : "已拒绝本次写入操作。",
          createdAt: new Date().toISOString(),
        },
      ]);
      const resultContent = extractResponseContent(result);
      const finalResponse = asRecord(result.final_response);
      const recommendations = normalizeRecommendations(finalResponse.recommendations);
      const citations = normalizeCitations(finalResponse.citations);
      const warnings = normalizeStringList(finalResponse.warnings);
      const suggestedActions = normalizeStringList(finalResponse.suggested_actions);
      const resultTraceId = firstString(result.trace_id);
      if (resultTraceId) onTraceDiscovered(resultTraceId);
      if (resultContent) {
        setMessages((current) => [
          ...current,
          {
            id: makeId("assistant"),
            role: "assistant",
            content: resultContent,
            createdAt: new Date().toISOString(),
            recommendations,
            citations,
            warnings,
            suggestedActions,
            traceId: resultTraceId,
          },
        ]);
      }
      if (resultTraceId) {
        try {
          const trace = await getTrace(resultTraceId);
          const tracedActivities = activitiesFromTrace(trace);
          if (tracedActivities.length) setActivities(tracedActivities);
        } catch {
          // Confirmation succeeds independently from Trace retrieval.
        }
      }
      setPendingConfirmation(null);
      setActivePhase(approved ? "操作已确认" : "操作已取消");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "确认请求失败");
    } finally {
      setConfirmationBusy(false);
    }
  };

  const startNewThread = () => {
    abortRef.current?.abort();
    const next = crypto.randomUUID();
    localStorage.setItem("nutripilot.threadId", next);
    setThreadId(next);
    setMessages([]);
    setActivities([]);
    setPendingConfirmation(null);
    setError(null);
    setActivePhase("等待请求");
  };

  const retryLast = () => {
    const lastUserMessage = [...messages].reverse().find((message) => message.role === "user");
    if (lastUserMessage) void sendMessage(lastUserMessage.content);
  };

  return (
    <div className="chat-layout">
      <section className="chat-surface" aria-label="Agent 对话">
        <div className="chat-toolbar">
          <div>
            <span className="chat-thread-label">当前会话</span>
            <strong title={threadId}>{threadId.slice(0, 8)}</strong>
          </div>
          <button className="button button-secondary button-compact" type="button" onClick={startNewThread}>
            <Plus size={16} />
            新对话
          </button>
        </div>

        <div className="messages" aria-live="polite">
          {messages.length === 0 ? (
            <EmptyState
              icon={<Sparkles size={24} />}
              title="从一个真实饮食目标开始"
              description="Agent 会补全必要信息、调用餐食与营养工具，并在写入计划前请求确认。"
              action={
                <div className="prompt-suggestions">
                  {promptSuggestions.map((suggestion) => (
                    <button key={suggestion} type="button" onClick={() => setDraft(suggestion)}>
                      {suggestion}
                    </button>
                  ))}
                </div>
              }
            />
          ) : (
            messages.map((message) => (
              <article className={`message message-${message.role}`} key={message.id}>
                <div className="message-avatar" aria-hidden="true">
                  {message.role === "user" ? <UserRound size={17} /> : message.role === "assistant" ? <Bot size={18} /> : <Activity size={17} />}
                </div>
                <div className="message-body">
                  <div className="message-meta">
                    <strong>{message.role === "user" ? "你" : message.role === "assistant" ? "NutriPilot" : "系统"}</strong>
                    {message.isStreaming ? <span><LoaderCircle className="spin" size={13} />生成中</span> : null}
                  </div>
                  <div className="message-content">{message.content || <span className="stream-placeholder">正在分析请求…</span>}</div>

                  {message.recommendations?.length ? (
                    <div className="recommendation-list">
                      {message.recommendations.map((meal, index) => (
                        <div className="meal-result" key={String(meal.mealId ?? `${meal.name}-${index}`)}>
                          <div className="meal-result-heading">
                            <div className="meal-result-icon"><Utensils size={17} /></div>
                            <div>
                              <span>推荐 {index + 1}</span>
                              <strong>{meal.name}</strong>
                            </div>
                            {meal.calories !== undefined ? <b>{Math.round(meal.calories)} kcal</b> : null}
                          </div>
                          {meal.reason ? <p>{meal.reason}</p> : null}
                          {(meal.protein !== undefined || meal.carbs !== undefined || meal.fat !== undefined) ? (
                            <div className="nutrition-row">
                              {meal.protein !== undefined ? <span>蛋白质 {meal.protein}g</span> : null}
                              {meal.carbs !== undefined ? <span>碳水 {meal.carbs}g</span> : null}
                              {meal.fat !== undefined ? <span>脂肪 {meal.fat}g</span> : null}
                            </div>
                          ) : null}
                          {meal.tags?.length ? <div className="tag-row">{meal.tags.map((tag) => <span key={tag}>{tag}</span>)}</div> : null}
                        </div>
                      ))}
                    </div>
                  ) : null}

                  {message.citations?.length ? (
                    <div className="citation-list">
                      <div className="section-inline-label"><Quote size={14} />回答依据</div>
                      {message.citations.map((citation, index) => (
                        <div className="citation-item" key={String(citation.id ?? `${citation.title}-${index}`)}>
                          <div>
                            <strong>{citation.title}</strong>
                            {citation.source ? <span>{citation.source}</span> : null}
                            {citation.excerpt ? <p>{citation.excerpt}</p> : null}
                          </div>
                          <div className="citation-aside">
                            {citation.score !== undefined ? (
                              <span title="融合召回与重排序后的排序分，不代表概率">
                                检索分 {citation.score.toFixed(2)}
                              </span>
                            ) : null}
                            {citation.url ? (
                              <a href={citation.url} target="_blank" rel="noreferrer" aria-label={`打开 ${citation.title}`}>
                                <ExternalLink size={15} />
                              </a>
                            ) : null}
                          </div>
                        </div>
                      ))}
                    </div>
                  ) : null}

                  {message.warnings?.length ? (
                    <div className="response-warnings">
                      {message.warnings.map((warning) => (
                        <p key={warning}><ShieldCheck size={15} />{warning}</p>
                      ))}
                    </div>
                  ) : null}

                  {message.suggestedActions?.length ? (
                    <div className="suggested-actions" aria-label="建议的后续操作">
                      {message.suggestedActions.map((action) => (
                        <button key={action} type="button" onClick={() => setDraft(action)}>
                          <Sparkles size={13} />{action}
                        </button>
                      ))}
                    </div>
                  ) : null}

                  {message.traceId ? (
                    <button className="trace-link" type="button" onClick={() => onOpenTrace(message.traceId!)}>
                      <Activity size={14} />查看 Trace {message.traceId.slice(0, 8)}
                    </button>
                  ) : null}
                </div>
              </article>
            ))
          )}
          <div ref={messagesEndRef} />
        </div>

        <div className="chat-bottom">
          {error ? (
            <ErrorNotice
              message={error}
              action={messages.some((message) => message.role === "user") ? (
                <button className="button button-ghost button-compact" type="button" onClick={retryLast} disabled={isSending}>
                  <RotateCcw size={15} />重试
                </button>
              ) : undefined}
            />
          ) : null}
          {connection === "offline" ? (
            <div className="offline-strip">Agent API 当前不可用，连接恢复后才能发送请求。</div>
          ) : null}
          <form className="composer" onSubmit={handleSubmit}>
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={handleComposerKey}
              rows={1}
              placeholder="描述你的餐次、口味、目标或需要调整的计划…"
              aria-label="发送给 NutriPilot 的消息"
              disabled={connection === "offline"}
            />
            {isSending ? (
              <button className="composer-send is-stop" type="button" onClick={() => abortRef.current?.abort()} aria-label="停止生成">
                <Square size={16} fill="currentColor" />
              </button>
            ) : (
              <button className="composer-send" type="submit" disabled={!draft.trim() || connection !== "online"} aria-label="发送消息">
                <Send size={17} />
              </button>
            )}
          </form>
          <p className="composer-note">营养建议仅用于日常决策，不替代医疗诊断或治疗。</p>
        </div>
      </section>

      <aside className="activity-panel" aria-label="Agent 执行状态">
        <div className="activity-panel-heading">
          <div>
            <span className="eyebrow">Live run</span>
            <h2>执行过程</h2>
          </div>
          <span className={`run-state${isSending ? " is-running" : ""}`}>
            {isSending ? <LoaderCircle className="spin" size={13} /> : <Check size={13} />}
            {activePhase}
          </span>
        </div>

        {pendingConfirmation ? (
          <div className="confirmation-panel">
            <div className="confirmation-title">
              <ShieldCheck size={18} />
              <div>
                <span>需要你的确认</span>
                <strong>{pendingConfirmation.title}</strong>
              </div>
            </div>
            <p>{pendingConfirmation.description}</p>
            {pendingConfirmation.operation ? <code>{pendingConfirmation.operation}</code> : null}
            {pendingConfirmation.payload && Object.keys(pendingConfirmation.payload).length ? (
              <dl className="confirmation-payload">
                {Object.entries(pendingConfirmation.payload).slice(0, 6).map(([key, value]) => (
                  <div key={key}>
                    <dt>{key}</dt>
                    <dd>{typeof value === "string" ? value : JSON.stringify(value)}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
            <div className="confirmation-actions">
              <button className="button button-primary" type="button" onClick={() => void handleConfirmation(true)} disabled={confirmationBusy}>
                {confirmationBusy ? <LoaderCircle className="spin" size={15} /> : <Check size={15} />}
                确认执行
              </button>
              <button className="button button-secondary" type="button" onClick={() => void handleConfirmation(false)} disabled={confirmationBusy}>
                <X size={15} />拒绝
              </button>
            </div>
          </div>
        ) : null}

        {activities.length ? (
          <ol className="activity-list">
            {activities.map((activity, index) => (
              <li className={`activity-item activity-${activity.status}`} key={`${activity.id}-${index}`}>
                <div className="activity-line" aria-hidden="true" />
                <div className="activity-status-icon">{activityIcon(activity)}</div>
                <div className="activity-copy">
                  <span>{activity.kind === "agent" ? "Agent" : activity.kind === "tool" ? "Tool" : activity.kind === "guard" ? "Guard" : "Flow"}</span>
                  <strong>{activity.label}</strong>
                  {activity.detail ? <p>{activity.detail}</p> : null}
                </div>
                {activity.durationMs !== undefined ? <time>{formatDuration(activity.durationMs)}</time> : null}
              </li>
            ))}
          </ol>
        ) : (
          <div className="activity-empty">
            <BrainCircuit size={24} />
            <strong>尚未开始执行</strong>
            <p>发送请求后，这里会展示 Agent、工具和安全检查的真实进度。</p>
          </div>
        )}

        <div className="activity-legend">
          <span><BrainCircuit size={14} />Agent 决策</span>
          <span><Wrench size={14} />工具调用</span>
          <span><ShieldCheck size={14} />安全校验</span>
        </div>
      </aside>
    </div>
  );
}
