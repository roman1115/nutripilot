export type ViewKey = "chat" | "profile" | "plans" | "traces" | "evaluations";

export type ConnectionState = "checking" | "online" | "offline";
export type ActivityStatus = "waiting" | "running" | "success" | "error";

export interface RuntimeStatus {
  status: string;
  appMode: string;
  modelProvider: string;
  modelName: string;
  embeddingProvider: string;
  embeddingModel: string;
  rerankProvider: string;
  rerankModel: string;
  storage: string;
  mcpTransport: string;
  authRequired: boolean;
}

export interface Citation {
  id?: string | number;
  title: string;
  source?: string;
  excerpt?: string;
  score?: number;
  url?: string;
}

export interface MealRecommendation {
  mealId?: string | number;
  name: string;
  reason?: string;
  calories?: number;
  protein?: number;
  carbs?: number;
  fat?: number;
  tags?: string[];
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  createdAt: string;
  isStreaming?: boolean;
  recommendations?: MealRecommendation[];
  citations?: Citation[];
  warnings?: string[];
  suggestedActions?: string[];
  traceId?: string;
}

export interface ToolActivity {
  id: string;
  name: string;
  label: string;
  status: ActivityStatus;
  detail?: string;
  durationMs?: number;
  kind: "agent" | "tool" | "guard" | "system";
}

export interface PendingConfirmation {
  id?: string;
  title: string;
  description: string;
  operation?: string;
  payload?: Record<string, unknown>;
  mealPreview?: MealPlanItem[];
  idempotencyKey: string;
}

export interface ChatStreamEvent {
  type: string;
  data: Record<string, unknown>;
  rawEvent?: string;
}

export interface UserProfile {
  id?: string | number;
  displayName: string;
  goals: string[];
  tastes: string[];
  cuisines: string[];
  avoidances: string[];
  allergens: string[];
  dietaryRestrictions: string[];
  dailyCalorieTarget?: number;
  dailyProteinTarget?: number;
  location: string;
  updatedAt?: string;
}

export interface MealPlanItem {
  id?: string | number;
  mealId?: string | number;
  date?: string;
  mealType?: string;
  mealName: string;
  calories?: number;
  protein?: number;
  status?: string;
}

export interface ConversationSummary {
  threadId: string;
  title: string;
  updatedAt: string;
  messageCount: number;
}

export interface MealPlan {
  id: string;
  title: string;
  status: string;
  startDate?: string;
  endDate?: string;
  createdAt?: string;
  totalCalories?: number;
  qualityWarnings: string[];
  items: MealPlanItem[];
}

export interface TraceEvent {
  id: string;
  order: number;
  type: string;
  component: string;
  status: string;
  timestamp?: string;
  durationMs?: number;
  model?: string;
  inputTokens?: number;
  outputTokens?: number;
  input?: unknown;
  output?: unknown;
  error?: string;
  fromPhase?: string;
  toPhase?: string;
}

export type FeedbackType = "like" | "dislike" | "replace" | "rating";

export interface MealFeedback {
  id: string;
  mealId: number;
  feedbackType: FeedbackType;
  rating?: number;
  reason?: string;
  createdAt?: string;
}

export interface TraceRun {
  traceId: string;
  threadId?: string;
  status: string;
  startedAt?: string;
  durationMs?: number;
  totalTokens?: number;
  totalCost?: number;
  events: TraceEvent[];
}

export interface EvaluationFailure {
  caseId: string;
  input?: string;
  reason: string;
  expected?: unknown;
  actual?: unknown;
}

export interface EvaluationRun {
  runId: string;
  status: string;
  datasetVersion?: string;
  promptVersion?: string;
  promptVersions: Record<string, string>;
  startedAt?: string;
  completedAt?: string;
  totalCases?: number;
  passedCases?: number;
  failedCases?: number;
  metrics: Record<string, number | string>;
  failures: EvaluationFailure[];
}
