import { useEffect, useState } from "react";
import { ErrorNotice } from "../../components/Common";
import { dietGet } from "./api";
import { nutrientKeys, nutrients, quantityText, rangeText } from "./types";
import type { DayAnalysis, Nutrient, Page, Quantity, Range } from "./types";

interface Deviation { analysis: DayAnalysis; plan_actual_difference: Record<Nutrient, Range | null>; comparison_reasons: Record<Nutrient, string | null>; execution: { planned_meal_ref: { id: string }; recorded_count: number; items: { planned_item_id: string; planned_quantity: Quantity; actual_quantities: Quantity[]; reason: string | null }[] }[] }
const reasons: Record<string, string> = { day_not_complete: "尚未声明记录完整", no_plan: "没有计划", nutrition_incomplete: "营养资料不完整", not_recorded: "尚未记录", quantity_basis_mismatch_or_unknown: "份量未知或口径不同" };

export function DeviationView({ start, end, revision }: { start: string; end: string; revision: number }) {
  const [rows, setRows] = useState<Deviation[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  useEffect(() => {
    let active = true; setLoading(true); setError(null);
    void dietGet<Page<Deviation>>(`/deviation?start_date=${start}&end_date=${end}`).then((result) => {
      if (active) setRows(result.items);
    }).catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : "偏差读取失败"); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [start, end, revision]);
  return <section className="diet-deviation" aria-label="计划与实际偏差"><h2>计划与实际偏差</h2>{error && <ErrorNotice message={error} />}{loading ? <p role="status">正在读取偏差</p> : rows.map((row) => <div className="diet-deviation-day" key={row.analysis.local_date}><h3>{row.analysis.local_date}</h3><dl className="diet-deviation-values">{nutrientKeys.map((key) => <div key={key}><dt>{nutrients[key].name}</dt><dd>{row.plan_actual_difference[key] ? rangeText(row.plan_actual_difference[key], key) : reasons[row.comparison_reasons[key] || ""] || "不可比较"}</dd></div>)}</dl>{row.execution.map((meal) => <div className="diet-deviation-execution" key={meal.planned_meal_ref.id}><strong>关联计划餐次：{meal.recorded_count ? `已记录 ${meal.recorded_count} 次` : "尚未记录"}</strong>{meal.items.map((item) => <p key={item.planned_item_id}>计划 {quantityText(item.planned_quantity)} · 实际 {item.actual_quantities.length ? item.actual_quantities.map(quantityText).join("；") : "未记录"}{item.reason ? ` · ${reasons[item.reason] || "不可比较"}` : ""}</p>)}</div>)}</div>)}</section>;
}
