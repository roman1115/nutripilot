import { ChevronDown, Target } from "lucide-react";
import { goalKeys, nutrientKeys, nutrients, rangeText } from "./types";
import type { DayAnalysis, Nutrient, WeekAnalysis } from "./types";

export function NutritionOverview({ analysis, onEditTarget }: {
  analysis: DayAnalysis | WeekAnalysis;
  onEditTarget: () => void;
}) {
  const day = "actual" in analysis ? analysis : null;
  const week = "days" in analysis ? analysis : null;
  const incomplete = day && nutrientKeys.some((key) => !day.actual.nutrients[key].complete);
  const metric = (key: Nutrient) => {
    const total = day?.actual.nutrients[key];
    const partial = total && !total.complete && total.known_item_count > 0;
    const value = total
      ? total.total || (partial ? total.known_subtotal : null)
      : week!.recorded_days ? week!.nutrients[key].known_subtotal : null;
    return (
      <div className={`diet-nutrient nutrient-${key}`} key={key}>
        <span><i aria-hidden="true" />{nutrients[key].name}</span>
        <strong>{rangeText(value, key)}</strong>
        <small>{partial ? `已知小计 · ${total.known_item_count}/${total.total_item_count} 项有依据` : week ? "本周已知小计" : total?.complete ? "已记录总量" : "营养资料不足"}</small>
      </div>
    );
  };
  return (
    <aside className="diet-nutrition" aria-label="营养汇总">
      <div className="diet-panel-heading">
        <h2>{week ? "本周营养" : "当日营养"}</h2>
        <button className="icon-button" title="营养目标" aria-label="营养目标" onClick={onEditTarget}>
          <Target size={17} />
        </button>
      </div>
      {incomplete && <p className="diet-summary-note">部分营养未知，当前仅计入有依据的数据。</p>}
      <div className="diet-summary">{goalKeys.map(metric)}</div>
      <details className="diet-disclosure diet-nutrition-details">
        <summary>更多营养与计划对比<ChevronDown size={16} aria-hidden="true" /></summary>
        <div className="diet-extra-nutrients">{metric("fiber_g")}{metric("sodium_mg")}</div>
        {day && <dl className="diet-comparison">
          {nutrientKeys.map((key) => {
            const comparison = goalKeys.includes(key as typeof goalKeys[number])
              ? day.target_comparison[key as typeof goalKeys[number]]
              : null;
            const status = comparison?.reason === "target_unknown" ? "未设置目标"
              : day.actual.day_status === "open" ? "尚未声明记录完整"
              : comparison?.status === "not_comparable" ? "数据不足，不能比较"
              : ({ within: "在目标区间内", below: "低于目标区间", above: "高于目标区间", overlap: "与目标区间交叉" } as Record<string, string>)[comparison?.status || ""] || "不设目标";
            return <div key={key}><dt>{nutrients[key].name}</dt><dd>计划 {rangeText(day.planned.nutrients[key].total, key)}<small>{status}</small></dd></div>;
          })}
        </dl>}
      </details>
      {week && <div className="diet-week-details">
        <div className="diet-week-coverage"><span>已记录 <strong>{week.recorded_days}/{week.covered_days}</strong> 天</span><span>完整声明 <strong>{week.complete_days}/{week.covered_days}</strong> 天</span></div>
        <dl>
          <div><dt>已记录日均：</dt><dd>{rangeText(week.nutrients.calories_kcal.recorded_day_average, "calories_kcal")}</dd></div>
          <div><dt>完整日均：</dt><dd>{rangeText(week.nutrients.calories_kcal.complete_day_average, "calories_kcal")}</dd></div>
          <div><dt>自然周日均：</dt><dd>{rangeText(week.nutrients.calories_kcal.seven_day_average, "calories_kcal")}</dd></div>
        </dl>
      </div>}
    </aside>
  );
}
