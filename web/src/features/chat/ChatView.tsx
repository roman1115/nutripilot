import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from "react";
import {
  Activity,
  Salad,
  ArrowUpRight,
  ChevronDown,
  ChevronRight,
  Search,
  BrainCircuit,
  Check,
  CheckCircle2,
  Circle,
  CircleAlert,
  ExternalLink,
  History,
  LoaderCircle,
  Plus,
  Quote,
  RotateCcw,
  Send,
  ShieldCheck,
  Square,
  UserRound,
  Utensils,
  Wrench,
  X,
} from "lucide-react";
import { ErrorNotice } from "../../components/Common";
import {
  ApiError, cancelChat, confirmChat, getConversationMessages, getConversations,
  getThread, getTrace, streamChat,
  normalizePlanItem,
} from "../../lib/api";
import { asArray, asRecord, firstNumber, firstString, formatDuration } from "../../lib/format";
import type {
  ChatMessage,
  ChatStreamEvent,
  Citation,
  ConnectionState,
  ConversationSummary,
  MealRecommendation,
  PendingConfirmation,
  ToolActivity,
  TraceRun,
} from "../../types";

const promptSuggestions = [
  { title: "今天吃点清爽的", detail: "清淡晚餐", prompt: "晚餐想吃清淡一点，预算 40 元以内",
    image: "/images/fresh-bowl.jpg", alt: "新鲜蔬菜与谷物组成的餐碗" },
  { title: "把一天安排妥当", detail: "一日餐单", prompt: "帮我制定一个高蛋白的一日饮食计划",
    image: "/images/daily-table.jpg", alt: "桌面上摆放的谷物、水果和蔬菜" },
  { title: "午餐也好好吃", detail: "高蛋白午餐", prompt: "推荐一份高蛋白的午餐",
    image: "/images/simple-meal.jpg", alt: "一份搭配蔬菜的日常餐碗" },
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
      protein: firstNumber(source.protein, source.protein_g, nutrition.protein, nutrition.protein_g),
      carbs: firstNumber(source.carbs, source.carbohydrates, source.carbs_g, nutrition.carbs, nutrition.carbohydrates_g),
      fat: firstNumber(source.fat, source.fat_g, nutrition.fat, nutrition.fat_g),
      nutrientRanges: normalizeNutrientRanges(source.nutrient_ranges ?? nutrition.nutrient_ranges),
      tags: asArray(source.tags).filter((tag): tag is string => typeof tag === "string"),
    });
  }
  return recommendations;
}

function normalizeNutrientRanges(value: unknown): MealRecommendation["nutrientRanges"] {
  const result: NonNullable<MealRecommendation["nutrientRanges"]> = {};
  for (const [key, raw] of Object.entries(asRecord(value))) {
    const bounds = asRecord(raw);
    const minimum = firstNumber(bounds.minimum);
    const maximum = firstNumber(bounds.maximum);
    if (minimum !== undefined && maximum !== undefined && minimum >= 0 && maximum >= minimum) {
      result[key] = { minimum, maximum };
    }
  }
  return result;
}

