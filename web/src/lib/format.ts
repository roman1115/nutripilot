import type { MealPlanItem, MealRecommendation } from "../types";

export function normalizeNutrientRanges(value: unknown): MealRecommendation["nutrientRanges"] {
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

export function nutrientLabel(
  meal: Pick<MealRecommendation, "nutrientRanges">,
  field: string,
  fallback?: number,
): string {
  const range = meal.nutrientRanges?.[field];
  const format = (value: number) => Number(value.toFixed(1)).toString();
  if (!range) return fallback === undefined ? "未知" : format(fallback);
  return range.minimum === range.maximum
    ? format(range.minimum)
    : `${format(range.minimum)}～${format(range.maximum)}`;
}

export function planNutrientTotal(items: MealPlanItem[], field: "calories_kcal" | "protein_g"): string {
  let minimum = 0;
  let maximum = 0;
  for (const item of items) {
    const value = field === "calories_kcal" ? item.calories : item.protein;
    const range = item.nutrientRanges?.[field];
    if (!range && value === undefined) return "未知";
    minimum += range?.minimum ?? value!;
    maximum += range?.maximum ?? value!;
  }
  if (!items.length) return "未知";
  return nutrientLabel({ nutrientRanges: { [field]: { minimum, maximum } } }, field);
}

export function formatDate(value?: string): string {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

export function formatDuration(value?: number): string {
  if (value === undefined || value === null) return "--";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(value >= 10_000 ? 1 : 2)} s`;
}

export function formatMetric(value: number | string): string {
  if (typeof value === "string") return value;
  if (value >= 0 && value <= 1) return `${(value * 100).toFixed(1)}%`;
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

export function formatMoney(value?: number): string {
  if (value === undefined || value === null) return "--";
  return `$${value.toFixed(4)}`;
}

export function titleFromKey(value: string): string {
  return value
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (character) => character.toUpperCase());
}

export function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function firstString(...values: unknown[]): string | undefined {
  return values.find((value): value is string => typeof value === "string" && value.length > 0);
}

export function firstNumber(...values: unknown[]): number | undefined {
  return values.find((value): value is number => typeof value === "number" && Number.isFinite(value));
}
