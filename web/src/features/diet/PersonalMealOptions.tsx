import { useEffect, useRef, useState } from "react";
import { CalendarPlus, Eye, RefreshCw, Utensils, X } from "lucide-react";
import { ErrorNotice } from "../../components/Common";
import { dietGet, dietWrite, loadPlanLibrary } from "./api";
import { DietPreview } from "./DietPreview";
import { goalKeys, meals, nutrients, quantityText, rangeText } from "./types";
import type { DietPlan, MealType, PersonalMealOption, Preview } from "./types";
import "./diet.css";

const portions = [["0.25", "1/4"], ["0.5", "1/2"], ["0.75", "3/4"], ["1", "整批"]];
const grams = [["50", "50 g"], ["100", "100 g"], ["150", "150 g"], ["200", "200 g"]];
const servings = [["0.5", "半份"], ["1", "1 份"], ["1.5", "1.5 份"], ["2", "2 份"]];
type Selection = { thread_id: string; turn_id: string; option_id: string; amounts: (string | null)[] };

export function PersonalMealOptions({
  options, threadId, disabled, onReplace,
}: {
  options: PersonalMealOption[];
  threadId: string;
  disabled: boolean;
  onReplace?: (option: PersonalMealOption) => void;
}) {
  return <div className="recommendation-list" aria-label="个人份量建议">
    {options.map((option) => <PersonalMealCard
      key={option.id} option={option} threadId={threadId} disabled={disabled}
    />)}
    {onReplace && <div className="personal-meal-replace">
      <button type="button" className="button button-secondary" disabled={disabled}
        onClick={() => onReplace(options[0])}><RefreshCw size={16} />换一组</button>
    </div>}
  </div>;
}

function PersonalMealCard({ option, threadId, disabled }: {
  option: PersonalMealOption; threadId: string; disabled: boolean;
}) {
  const [current, setCurrent] = useState(option);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [editing, setEditing] = useState(false);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [saved, setSaved] = useState<DietPlan | null>(null);
  const previewKey = `nutripilot.recommendation-preview.${threadId}.${option.id}`;
  useEffect(() => {
    const id = sessionStorage.getItem(previewKey);
    if (!id || disabled) return;
    let active = true;
    dietGet<Preview>(`/previews/${encodeURIComponent(id)}`).then((value) => {
      if (!active || !("after_plan" in value.content)) return;
      if (value.state === "committed") setSaved(value.content.after_plan);
      if (value.state === "ready") setPreview(value);
    }).catch((caught) => {
      if (active) setError(caught instanceof Error ? caught.message : "预览读取失败");
    });
    return () => { active = false; };
  }, [disabled, previewKey]);
  const closePreview = () => {
    sessionStorage.removeItem(previewKey);
    setPreview(null);
  };
  const amounts = current.items.map(({ quantity }) => {
    if (quantity.kind === "recipe_fraction") return String(Number(quantity.share));
    if (quantity.kind === "mass") return String(Number(quantity.grams));
    if (quantity.kind === "servings") return String(Number(quantity.count));
    return null;
  });
  const selection: Selection = {
    thread_id: threadId, turn_id: option.turn_id, option_id: option.id, amounts,
  };
  const changeAmount = async (index: number, amount: string) => {
    setLoading(true);
    setError("");
    try {
      const changed = amounts.map((value, position) => position === index ? amount : value);
      setCurrent(await dietWrite<PersonalMealOption>("/recommendation-options/calculate", {
        ...selection, amounts: changed,
      }));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "份量调整失败");
    } finally {
      setLoading(false);
    }
  };
  return <article className="meal-result">
    <div className="meal-result-heading">
      <div className="meal-result-icon"><Utensils size={17} /></div>
      <div>
        <span>{option.local_date} · {option.meal_type ? meals[option.meal_type] : "下一餐"} · {current.items.length === 1 ? "单品" : "搭配"}</span>
        <strong>{current.items.map((item) => item.name).join(" + ")}</strong>
      </div>
    </div>
    <div className="diet-plan-actions">
      {current.items.map((item, index) => {
        const amount = amounts[index];
        const kind = item.quantity.kind;
        const choices = kind === "recipe_fraction" ? portions : kind === "mass" ? grams : servings;
        const label = kind === "recipe_fraction" ? "本人占原配方比例" :
          kind === "mass" ? (item.quantity.food_state === "cooked" ? "个人熟重" : "个人重量") : "个人份数";
        return amount === null ?
          <span key={index}>{item.name} · {quantityText(item.quantity)}</span> :
          <label className="field" key={index}>
            <span>{item.name} · {label}</span>
            <select value={amount} disabled={disabled || loading || !!saved}
              onChange={(event) => void changeAmount(index, event.target.value)}>
              {!choices.some(([value]) => value === amount) &&
                <option value={amount}>{quantityText(item.quantity)}</option>}
              {choices.map(([value, text]) => <option key={value} value={value}>{text}</option>)}
            </select>
          </label>;
      })}
    </div>
    <span className="meal-result-basis">个人份量 · 营养估算</span>
    <div className="nutrition-row" aria-live="polite">
      {goalKeys.map((key) => <span key={key}>
        {nutrients[key].name} {rangeText(current.nutrition.nutrients[key].total, key)}
      </span>)}
    </div>
    <p className="meal-result-basis">{current.reason}</p>
    <details>
      <summary>营养依据</summary>
      <ul>{current.items.map((item, index) => <li key={index}>
        {item.evidence.source_uri ?
          <a href={item.evidence.source_uri} target="_blank" rel="noreferrer">{item.name}</a> :
          item.name} · {item.evidence.calculation_note}
      </li>)}</ul>
      <a href="#diet">查看饮食记录与目标</a>
    </details>
    {error && <ErrorNotice message={error} />}
    <div className="diet-plan-actions">
      <button type="button" className="button button-secondary" disabled={disabled || loading || !!saved}
        onClick={() => setEditing(true)}><CalendarPlus size={16} />加入计划</button>
      {saved && <a href={`?${new URLSearchParams({
        plan_date: option.local_date, plan_id: saved.id,
      })}#plans`}>已加入 · 查看周餐单</a>}
    </div>
    {editing && <PersonalPlanDialog option={current} selection={selection}
      onClose={() => setEditing(false)} onPreview={(value) => {
        sessionStorage.setItem(previewKey, value.id);
        setEditing(false);
        setPreview(value);
      }} />}
    {preview && <DietPreview value={preview} onClose={closePreview}
      notice={preview.recommendation_basis_changed ? "已按当前记录、目标和餐次更新保存依据，请核对预览。" : undefined}
      onSaved={() => {
        if ("after_plan" in preview.content) setSaved(preview.content.after_plan);
        setPreview(null);
      }} />}
  </article>;
}

