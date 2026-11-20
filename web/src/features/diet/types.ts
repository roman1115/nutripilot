export type Nutrient = "calories_kcal" | "protein_g" | "fat_g" | "carbohydrates_g" | "fiber_g" | "sodium_mg";
export type MealType = "breakfast" | "lunch" | "dinner" | "snack";
export interface Range { lower: string; upper: string }
export interface NutrientValue { value: Range | null; basis: string; missing_reasons: string[] }
export type FoodRef = { kind: "unknown"; name: string } | { kind: "ingredient"; id: string; version: string } | { kind: "recipe"; id: number; version: string } | { kind: "personal_food"; id: string; version: number };
export type Quantity = { kind: "unknown"; description: string } | { kind: "mass"; grams: string; food_state: string } | { kind: "mass_range"; grams_lower: string; grams_upper: string; food_state: string } | { kind: "servings"; count: string; serving_evidence_id: string } | { kind: "recipe_fraction"; batch_multiplier: string; share: string } | { kind: "cooked_mass"; batch_multiplier: string; consumed_grams: string; batch_cooked_grams: string };
export interface DietItem { id: string; version: number; client_item_key: string; name_snapshot: string; food_ref: FoodRef; quantity: Quantity; note: string | null; nutrition_snapshot: Record<Nutrient, NutrientValue>; evidence_snapshot: { source_kind: "catalog" | "label" | "user_report" | "model_estimate"; source_ref: string; source_version: string; calculation_note: string; source_uri: string | null; license: string | null } | null }
export interface DietLog { id: string; version: number; temporal: { local_date: string; local_time?: string | null; utc_offset_minutes?: number | null; meal_type: MealType; note: string | null }; items: DietItem[]; deleted_at: string | null; timezone: string }
export interface ProposedItem { client_item_key: string; food: FoodRef; quantity: Quantity; note?: string }
export interface Entry { actuality: "actual" | "planned" | "hypothetical" | "unknown"; subject: "self" | "third_party" | "unknown"; temporal: { local_date: string; local_time?: string | null; utc_offset_minutes?: number | null; meal_type: MealType; note?: string | null }; items: ProposedItem[] }
export interface Draft { id: string; version: number; state: string; preview_hash: string; expires_at: string; job_id?: string | null; channel: string; media_ids: string[]; source_text?: string | null; field_locks_version: number; parse_message?: string; parse_suggestion?: Entry | null; parse_candidates?: Entry[]; label_suggestion?: { original_excerpt: string; clarification: string } | null; content: { entry: Entry; items: DietItem[]; field_locks: string[]; anchor: { timezone: string } } }
export interface DietJob { id: string; kind: "parse_text" | "parse_image" | "export" | "purge"; state: "queued" | "running" | "succeeded" | "failed" | "cancelled"; error_code: string | null; attempt: number; usage: { result: "pending" | "applied" | "suggestion" | "not_applied" } }
export interface LabelProposal { name: string; brand: string; basis: string; serving_grams: string | null; source_note: string; nutrients: Record<Nutrient, Range | null>; media_id: string; media_ids: string[]; label_draft_id: string; label_draft_version: number }
export interface Target { id: string; version: number; effective_date: string; goals: Record<GoalNutrient, Range | null> }
export interface PersonalFood { id: string; version: number; name: string; brand: string | null; deleted_at: string | null; allergens: { name: string; state: string; evidence_ref: string | null; explanation: string }[]; evidence: { food_state: string; basis_quantity: "per_100g" | "per_serving"; original_excerpt: string; source_kind: string; serving_grams: string | null; media_id: string | null; media_ids: string[]; nutrients: Record<Nutrient, NutrientValue> } }
export interface DayDeclaration { local_date: string; version: number; status: "open" | "complete" | "no_intake" }
export interface DietPlan { id: string; version: number; title: string; start_date: string; deleted_at: string | null }
export interface PlannedMeal { id: string; version: number; plan_id: string; local_date: string; meal_type: MealType; note: string | null; items: DietItem[]; deleted_at: string | null }
export interface PlanMealView { plan: DietPlan; meal: PlannedMeal; actual_refs: { id: string; version: number }[] }
export type PlanParent = { kind: "new"; title: string; start_date: string } | { kind: "existing"; plan_id: string; base_version: number };
export type GoalNutrient = "calories_kcal" | "protein_g" | "fat_g" | "carbohydrates_g";
export interface Preview { id: string; version: number; state: string; content_hash: string; expires_at: string; recommendation_basis_changed?: boolean; recommendation_note?: string | null; content: { operation: string; before: DietLog | Target | PersonalFood | DayDeclaration | null; after: DietLog | Target | PersonalFood | DayDeclaration } | { operation: string; before_plan: DietPlan | null; after_plan: DietPlan; before_meals: PlannedMeal[]; after_meals: PlannedMeal[] } }
export interface NutrientTotal { total: Range | null; known_subtotal: Range; complete: boolean; known_item_count: number; total_item_count: number; missing_reasons: string[] }
export interface PersonalMealOption {
  id: string;
  turn_id: string;
  local_date: string;
  meal_type: MealType | null;
  diet_epoch: number | null;
  items: {
    name: string;
    quantity: Quantity;
    evidence: { source_uri: string | null; calculation_note: string };
  }[];
  nutrition: { nutrients: Record<Nutrient, NutrientTotal> };
  reason: string;
}
export interface DayAnalysis { local_date: string; actual: { state: string; day_status: string; record_count: number; nutrients: Record<Nutrient, NutrientTotal> }; planned: { nutrients: Record<Nutrient, NutrientTotal> }; target_comparison: Record<GoalNutrient, { status: string; reason: string | null }> }
export interface WeekAnalysis { week_start: string; covered_days: number; recorded_days: number; complete_days: number; days: DayAnalysis[]; nutrients: Record<Nutrient, { known_subtotal: Range; recorded_day_average: Range | null; complete_day_average: Range | null; seven_day_average: Range | null }> }
export interface FoodHit { food_ref: FoodRef; name: string; food_state: string; basis: string; serving_evidence_id: string | null }
export interface Page<T> { items: T[]; next_cursor: string | null; truncated: boolean; diet_epoch: number }
export interface DietContext { timezone: string; diet_epoch: number; anchor: { anchor_local_date: string }; capabilities: { text: boolean; image: boolean } }
export const meals: Record<MealType, string> = { breakfast: "早餐", lunch: "午餐", dinner: "晚餐", snack: "加餐" };
export const nutrients: Record<Nutrient, { name: string; unit: string }> = {
  calories_kcal: { name: "热量", unit: "kcal" }, protein_g: { name: "蛋白质", unit: "g" }, fat_g: { name: "脂肪", unit: "g" }, carbohydrates_g: { name: "碳水", unit: "g" }, fiber_g: { name: "膳食纤维", unit: "g" }, sodium_mg: { name: "钠", unit: "mg" },
};
export const goalKeys: GoalNutrient[] = ["calories_kcal", "protein_g", "fat_g", "carbohydrates_g"];
export const nutrientKeys: Nutrient[] = ["calories_kcal", "protein_g", "fat_g", "carbohydrates_g", "fiber_g", "sodium_mg"];
export function quantityText(value: Quantity): string {
  switch (value.kind) {
    case "unknown": return `份量未知：${value.description}`;
    case "mass": return `${value.grams} g · ${({ raw: "生重", cooked: "熟重", as_sold: "销售状态", unknown: "生熟未知" } as Record<string, string>)[value.food_state]}`;
    case "mass_range": return `${value.grams_lower}–${value.grams_upper} g · 估算区间`;
    case "servings": return `${value.count} 份 · 有份量依据`;
    case "recipe_fraction": return `烹饪 ${value.batch_multiplier} 倍 · 本人占比 ${value.share}`;
    case "cooked_mass": return `烹饪 ${value.batch_multiplier} 倍 · 本人 ${value.consumed_grams}/${value.batch_cooked_grams} g 熟重`;
  }
}
export function rangeText(value: Range | null, field: Nutrient): string {
  if (!value) return "未知";
  const digits = field === "calories_kcal" || field === "sodium_mg" ? 0 : 1;
  const factor = 10 ** digits;
  const low = Math.floor(Number(value.lower) * factor) / factor;
  const high = Math.ceil(Number(value.upper) * factor) / factor;
  return `${low === high ? low.toFixed(digits) : `${low.toFixed(digits)}–${high.toFixed(digits)}`} ${nutrients[field].unit}`;
}
export function evidence(field = "operation") {
  return [{ field, source_kind: "user_form", source_ref: "diet-form", original_text: "explicit form submission", operation: "set", status: "explicit" }];
}
