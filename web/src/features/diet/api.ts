import { apiRequest } from "../../lib/api";
import type { DietLog, DietPlan, Draft, Page, PersonalFood, PlanMealView, Preview } from "./types";

export function dietGet<T>(path: string): Promise<T> { return apiRequest<T>(`/api/v1/diet${path}`); }
export function dietWrite<T>(path: string, body: unknown, version?: number, method = "POST", key: string = crypto.randomUUID()): Promise<T> {
  return apiRequest<T>(`/api/v1/diet${path}`, { method, headers: { "Idempotency-Key": key, ...(version !== undefined ? { "If-Match": `"${version}"` } : {}) }, body: JSON.stringify(body) });
}
export function confirmDiet(value: Draft | Preview, approved: boolean): Promise<unknown> {
  return dietWrite(`/previews/${value.id}/confirm`, { approved, preview_version: value.version, content_hash: "preview_hash" in value ? value.preview_hash : value.content_hash }, value.version, "POST", `confirm:${value.id}:${value.version}:${approved}`);
}
async function loadPage<T extends { id: string }>(path: string, detail: string): Promise<Page<T>> {
  const page = await dietGet<Page<T | { ref: { id: string } }>>(path);
  const items = await Promise.all(page.items.map((item) => "ref" in item ? dietGet<T>(`${detail}/${encodeURIComponent(item.ref.id)}`) : item));
  return { ...page, items };
}
export function loadLogs(path: string) { return loadPage<DietLog>(path, "/logs"); }
export function loadDrafts(path: string) { return loadPage<Draft>(path, "/drafts"); }
export function loadPersonalFoods(path: string) { return loadPage<PersonalFood>(path, "/foods"); }
export function loadPlanLibrary(path: string) { return loadPage<DietPlan>(path, "/plans"); }
export async function loadPlanMeals(path: string): Promise<Page<PlanMealView>> {
  const page = await dietGet<Page<PlanMealView | { ref: { id: string } }>>(path);
  const items = await Promise.all(page.items.map((item) => "ref" in item ? dietGet<PlanMealView>(`/planned-meals/${item.ref.id}`) : item));
  return { ...page, items };
}
