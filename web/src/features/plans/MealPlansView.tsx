import { useEffect, useMemo, useState } from "react";
import { CalendarDays, CheckCircle2, ChevronRight, CircleAlert, MessageSquareText, RefreshCw, Star, Utensils } from "lucide-react";
import { EmptyState, ErrorNotice, LoadingState, PageHeader, StatusBadge } from "../../components/Common";
import { FeedbackDialog } from "../feedback/FeedbackDialog";
import { getMealPlans } from "../../lib/api";
import { formatDate, nutrientLabel, planNutrientTotal } from "../../lib/format";
import type { ConnectionState, MealFeedback, MealPlan, MealPlanItem } from "../../types";

const mealTypeLabels: Record<string, string> = {
  breakfast: "早餐",
  lunch: "午餐",
  dinner: "晚餐",
  snack: "加餐",
};

function mealTypeLabel(value?: string): string {
  if (!value) return "餐食";
  return mealTypeLabels[value.toLowerCase()] || value;
}

export function MealPlansView({
  connection,
  onNavigateChat,
}: {
  connection: ConnectionState;
  onNavigateChat: () => void;
}) {
  const [plans, setPlans] = useState<MealPlan[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [feedbackMeal, setFeedbackMeal] = useState<MealPlanItem | null>(null);
  const [submittedFeedback, setSubmittedFeedback] = useState<Record<string, MealFeedback>>({});
  const [feedbackNotice, setFeedbackNotice] = useState<string | null>(null);

  const load = async (background = false) => {
    if (!background) {
      setLoading(true);
      setError(null);
    }
    try {
      const values = (await getMealPlans()).sort(
        (left, right) => (right.createdAt || "").localeCompare(left.createdAt || ""),
      );
      setPlans(values);
      setError(null);
      setSelectedId((current) => current && values.some((plan) => plan.id === current) ? current : values[0]?.id ?? null);
    } catch (caught) {
      if (background) return;
      setPlans([]);
      setSelectedId(null);
      setError(caught instanceof Error ? caught.message : "无法读取饮食计划");
    } finally {
      if (!background) setLoading(false);
    }
  };

  useEffect(() => {
    if (connection !== "checking") void load();
    if (connection !== "online") return;
    const refresh = () => void load(true);
    const timer = window.setInterval(refresh, 3000);
    window.addEventListener("focus", refresh);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", refresh);
    };
  }, [connection]);

  const selected = useMemo(() => plans.find((plan) => plan.id === selectedId) ?? null, [plans, selectedId]);
  const groupedItems = useMemo(() => {
    if (!selected) return [];
    const groups = new Map<string, typeof selected.items>();
    for (const item of selected.items) {
      const key = item.date || "未指定日期";
      groups.set(key, [...(groups.get(key) || []), item]);
    }
    return [...groups.entries()];
  }, [selected]);
  const planStats = useMemo(() => {
    if (!selected) return null;
    return {
      dayCount: groupedItems.length,
      mealCount: selected.items.length,
      calories: planNutrientTotal(selected.items, "calories_kcal"),
      protein: planNutrientTotal(selected.items, "protein_g"),
    };
  }, [groupedItems.length, selected]);

  return (
    <div className="page-container">
      <PageHeader
        eyebrow="Saved decisions"
        title="菜品餐单"
        description=""
        action={
          <div className="page-action-group">
            <button className="button button-secondary" type="button" onClick={() => void load()} disabled={loading || connection === "checking"}>
              <RefreshCw size={16} className={loading ? "spin" : ""} />刷新
            </button>
            <button className="button button-primary" type="button" onClick={onNavigateChat}>
              <MessageSquareText size={16} />制定餐单
            </button>
          </div>
        }
      />

      {error ? <ErrorNotice message={error} /> : null}
      {feedbackNotice ? (
        <div className="notice notice-success" role="status">
          <CheckCircle2 size={18} />
          <div><strong>反馈已记录</strong><p>{feedbackNotice}</p></div>
        </div>
      ) : null}
      {loading ? (
        <LoadingState label="正在读取饮食计划" />
      ) : plans.length === 0 ? (
        <EmptyState
          icon={<CalendarDays size={24} />}
          title="还没有已保存的计划"
          description="确认后的餐单会保存在这里。"
          action={<button className="button button-primary" type="button" onClick={onNavigateChat}><MessageSquareText size={16} />开始制定</button>}
        />
      ) : (
        <div className="plans-layout">
          <aside className="plan-list" aria-label="饮食计划列表">
            <div className="panel-heading-small"><span>全部计划</span><b>{plans.length}</b></div>
            {plans.map((plan) => (
              <button className={`plan-list-item${selectedId === plan.id ? " is-active" : ""}`} key={plan.id} type="button" onClick={() => setSelectedId(plan.id)}>
                <div>
                  <strong>{plan.title}</strong>
                  <span>{plan.startDate && plan.endDate ? `${plan.startDate} 至 ${plan.endDate}` : formatDate(plan.createdAt)}</span>
                </div>
                <ChevronRight size={17} />
              </button>
            ))}
          </aside>

          {selected ? (
            <section className="plan-detail">
              <div className="plan-detail-header">
                <div>
                  <div className="plan-title-row"><h2>{selected.title}</h2><StatusBadge status={selected.status} /></div>
                  <p>{selected.startDate && selected.endDate ? `${selected.startDate} 至 ${selected.endDate}` : `创建于 ${formatDate(selected.createdAt)}`}</p>
                </div>
                {planStats ? (
                  <div className="plan-calories">
                    <span>{planStats.dayCount} 天 · {planStats.mealCount} 个餐次</span>
                    <strong>{planStats.calories} kcal</strong>
                    <small>{selected.nutritionBasis === "whole_recipe" ? "所选整菜合计" : "历史记录份量合计"}
                      {" · 蛋白质 "}{planStats.protein} g</small>
                  </div>
                ) : null}
              </div>

              {selected.nutritionBasis !== "whole_recipe" ? <div className="notice notice-warning">
                <CircleAlert size={18} aria-hidden="true" />
                <div><p>历史餐单按原记录份量展示，旧餐食已退出新推荐，原编号和反馈保留。</p></div>
              </div> : null}
              {selected.qualityWarnings.length ? <div className="notice notice-warning" role="status">
                <CircleAlert size={18} aria-hidden="true" />
                <div>{selected.qualityWarnings.map((warning) => <p key={warning}>{warning}</p>)}</div>
              </div> : null}
              {groupedItems.length ? (
                <div className="plan-days">
                  {groupedItems.map(([date, items]) => (
                    <section className="plan-day" key={date}>
                      <div className="plan-day-heading">
                        <CalendarDays size={16} />
                        <strong>{date}</strong>
                        <span>
                          {selected.nutritionBasis === "whole_recipe" ? "整菜合计" : "原记录合计"}
                          {" · "}{planNutrientTotal(items, "calories_kcal")} kcal
                          {" · 蛋白质 "}
                          {planNutrientTotal(items, "protein_g")} g
                        </span>
                      </div>
                      <div className="plan-meals">
                        {items.map((item, index) => (
                          <div className="plan-meal-row" key={String(item.id ?? `${date}-${index}`)}>
                            <div className="meal-slot"><Utensils size={15} /><span>{mealTypeLabel(item.mealType)}</span></div>
                            <strong>{item.mealName}{item.archived ? "（已退役）" : ""}
                              {item.sourceUri ? <a href={item.sourceUri} target="_blank" rel="noreferrer" className="plan-source">菜谱来源</a> : null}
                            </strong>
                            <span>{nutrientLabel(item, "calories_kcal", item.calories)} kcal</span>
                            <button
                              className={`meal-feedback-button${submittedFeedback[String(item.mealId)] ? " is-submitted" : ""}`}
                              type="button"
                              onClick={() => setFeedbackMeal(item)}
                              disabled={!item.mealId || connection !== "online"}
                            >
                              {submittedFeedback[String(item.mealId)] ? <CheckCircle2 size={14} /> : <Star size={14} />}
                              {submittedFeedback[String(item.mealId)] ? "已反馈" : "反馈"}
                            </button>
                          </div>
                        ))}
                      </div>
                    </section>
                  ))}
                </div>
              ) : (
                <div className="inline-empty"><Utensils size={20} /><div><strong>计划暂时没有明细</strong><p>服务端返回了计划记录，但未包含餐食项目。</p></div></div>
              )}
            </section>
          ) : null}
        </div>
      )}

      {feedbackMeal ? (
        <FeedbackDialog
          meal={{ ...feedbackMeal, mealType: mealTypeLabel(feedbackMeal.mealType) }}
          onClose={() => setFeedbackMeal(null)}
          onSubmitted={(feedback) => {
            setSubmittedFeedback((current) => ({ ...current, [String(feedback.mealId)]: feedback }));
            setFeedbackNotice(`已记录对“${feedbackMeal.mealName}”的反馈，后续推荐可据此调整。`);
            setFeedbackMeal(null);
          }}
        />
      ) : null}
    </div>
  );
}
