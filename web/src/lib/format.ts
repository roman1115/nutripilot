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