function nutrientLabel(meal: MealRecommendation, field: string, fallback: number): string {
  const range = meal.nutrientRanges?.[field];
  const format = (value: number) => Number(value.toFixed(1)).toString();
  if (!range) return format(fallback);
  return range.minimum === range.maximum
    ? format(range.minimum)
    : `${format(range.minimum)}～${format(range.maximum)}`;
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
  const [historyOpen, setHistoryOpen] = useState(() => window.matchMedia("(min-width: 1100px)").matches);
  const [activityOpen, setActivityOpen] = useState(false);
  const [historyQuery, setHistoryQuery] = useState("");
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [historyMore, setHistoryMore] = useState(false);
  const [restoring, setRestoring] = useState(false);
  const [planWarnings, setPlanWarnings] = useState<string[]>([]);
  const restoredThreadRef = useRef<string | null>(null);
  const [activePhase, setActivePhase] = useState("等待请求");
  const abortRef = useRef<AbortController | null>(null);
  const messagesEndRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 1100px)");
    const updateHistory = (event: MediaQueryListEvent) => setHistoryOpen(event.matches);
    desktop.addEventListener("change", updateHistory);
    return () => desktop.removeEventListener("change", updateHistory);
  }, []);

  useEffect(() => {
    if (pendingConfirmation) setActivityOpen(true);
  }, [pendingConfirmation]);

  useEffect(() => {
    if (!activityOpen) return;
    const close = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") setActivityOpen(false);
    };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, [activityOpen]);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  useEffect(() => () => {
    if (abortRef.current) {
      void cancelChat(threadId).catch(() => undefined);
      abortRef.current.abort();
    }
  }, [threadId]);

  useEffect(() => {
    if (connection !== "online" || isSending || restoredThreadRef.current === threadId) return;
    let cancelled = false;
    let finished = false;
    restoredThreadRef.current = threadId;
    setRestoring(true);
    void Promise.all([getThread(threadId), getConversationMessages(threadId)])
      .then(async ([result, history]) => {
        if (cancelled) return;
        const finalResponse = asRecord(result.final_response);
        const pendingAction = asRecord(result.pending_action);
        const traceId = firstString(result.trace_id);
        const content = extractResponseContent(result);
        const restored: ChatMessage[] = [];
        for (const item of history) {
          const response = asRecord(item.response);
          const role = item.role === "user" || item.role === "system" ? item.role : "assistant";
          restored.push({
            id: String(item.id),
            role,
            content: String(item.content),
            createdAt: String(item.created_at),
            traceId: firstString(item.trace_id),
            recommendations: normalizeRecommendations(response.recommendations),
            citations: normalizeCitations(response.citations),
            warnings: normalizeStringList(response.warnings),
            suggestedActions: normalizeStringList(response.suggested_actions),
          });
        }
        if (!history.length && (content || result.requires_confirmation === true)) {
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
        setPlanWarnings(normalizeStringList(asRecord(result.plan_quality).warnings));
        if (result.requires_confirmation === true && Object.keys(pendingAction).length) {
          setPendingConfirmation({
            id: firstString(pendingAction.id),
            title: firstString(pendingAction.title) || "确认数据写入",
            description:
              firstString(pendingAction.description) ||
              "Agent 准备执行一项会修改数据的操作，请核对后确认。",
            operation: firstString(pendingAction.operation),
            payload: asRecord(pendingAction.payload),
            mealPreview: asArray(pendingAction.meal_preview).map(normalizePlanItem),
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
      })
      .finally(() => {
        finished = true;
        if (!cancelled) setRestoring(false);
      });
    return () => {
      cancelled = true;
      if (!finished) restoredThreadRef.current = null;
    };
  }, [connection, isSending, onTraceDiscovered, threadId]);

  useEffect(() => {
    if (!historyOpen || connection !== "online") return;
    let cancelled = false;
    setHistoryLoading(true);
    setHistoryError(null);
    const timer = window.setTimeout(() => {
      void getConversations(historyQuery.trim()).then((items) => {
        if (cancelled) return;
        setConversations(items);
        setHistoryOffset(items.length);
        setHistoryMore(items.length === 30);
      }).catch((caught) => {
        if (!cancelled) setHistoryError(caught instanceof Error ? caught.message : "历史会话读取失败");
      }).finally(() => { if (!cancelled) setHistoryLoading(false); });
    }, 250);
    return () => { cancelled = true; window.clearTimeout(timer); };
  }, [connection, historyOpen, historyQuery, isSending]);

  const loadMoreHistory = async () => {
    setHistoryLoading(true);
    try {
      const items = await getConversations(historyQuery.trim(), historyOffset);
      setConversations((current) => [...current, ...items]);
      setHistoryOffset((current) => current + items.length);
      setHistoryMore(items.length === 30);
    } catch (caught) {
      setHistoryError(caught instanceof Error ? caught.message : "历史会话读取失败");
    } finally {
      setHistoryLoading(false);
    }
  };

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
    if (source.plan_quality) {
      setPlanWarnings(normalizeStringList(asRecord(source.plan_quality).warnings));
    }
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
        mealPreview: asArray(confirmation.meal_preview).map(normalizePlanItem),
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
    if (!trimmed || isSending || restoring || confirmationBusy) return;
    restoredThreadRef.current = threadId;
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
    setPlanWarnings([]);
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
      setActivityOpen(false);
      setActivePhase(approved ? "操作已确认" : "操作已取消");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "确认请求失败");
    } finally {
      setConfirmationBusy(false);
    }
  };

  const stopGeneration = async () => {
    try {
      await cancelChat(threadId);
      abortRef.current?.abort();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "无法停止服务端请求，请重试");
    }
  };

  const startNewThread = () => {
    if (isSending || confirmationBusy || restoring) return;
    const next = crypto.randomUUID();
    localStorage.setItem("nutripilot.threadId", next);
    setThreadId(next);
    setMessages([]);
    setActivities([]);
    setPendingConfirmation(null);
    setError(null);
    setActivePhase("等待请求");
    if (!window.matchMedia("(min-width: 1100px)").matches) setHistoryOpen(false);
    setPlanWarnings([]);
  };

  const selectThread = (id: string) => {
    if (isSending || confirmationBusy || restoring) return;
    if (id === threadId) {
      if (!window.matchMedia("(min-width: 1100px)").matches) setHistoryOpen(false);
      return;
    }
    localStorage.setItem("nutripilot.threadId", id);
    setThreadId(id);
    setMessages([]);
    setActivities([]);
    setPendingConfirmation(null);
    setPlanWarnings([]);
    setError(null);
    setDraft("");
    if (!window.matchMedia("(min-width: 1100px)").matches) setHistoryOpen(false);
  };

  const retryLast = () => {
    const lastUserMessage = [...messages].reverse().find((message) => message.role === "user");
    if (lastUserMessage) void sendMessage(lastUserMessage.content);
  };

  return (
    <div className={`chat-layout${historyOpen ? " has-history" : ""}`}>
          {historyOpen ? <>
            <button className="history-scrim" type="button" aria-label="收起历史会话" onClick={() => setHistoryOpen(false)} />
            <section className="conversation-history" aria-label="历史会话列表">
              <div className="history-heading">
                <h2><History size={16} />历史会话</h2>
                <button className="icon-button" type="button" title="关闭历史会话" aria-label="关闭历史会话"
                  onClick={() => setHistoryOpen(false)}><X size={18} /></button>
              </div>
              <label className="history-search">
                <Search size={15} aria-hidden="true" />
                <input value={historyQuery} onChange={(event) => setHistoryQuery(event.target.value)}
                  type="search" maxLength={200} aria-label="搜索会话" placeholder="搜索对话…" />
              </label>
              {historyError ? <ErrorNotice message={historyError} /> : null}
              <div className="conversation-items">
              {conversations.map((item) => (
                <button key={item.threadId} type="button" className="conversation-item"
                  aria-current={item.threadId === threadId ? "true" : undefined}
                  disabled={isSending || confirmationBusy || restoring}
                  onClick={() => selectThread(item.threadId)}>
                  <strong>{item.title}</strong>
                  <span>{new Date(item.updatedAt).toLocaleDateString("zh-CN", { month: "long", day: "numeric" })} · {item.messageCount} 条消息</span>
                </button>
              ))}
              {!conversations.length && !historyLoading ? <div className="history-empty">
                <History size={24} strokeWidth={1.5} aria-hidden="true" />
                <p>{historyQuery ? "没有找到相关对话" : "还没有历史会话"}</p>
              </div> : null}
              {historyLoading ? <p role="status">正在读取历史会话…</p> : null}
              {historyMore ? <button type="button" className="button button-secondary"
                onClick={() => void loadMoreHistory()} disabled={historyLoading}>加载更多</button> : null}
              </div>
            </section>
          </> : null}
      <section className="chat-surface" aria-label="饮食对话">
        <div className="chat-toolbar">
          <div className="chat-toolbar-title">
            <button className="icon-button" type="button" title="历史会话" aria-label="历史会话"
              aria-expanded={historyOpen} onClick={() => setHistoryOpen((open) => !open)}>
              <History size={18} />
            </button>
            <strong title={threadId}>{messages.find((message) => message.role === "user")?.content || "新的饮食对话"}</strong>
          </div>
          <div className="chat-toolbar-actions">
            <button className={`icon-button${activityOpen ? " is-selected" : ""}`} type="button"
              title="执行过程" aria-label="执行过程" aria-expanded={activityOpen}
              aria-controls="chat-activity" onClick={() => setActivityOpen((open) => !open)}>
              <Activity size={18} />
              {pendingConfirmation ? <span className="pending-dot" /> : null}
            </button>
            <button className="button button-secondary button-compact" type="button" onClick={startNewThread}
              disabled={isSending || confirmationBusy || restoring}>
              <Plus size={16} />新对话
            </button>
          </div>
        </div>

        <div className="messages" aria-live="polite">
          {restoring ? <p className="history-loading" role="status">正在恢复会话…</p> : null}
          {messages.length === 0 ? (
            <div className="chat-welcome">
              <div className="welcome-mark"><Salad size={31} strokeWidth={1.5} /></div>
              <p className="welcome-eyebrow">好好吃饭的每一天</p>
              <h1>今天，想吃点什么？</h1>
              <div className="prompt-suggestions">
                {promptSuggestions.map((suggestion) => (
                  <button key={suggestion.title} type="button" onClick={() => setDraft(suggestion.prompt)}>
                    <img src={suggestion.image} alt={suggestion.alt} width="720" height="440" />
                    <span className="suggestion-copy"><small>{suggestion.detail}</small>
                      <strong>{suggestion.title}</strong><ArrowUpRight size={17} aria-hidden="true" /></span>
                  </button>
                ))}
              </div>
              <p className="welcome-footnote">餐桌灵感 · 图片为示意，具体餐食以推荐结果为准</p>
            </div>
          ) : (
            messages.map((message) => (
              <article className={`message message-${message.role}`} key={message.id}>
                <div className="message-avatar" aria-hidden="true">
                  {message.role === "user" ? <UserRound size={17} /> : message.role === "assistant" ? <Salad size={18} /> : <Activity size={17} />}
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
                            {meal.calories !== undefined ? <b>{nutrientLabel(meal, "calories_kcal", meal.calories)} kcal</b> : null}
                          </div>
                          {meal.reason ? <p>{meal.reason}</p> : null}
                          {(meal.protein !== undefined || meal.carbs !== undefined || meal.fat !== undefined) ? (
                            <div className="nutrition-row">
                              {meal.protein !== undefined ? <span>蛋白质 {nutrientLabel(meal, "protein_g", meal.protein)}g</span> : null}
                              {meal.carbs !== undefined ? <span>碳水 {nutrientLabel(meal, "carbohydrates_g", meal.carbs)}g</span> : null}
                              {meal.fat !== undefined ? <span>脂肪 {nutrientLabel(meal, "fat_g", meal.fat)}g</span> : null}
                            </div>
                          ) : null}
                          {meal.tags?.length ? <div className="tag-row">{meal.tags.map((tag) => <span key={tag}>{tag}</span>)}</div> : null}
                        </div>
                      ))}
                    </div>
                  ) : null}

                  {message.citations?.length ? (
                    <details className="citation-list">
                      <summary className="section-inline-label"><Quote size={14} />回答依据
                        <span>{message.citations.length} 条来源</span><ChevronDown size={14} /></summary>
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
                    </details>
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
                          <ChevronRight size={13} />{action}
                        </button>
                      ))}
                    </div>
                  ) : null}

                  {message.traceId ? (
                    <button className="trace-link" type="button" onClick={() => onOpenTrace(message.traceId!)}>
                      <Activity size={14} />查看执行记录
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
            <div className="offline-strip">暂时无法连接服务，请稍后重试。</div>
          ) : null}
          <form className="composer" onSubmit={handleSubmit}>
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={handleComposerKey}
              rows={1}
              placeholder="想吃什么，或是有什么饮食打算？"
              aria-label="发送给 NutriPilot 的消息"
              disabled={connection === "offline" || restoring || confirmationBusy}
            />
            {isSending ? (
              <button className="composer-send is-stop" type="button" onClick={() => void stopGeneration()} aria-label="停止生成">
                <Square size={16} fill="currentColor" />
              </button>
            ) : (
              <button className="composer-send" type="submit"
                disabled={!draft.trim() || connection !== "online" || restoring || confirmationBusy} aria-label="发送消息">
                <Send size={17} />
              </button>
            )}
          </form>
          <p className="composer-note">营养建议仅用于日常决策，不替代医疗诊断或治疗。</p>
        </div>
      </section>

      {activityOpen ? <button className="activity-scrim" type="button" aria-label="收起执行过程" onClick={() => setActivityOpen(false)} /> : null}
      <aside id="chat-activity" className="activity-panel" aria-label="Agent 执行状态" hidden={!activityOpen}>
        {planWarnings.length ? <div className="plan-quality-warnings" role="status">
          {planWarnings.map((warning) => <p key={warning}>{warning}</p>)}
        </div> : null}
        <div className="activity-panel-heading">
          <div>
            <h2>执行过程</h2>
          </div>
          <span className={`run-state${isSending ? " is-running" : ""}`}>
            {isSending ? <LoaderCircle className="spin" size={13} /> : <Check size={13} />}
            {activePhase}
          </span>
          <button className="icon-button" type="button" title="关闭执行过程" aria-label="关闭执行过程"
            onClick={() => setActivityOpen(false)}><X size={18} /></button>
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
            {pendingConfirmation.mealPreview?.length ? (
              <div className="confirmation-meals">
                <p>每日热量目标：{String(asRecord(pendingConfirmation.payload?.quality).daily_calorie_target ?? "未设置")}
                  {" kcal（匹配容差 ±10%）"}
                  <br />每日蛋白质最低目标：{String(asRecord(pendingConfirmation.payload?.quality).daily_protein_target_g ?? "未设置")} g
                </p>
                {asArray(asRecord(pendingConfirmation.payload?.quality).daily_totals).map((value) => {
                  const day = asRecord(value);
                  return <p key={String(day.date)}>{String(day.date)}：
                    {" "}{String(day.calories_kcal ?? "未知")} kcal
                    {" · "}{String(day.protein_g ?? "未知")} g 蛋白质</p>;
                })}
                <details className="plan-preview">
                  <summary>餐单明细（{pendingConfirmation.mealPreview.length} 餐）</summary>
                  <ol>{pendingConfirmation.mealPreview.map((meal, index) => (
                    <li key={`${meal.date}-${meal.mealType}-${index}`}>
                      <span>{meal.date} · {({ breakfast: "早餐", lunch: "午餐", dinner: "晚餐", snack: "加餐" } as Record<string, string>)[meal.mealType || ""] || "餐食"}</span>
                      <strong>{meal.mealName}</strong>
                      <span>{meal.calories ?? "未知"} kcal · 蛋白质 {meal.protein ?? "未知"} g</span>
                    </li>
                  ))}</ol>
                </details>
              </div>
            ) : pendingConfirmation.payload && Object.keys(pendingConfirmation.payload).length ? (
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
            <p>暂无执行记录</p>
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
