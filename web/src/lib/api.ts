import type {
  ChatStreamEvent,
  ConversationSummary,
  EvaluationRun,
  FeedbackType,
  MealFeedback,
  MealPlan,
  MealPlanItem,
  RuntimeStatus,
  TraceEvent,
  TraceRun,
  UserProfile,
} from "../types";
import { asArray, asRecord, firstNumber, firstString } from "./format";

const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim();
export const API_BASE_URL = (configuredBaseUrl || "http://localhost:8000").replace(/\/$/, "");
export const DEMO_USER_ID = import.meta.env.VITE_DEMO_USER_ID?.trim() || "1";

function activeUserId(): number {
  return Number(sessionStorage.getItem("nutripilot.userId") || DEMO_USER_ID);
}

export async function authenticateAccessToken(token: string): Promise<void> {
  const response = await fetch(endpoint("/api/v1/profile"), {
    headers: { Authorization: `Bearer ${token}` },
    signal: AbortSignal.timeout(5000),
  });
  if (!response.ok) throw await readError(response);
  const profile = asRecord(await response.json());
  const userId = String(profile.user_id);
  if (sessionStorage.getItem("nutripilot.userId") !== userId) {
    localStorage.removeItem("nutripilot.threadId");
    localStorage.removeItem("nutripilot.traceIds");
  }
  sessionStorage.setItem("nutripilot.accessToken", token);
  sessionStorage.setItem("nutripilot.userId", userId);
}

export class ApiError extends Error {
  readonly status?: number;
  readonly details?: unknown;

  constructor(message: string, status?: number, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.details = details;
  }
}