function PersonalPlanDialog({ option, selection, onClose, onPreview }: {
  option: PersonalMealOption;
  selection: Selection;
  onClose: () => void;
  onPreview: (value: Preview) => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const key = useRef(crypto.randomUUID());
  const [mealType, setMealType] = useState<MealType | "">(option.meal_type || "");
  const [plans, setPlans] = useState<DietPlan[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [planId, setPlanId] = useState("");
  const [title, setTitle] = useState(`${option.local_date} 周餐单`);
  const [conflict, setConflict] = useState<"append" | "replace" | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const end = option.local_date;
  const day = new Date(`${end}T12:00:00Z`);
  day.setUTCDate(day.getUTCDate() - 6);
  const start = day.toISOString().slice(0, 10);
  const path = `/plan-library?start_date=${start}&end_date=${end}`;
  useEffect(() => {
    const element = dialog.current;
    element?.showModal();
    let active = true;
    loadPlanLibrary(path).then((page) => {
      if (active) {
        setPlans(page.items.filter((plan) => plan.start_date >= start && plan.start_date <= end));
        setCursor(page.next_cursor);
      }
    }).catch((caught) => {
      if (active) setError(caught instanceof Error ? caught.message : "读取餐单失败");
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; element?.close(); };
  }, [path, start, end]);
  const more = async () => {
    setLoading(true);
    try {
      const page = await loadPlanLibrary(`${path}&cursor=${encodeURIComponent(cursor!)}`);
      setPlans((items) => [...items, ...page.items.filter((plan) => plan.start_date >= start && plan.start_date <= end)]);
      setCursor(page.next_cursor);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "读取餐单失败");
    } finally { setLoading(false); }
  };
  const invalidate = () => { key.current = crypto.randomUUID(); setError(""); };
  const submit = async () => {
    if (!mealType || (!planId && !title.trim())) return;
    const plan = plans.find((value) => value.id === planId);
    setBusy(true);
    setError("");
    try {
      onPreview(await dietWrite<Preview>("/recommendation-options/preview", {
        ...selection, meal_type: mealType, ...(conflict ? { conflict } : {}),
        parent: plan
          ? { kind: "existing", plan_id: plan.id, base_version: plan.version }
          : { kind: "new", title, start_date: option.local_date },
      }, undefined, "POST", key.current));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "准备预览失败");
    } finally { setBusy(false); }
  };
  return <dialog className="diet-dialog" ref={dialog} aria-label="加入计划"
    onCancel={(event) => { if (busy) event.preventDefault(); else onClose(); }}>
    <div className="diet-dialog-heading">
      <h2>加入计划</h2>
      <button type="button" className="icon-button" aria-label="关闭" title="关闭"
        onClick={onClose} disabled={busy}><X size={18} /></button>
    </div>
    {error && <ErrorNotice message={error} />}
    <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <div className="diet-form-grid">
        <label className="field"><span>{option.local_date} · 餐次</span>
          <select value={mealType} required disabled={busy}
            onChange={(event) => { setMealType(event.target.value as MealType); setConflict(null); invalidate(); }}>
            <option value="">请选择餐次</option>
            {Object.entries(meals).map(([type, label]) => <option key={type} value={type}>{label}</option>)}
          </select>
        </label>
        <label className="field"><span>餐单</span>
          <select value={planId} disabled={busy || loading}
            onChange={(event) => { setPlanId(event.target.value); setConflict(null); invalidate(); }}>
            <option value="">新建周餐单</option>
            {plans.map((plan) => <option key={plan.id} value={plan.id}>{plan.title}</option>)}
          </select>
        </label>
      </div>
      {cursor && <button type="button" className="button button-secondary" disabled={loading || busy}
        onClick={() => void more()}>更多餐单</button>}
      {!planId && <label className="field"><span>餐单名称</span>
        <input value={title} required maxLength={120} disabled={busy}
          onChange={(event) => { setTitle(event.target.value); invalidate(); }} />
      </label>}
      {planId && <label className="field"><span>已有同餐次时</span>
        <select value={conflict || ""} disabled={busy}
          onChange={(event) => { setConflict((event.target.value || null) as typeof conflict); invalidate(); }}>
          <option value="">提醒我选择</option><option value="append">追加</option><option value="replace">替换</option>
        </select>
      </label>}
      <div className="modal-actions">
        <button type="submit" className="button button-primary" disabled={busy || loading}>
          <Eye size={16} />{busy ? "正在核算" : "查看预览"}
        </button>
      </div>
    </form>
  </dialog>;
}