function endpoint(path: string): string {
  return `${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
}

function baseHeaders(): HeadersInit {
  const token = sessionStorage.getItem("nutripilot.accessToken");
  return {
    Accept: "application/json",
    "Content-Type": "application/json",
    "X-User-ID": String(activeUserId()),
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
  };
}

async function readError(response: Response): Promise<ApiError> {
  let details: unknown;
  try {
    details = await response.json();
  } catch {
    details = await response.text().catch(() => undefined);
  }
  const record = asRecord(details);
  const message =
    firstString(record.detail, record.message, record.error) ||
    `请求失败（HTTP ${response.status}）`;
  return new ApiError(message, response.status, details);
}

function unwrap(value: unknown): unknown {
  const record = asRecord(value);
  if (Object.prototype.hasOwnProperty.call(record, "data")) return record.data;
  return value;
}

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await fetch(endpoint(path), {
      ...init,
      headers: { ...baseHeaders(), ...init.headers },
    });
  } catch (error) {
    throw new ApiError(
      error instanceof Error
        ? `无法连接 Agent 服务：${error.message}`
        : "无法连接 Agent 服务",
    );
  }
  if (!response.ok) throw await readError(response);
  if (response.status === 204) return undefined as T;
  return unwrap(await response.json()) as T;
}

export async function checkHealth(signal?: AbortSignal): Promise<RuntimeStatus | null> {
  try {
    const response = await fetch(endpoint("/health/ready"), {
      headers: { Accept: "application/json" },
      signal,
    });
    if (!response.ok) return null;
    const source = asRecord(await response.json());
    return {
      status: firstString(source.status) || "ready",
      appMode: firstString(source.app_mode) || "unknown",
      modelProvider: firstString(source.model_provider) || "unknown",
      modelName: firstString(source.model_name) || "unknown",
      embeddingProvider: firstString(source.embedding_provider) || "unknown",
      embeddingModel: firstString(source.embedding_model) || "unknown",
      rerankProvider: firstString(source.rerank_provider) || "unknown",
      rerankModel: firstString(source.rerank_model) || "unknown",
      storage: firstString(source.storage) || "unknown",
      mcpTransport: firstString(source.mcp_transport) || "unknown",
      authRequired: source.auth_required === true,
    };
  } catch {
    return null;
  }
}

export async function cancelChat(threadId: string): Promise<void> {
  await apiRequest(`/api/v1/chat/${encodeURIComponent(threadId)}/cancel`, {
    method: "POST",
    signal: AbortSignal.timeout(5000),
  });
}

function parseSseBlock(block: string): ChatStreamEvent | null {
  let eventName = "message";
  const dataLines: string[] = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) eventName = line.slice(6).trim();
    if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
  }
  if (!dataLines.length) return null;
  const raw = dataLines.join("\n");
  if (raw === "[DONE]") return { type: "done", data: {}, rawEvent: eventName };
  let parsed: unknown = raw;
  try {
    parsed = JSON.parse(raw);
  } catch {
    // Token streams may intentionally send plain text.
  }
  const record = asRecord(parsed);
  return {
    type: firstString(record.type, record.event, eventName) || "message",
    data: Object.keys(record).length ? record : { content: String(parsed) },
    rawEvent: eventName,
  };
}

async function consumeSse(
  response: Response,
  onEvent: (event: ChatStreamEvent) => void,
): Promise<void> {
  if (!response.body) throw new ApiError("浏览器未收到流式响应体");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value, { stream: !done }).replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const block = buffer.slice(0, boundary).trim();
      buffer = buffer.slice(boundary + 2);
      if (block) {
        const parsed = parseSseBlock(block);
        if (parsed) onEvent(parsed);
      }
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }
  if (buffer.trim()) {
    const parsed = parseSseBlock(buffer.trim());
    if (parsed) onEvent(parsed);
  }
}

export interface ChatRequest {
  threadId: string;
  message: string;
}

export async function streamChat(
  request: ChatRequest,
  onEvent: (event: ChatStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  let response: Response;
  try {
    response = await fetch(endpoint("/api/v1/chat/stream"), {
      method: "POST",
      headers: { ...baseHeaders(), Accept: "text/event-stream, application/json" },
      body: JSON.stringify({
        thread_id: request.threadId,
        user_id: activeUserId(),
        message: request.message,
      }),
      signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") throw error;
    throw new ApiError(
      error instanceof Error
        ? `无法连接对话服务：${error.message}`
        : "无法连接对话服务",
    );
  }
  if (!response.ok) throw await readError(response);

  const contentType = response.headers.get("content-type")?.toLowerCase() || "";
  if (contentType.includes("text/event-stream")) {
    await consumeSse(response, onEvent);
    return;
  }

  const value = unwrap(await response.json());
  const record = asRecord(value);
  const events = asArray(record.events);
  if (events.length) {
    for (const event of events) {
      const eventRecord = asRecord(event);
      onEvent({
        type: firstString(eventRecord.type, eventRecord.event) || "message",
        data: eventRecord,
      });
    }
  } else {
    onEvent({
      type: firstString(record.type, record.event, "final") || "final",
      data: record,
    });
  }
}

export async function confirmChat(
  threadId: string,
  approved: boolean,
  confirmationId: string | undefined,
  idempotencyKey: string,
): Promise<Record<string, unknown>> {
  return apiRequest<Record<string, unknown>>(`/api/v1/chat/${encodeURIComponent(threadId)}/confirm`, {
    method: "POST",
    body: JSON.stringify({
      approved,
      confirmation_id: confirmationId,
      idempotency_key: idempotencyKey,
      user_id: activeUserId(),
    }),
  });
}

export async function getThread(threadId: string): Promise<Record<string, unknown>> {
  return apiRequest<Record<string, unknown>>(
    `/api/v1/threads/${encodeURIComponent(threadId)}?user_id=${activeUserId()}`,
  );
}

export async function getConversations(query: string, offset = 0): Promise<ConversationSummary[]> {
  const value = await apiRequest<{ items: Array<Record<string, unknown>> }>(
    `/api/v1/threads?q=${encodeURIComponent(query)}&limit=30&offset=${offset}`,
  );
  return value.items.map((item) => ({
    threadId: String(item.thread_id),
    title: String(item.title),
    updatedAt: String(item.updated_at),
    messageCount: Number(item.message_count),
  }));
}

export async function getConversationMessages(threadId: string): Promise<Array<Record<string, unknown>>> {
  const messages: Array<Record<string, unknown>> = [];
  for (let offset = 0; ; offset += 200) {
    const page = await apiRequest<{ items: Array<Record<string, unknown>> }>(
      `/api/v1/threads/${encodeURIComponent(threadId)}/messages?limit=200&offset=${offset}`,
    );
    messages.push(...page.items);
    if (page.items.length < 200) return messages;
  }
}

function stringList(...values: unknown[]): string[] {
  const found = values.find(Array.isArray);
  return asArray(found).filter((item): item is string => typeof item === "string");
}

export function normalizeProfile(value: unknown): UserProfile {
  const source = asRecord(unwrap(value));
  return {
    id: (source.id as string | number | undefined) ?? (source.user_id as string | number | undefined),
    displayName: firstString(source.display_name, source.name, source.nickname) || "",
    goals: stringList(source.goals, source.health_goals),
    tastes: stringList(source.tastes, source.preferences, source.taste_preferences),
    cuisines: stringList(source.cuisines, source.cuisine_preferences),
    avoidances: stringList(source.avoidances, source.dislikes),
    allergens: stringList(source.allergens, source.allergies),
    dietaryRestrictions: stringList(source.dietary_restrictions, source.restrictions),
    dailyCalorieTarget: firstNumber(source.daily_calorie_target, source.calorie_target),
    dailyProteinTarget: firstNumber(source.daily_protein_target_g),
    location: firstString(source.location, source.city) || "",
    updatedAt: firstString(source.updated_at),
  };
}

export async function getProfile(): Promise<UserProfile> {
  return normalizeProfile(await apiRequest<unknown>("/api/v1/profile"));
}

export async function updateProfile(profile: UserProfile): Promise<UserProfile> {
  const value = await apiRequest<unknown>("/api/v1/profile", {
    method: "PUT",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      goals: profile.goals,
      tastes: profile.tastes,
      display_name: profile.displayName || undefined,
      cuisines: profile.cuisines,
      avoidances: profile.avoidances,
      allergens: profile.allergens,
      dietary_restrictions: profile.dietaryRestrictions,
      daily_calorie_target: profile.dailyCalorieTarget ?? null,
      daily_protein_target_g: profile.dailyProteinTarget ?? null,
      location: profile.location || undefined,
    }),
  });
  return normalizeProfile(value);
}

export function normalizePlanItem(value: unknown): MealPlanItem {
  const source = asRecord(value);
  const meal = asRecord(source.meal);
  return {
    id: (source.id as string | number | undefined) ?? (meal.id as string | number | undefined),
    mealId:
      (source.meal_id as string | number | undefined) ??
      (meal.id as string | number | undefined),
    date: firstString(source.date, source.meal_date, source.plan_date),
    mealType: firstString(source.meal_type, source.mealType, source.slot),
    mealName: firstString(source.meal_name, source.name, meal.name, meal.title) || "未命名餐食",
    calories: firstNumber(source.calories, source.calories_kcal, meal.calories, meal.calories_kcal),
    protein: firstNumber(source.protein_g),
    status: firstString(source.status),
  };
}

function normalizePlan(value: unknown, index: number): MealPlan {
  const source = asRecord(value);
  return {
    id: String(source.id ?? source.plan_id ?? `plan-${index}`),
    title: firstString(source.title, source.name) || "饮食计划",
    status: firstString(source.status) || "active",
    startDate: firstString(source.start_date, source.startDate),
    endDate: firstString(source.end_date, source.endDate),
    createdAt: firstString(source.created_at, source.createdAt),
    totalCalories: firstNumber(source.total_calories, source.totalCalories),
    qualityWarnings: stringList(asRecord(source.quality).warnings),
    items: asArray(source.items ?? source.meals).map(normalizePlanItem),
  };
}

export async function getMealPlans(): Promise<MealPlan[]> {
  const value = await apiRequest<unknown>("/api/v1/meal-plans");
  const source = asRecord(value);
  return asArray(Array.isArray(value) ? value : source.items ?? source.plans).map(normalizePlan);
}

function normalizeTraceEvent(value: unknown, index: number): TraceEvent {
  const source = asRecord(value);
  const payload = asRecord(source.payload);
  return {
    id: String(source.id ?? source.event_id ?? `event-${index}`),
    order: firstNumber(source.step_order, source.order, source.sequence) ?? index + 1,
    type: firstString(source.event_type, source.type, source.name) || "event",
    component: firstString(source.component, source.agent, source.tool_name) || "system",
    status: firstString(source.status) || "completed",
    timestamp: firstString(source.timestamp, source.started_at, source.created_at),
    durationMs: firstNumber(source.duration_ms, source.latency_ms),
    model: firstString(source.model, source.model_name, payload.model),
    inputTokens: firstNumber(source.input_tokens),
    outputTokens: firstNumber(source.output_tokens),
    input: source.input_summary ?? source.input ?? payload.input,
    output: source.output_summary ?? source.output ?? payload.output ?? (Object.keys(payload).length ? payload : undefined),
    error: firstString(source.error, source.error_message),
    fromPhase: firstString(source.from_phase, source.phase_before),
    toPhase: firstString(source.to_phase, source.phase_after),
  };
}

export function normalizeTrace(value: unknown, fallbackId: string): TraceRun {
  const source = asRecord(unwrap(value));
  const events = asArray(source.events ?? source.spans)
    .map(normalizeTraceEvent)
    .sort((left, right) => left.order - right.order);
  const inferredTokens = events.reduce(
    (total, event) => total + (event.inputTokens ?? 0) + (event.outputTokens ?? 0),
    0,
  );
  const inferredDuration = events.reduce((total, event) => total + (event.durationMs ?? 0), 0);
  return {
    traceId: String(source.trace_id ?? source.id ?? fallbackId),
    threadId: firstString(source.thread_id),
    status: firstString(source.status) || (events.length ? "completed" : "unknown"),
    startedAt: firstString(source.started_at, source.created_at, asRecord(events[0]).timestamp),
    durationMs: firstNumber(source.duration_ms, source.latency_ms) ?? inferredDuration,
    totalTokens: firstNumber(source.total_tokens, source.token_count) ?? inferredTokens,
    totalCost: firstNumber(source.total_cost, source.cost),
    events,
  };
}

export async function getTrace(traceId: string): Promise<TraceRun> {
  return normalizeTrace(
    await apiRequest<unknown>(`/api/v1/traces/${encodeURIComponent(traceId)}`),
    traceId,
  );
}

function normalizeEvaluation(value: unknown, fallbackId = ""): EvaluationRun {
  const source = asRecord(unwrap(value));
  const metricsSource = asRecord(source.metrics ?? source.summary);
  const metrics: Record<string, number | string> = {};
  for (const [key, metric] of Object.entries(metricsSource)) {
    if (typeof metric === "number" || typeof metric === "string") metrics[key] = metric;
  }
  const failures = asArray(source.failures ?? source.failed_cases).map((failure, index) => {
    const item = asRecord(failure);
    return {
      caseId: String(item.case_id ?? item.id ?? `case-${index + 1}`),
      input: firstString(item.input, item.user_message),
      reason: firstString(item.reason, item.error, item.failure_reason) || "未提供失败原因",
      expected: item.expected,
      actual: item.actual,
    };
  });
  return {
    runId: String(source.run_id ?? source.id ?? fallbackId),
    status: firstString(source.status) || "unknown",
    datasetVersion: firstString(source.dataset_version, source.dataset),
    promptVersion: firstString(source.prompt_version),
    promptVersions: Object.fromEntries(
      Object.entries(asRecord(source.prompt_versions)).filter(
        (entry): entry is [string, string] => typeof entry[1] === "string",
      ),
    ),
    startedAt: firstString(source.started_at, source.created_at),
    completedAt: firstString(source.completed_at, source.finished_at),
    totalCases: firstNumber(source.total_cases, source.case_count),
    passedCases: firstNumber(source.passed_cases, source.passed),
    failedCases: firstNumber(source.failed_cases_count, source.failed),
    metrics,
    failures,
  };
}

export async function startEvaluation(options: {
  datasetVersion?: string;
  promptVersion?: string;
}): Promise<EvaluationRun> {
  const value = await apiRequest<unknown>("/api/v1/evaluations/run", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      dataset_version: options.datasetVersion || undefined,
      prompt_version: options.promptVersion || undefined,
    }),
  });
  return normalizeEvaluation(value);
}

export async function getEvaluation(runId: string): Promise<EvaluationRun> {
  return normalizeEvaluation(
    await apiRequest<unknown>(`/api/v1/evaluations/${encodeURIComponent(runId)}`),
    runId,
  );
}

function normalizeFeedback(value: unknown, mealId: number, feedbackType: FeedbackType): MealFeedback {
  const source = asRecord(unwrap(value));
  return {
    id: String(source.id ?? crypto.randomUUID()),
    mealId: firstNumber(source.meal_id) ?? mealId,
    feedbackType: (firstString(source.feedback_type) as FeedbackType | undefined) ?? feedbackType,
    rating: firstNumber(source.rating),
    reason: firstString(source.reason),
    createdAt: firstString(source.created_at),
  };
}

export async function submitMealFeedback(input: {
  mealId: number;
  feedbackType: FeedbackType;
  rating?: number;
  reason?: string;
}): Promise<MealFeedback> {
  const value = await apiRequest<unknown>("/api/v1/feedback", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID() },
    body: JSON.stringify({
      meal_id: input.mealId,
      feedback_type: input.feedbackType,
      rating: input.rating,
      reason: input.reason?.trim() || undefined,
    }),
  });
  return normalizeFeedback(value, input.mealId, input.feedbackType);
}
