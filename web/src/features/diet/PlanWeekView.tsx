import { useCallback, useEffect, useRef, useState } from "react";
import { Archive, ArchiveRestore, Check, ChevronLeft, ChevronRight, Copy, Edit3, History, Plus, RefreshCw, Trash2, X } from "lucide-react";
import { ErrorNotice, LoadingState, PageHeader } from "../../components/Common";
import type { ConnectionState } from "../../types";
import { dietGet, dietWrite, loadPlanLibrary, loadPlanMeals } from "./api";
import { DietPreview } from "./DietPreview";
import { IntakeEditor } from "./IntakeEditor";
import { evidence, meals, rangeText } from "./types";
import type { DietContext, DietPlan, Draft, MealType, PlanMealView, PlannedMeal, Preview } from "./types";
import "./diet.css";

function shift(date: string, offset: number) { const value = new Date(`${date}T12:00:00Z`); value.setUTCDate(value.getUTCDate() + offset); return value.toISOString().slice(0, 10); }
function monday(date: string) { return shift(date, -((new Date(`${date}T12:00:00Z`).getUTCDay() + 6) % 7)); }

export function PlanWeekView({ connection, onLegacy }: { connection: ConnectionState; onLegacy: () => void }) {
  const [date, setDate] = useState(() => {
    const value = new URLSearchParams(location.search).get("plan_date") || "";
    return /^\d{4}-\d{2}-\d{2}$/.test(value) && Number.isFinite(Date.parse(value)) ? value : "";
  });
  const [plans, setPlans] = useState<DietPlan[]>([]);
  const [selected, setSelected] = useState<DietPlan | null>(null);
  const [mealViews, setMealViews] = useState<PlanMealView[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [libraryCursor, setLibraryCursor] = useState<string | null>(null);
  const [deleted, setDeleted] = useState(false);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [preview, setPreview] = useState<Draft | Preview | null>(null);
  const [editor, setEditor] = useState<{ date: string; meal?: PlannedMeal; actual?: boolean; repeat?: boolean; fresh?: boolean } | null>(null);
  const [copy, setCopy] = useState<{ meal?: PlannedMeal } | null>(null);
  const generation = useRef(0);
  const selectedId = useRef(new URLSearchParams(location.search).get("plan_id"));
  const start = date ? monday(date) : "";
  const end = start ? shift(start, 6) : "";
  const online = connection === "online";
  const readMeals = async (id: string, requestGeneration: number) => {
    const page = await loadPlanMeals(`/plans/${encodeURIComponent(id)}/meals`);
    if (generation.current === requestGeneration) { setMealViews(page.items); setCursor(page.next_cursor); }
  };
  const refresh = useCallback(async () => {
    const current = ++generation.current;
    setLoading(true); setError(null);
    try {
      if (!date) { const context = await dietGet<DietContext>("/context"); if (current === generation.current) setDate(context.anchor.anchor_local_date); return; }
      const page = await loadPlanLibrary(`/plan-library?start_date=${start}&end_date=${end}&include_deleted=${deleted}`);
      if (current !== generation.current) return;
      setPlans(page.items); setLibraryCursor(page.next_cursor);
      const selected = page.items.find((plan) => plan.id === selectedId.current) || page.items[0] || null;
      setSelected(selected); selectedId.current = selected?.id || null;
      if (selected) await readMeals(selected.id, current); else { setMealViews([]); setCursor(null); }
    } catch (caught) { if (current === generation.current) setError(caught instanceof Error ? caught.message : "餐单读取失败"); }
    finally { if (current === generation.current) setLoading(false); }
  }, [date, start, end, deleted]);
  useEffect(() => { if (connection !== "checking") void refresh(); return () => { generation.current += 1; }; }, [connection, refresh]);
  useEffect(() => {
    const id = new URLSearchParams(location.search).get("diet_plan_preview");
    if (!id || connection !== "online") return;
    let active = true;
    dietGet<Draft | Preview>(`/previews/${encodeURIComponent(id)}`).then((value) => {
      if (active && value.state === "ready") setPreview(value);
    }).catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : "预览读取失败"); });
    return () => { active = false; };
  }, [connection]);
  const showPreview = (value: Draft | Preview) => { setPreview(value); setEditor(null); setCopy(null); const url = new URL(location.href); url.searchParams.set("diet_plan_preview", value.id); history.replaceState({}, "", url); };
  const closePreview = () => { setPreview(null); const url = new URL(location.href); url.searchParams.delete("diet_plan_preview"); history.replaceState({}, "", url); void refresh(); };
  const prepare = async (operation: string, args: object, version: number) => {
    setBusy(true); setError(null);
    try { showPreview(await dietWrite<Preview>("/previews", { operation, arguments: args }, version)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "准备变更失败"); }
    finally { setBusy(false); }
  };
  const select = async (plan: DietPlan) => {
    selectedId.current = plan.id; setSelected(plan); setBusy(true); setError(null);
    const current = ++generation.current;
    try { await readMeals(plan.id, current); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "餐次读取失败"); }
    finally { setBusy(false); }
  };
  const more = async (library: boolean) => {
    const next = library ? libraryCursor : cursor;
    if (!next || (!library && !selected)) return;
    setBusy(true); setError(null);
    try {
      if (library) { const page = await loadPlanLibrary(`/plan-library?start_date=${start}&end_date=${end}&include_deleted=${deleted}&cursor=${encodeURIComponent(next)}`); setPlans((values) => [...values, ...page.items]); setLibraryCursor(page.next_cursor); }
      else { const page = await loadPlanMeals(`/plans/${encodeURIComponent(selected!.id)}/meals?cursor=${encodeURIComponent(next)}`); setMealViews((values) => [...values, ...page.items]); setCursor(page.next_cursor); }
    } catch (caught) { setError(caught instanceof Error ? caught.message : "餐单已变化，请刷新后重试"); }
    finally { setBusy(false); }
  };
  return <div className="page-container diet-view"><PageHeader title="周餐单" description="" eyebrow="" action={<div className="page-action-group"><button className="button button-secondary" onClick={onLegacy}><History size={16} />历史菜品餐单</button><button className="button button-secondary" disabled={loading} onClick={() => void refresh()}><RefreshCw size={16} />刷新</button><button className="button button-primary" disabled={!online || !date} onClick={() => setEditor({ date: start, fresh: true })}><Plus size={16} />新建周餐单</button></div>} />
    {error && <ErrorNotice message={error} />}{success && <p className="notice notice-success" role="status">{success}</p>}
    <div className="diet-toolbar"><button className="icon-button" title="上一周" aria-label="上一周" disabled={!date} onClick={() => setDate(shift(start, -7))}><ChevronLeft size={18} /></button><label className="diet-week-date"><span>所在周日期</span><input type="date" value={date} required onChange={(event) => setDate(event.target.value)} /></label><button className="icon-button" title="下一周" aria-label="下一周" disabled={!date} onClick={() => setDate(shift(start, 7))}><ChevronRight size={18} /></button><span>{start} 至 {end}</span><label><input type="checkbox" checked={deleted} onChange={(event) => setDeleted(event.target.checked)} />显示已归档</label></div>
    {loading ? <LoadingState label="读取周餐单" /> : <><div className="diet-plan-selector">{plans.map((plan) => <button key={plan.id} className="button button-secondary" aria-pressed={selected?.id === plan.id} onClick={() => void select(plan)} disabled={busy}>{plan.title}{plan.deleted_at ? " · 已归档" : ""}</button>)}{libraryCursor && <button className="button button-secondary" disabled={busy} onClick={() => void more(true)}>更多餐单</button>}</div>
      {selected ? <><div className="diet-section-heading"><h2>{selected.title}</h2><div className="diet-event-actions">{selected.deleted_at ? <button className="button button-secondary" disabled={busy || !online} onClick={() => void prepare("plan_restore", { action: "restore", plan_id: selected.id, base_version: selected.version }, selected.version)}><ArchiveRestore size={16} />恢复餐单</button> : <><button className="button button-secondary" disabled={busy || !online} onClick={() => setCopy({})}><Copy size={16} />复制整周</button><button className="button button-secondary" disabled={busy || !online} onClick={() => void prepare("plan_archive", { action: "archive", plan_id: selected.id, base_version: selected.version }, selected.version)}><Archive size={16} />归档餐单</button></>}</div></div>
        <div className="diet-plan-week">{Array.from({ length: 7 }, (_, offset) => shift(selected.start_date, offset)).map((day) => <section className="diet-plan-day" key={day}><h3>{day}</h3>{(Object.keys(meals) as MealType[]).map((mealType) => {
          const view = mealViews.find((value) => value.meal.local_date === day && value.meal.meal_type === mealType);
          const meal = view?.meal;
          return <div className="diet-plan-slot" key={mealType}><div className="diet-section-heading"><h4>{meals[mealType]}</h4>{!meal && !selected.deleted_at && <button className="icon-button" title={`添加${day}${meals[mealType]}`} aria-label={`添加${day}${meals[mealType]}`} disabled={!online || busy || !!cursor} onClick={() => setEditor({ date: day })}><Plus size={17} /></button>}</div>{meal ? <><ul>{meal.items.map((item) => <li key={item.id}><span>{item.name_snapshot}</span><small>{rangeText(item.nutrition_snapshot.calories_kcal.value, "calories_kcal")}</small></li>)}</ul>{meal.note && <p>{meal.note}</p>}<small>已记实际 {view.actual_refs.length} 次</small>{!selected.deleted_at && <div className="diet-plan-actions"><button className="icon-button" title="编辑餐次" aria-label="编辑餐次" disabled={!online || busy} onClick={() => setEditor({ date: day, meal })}><Edit3 size={16} /></button><button className="icon-button" title="复制餐次" aria-label="复制餐次" disabled={!online || busy} onClick={() => setCopy({ meal })}><Copy size={16} /></button><button className="icon-button" title="删除餐次" aria-label="删除餐次" disabled={!online || busy} onClick={() => void prepare("plan_delete_meal", { action: "delete", planned_meal_id: meal.id, base_version: meal.version, evidence: evidence() }, meal.version)}><Trash2 size={16} /></button><button className="button button-secondary" disabled={!online || busy} onClick={() => setEditor({ date: day, meal, actual: true, repeat: view.actual_refs.length > 0 })}><Check size={15} />{view.actual_refs.length ? "再记一次" : "标记已吃"}</button></div>}</> : <span className="muted">{cursor ? "餐次待加载" : "未安排"}</span>}</div>;
        })}</section>)}</div>{cursor && <button className="button button-secondary" disabled={busy} onClick={() => void more(false)}>加载其余餐次</button>}</> : <div className="diet-empty"><h3>本周暂无餐单</h3></div>}
    </>}
    {editor && <IntakeEditor date={editor.date} plan={editor.actual ? undefined : { parent: !editor.fresh && selected ? { kind: "existing", plan_id: selected.id, base_version: selected.version } : { kind: "new", title: `${start} 周餐单`, start_date: start }, meal: editor.meal }} sourcePlan={editor.actual && editor.meal ? { meal: editor.meal, recordAgain: !!editor.repeat } : undefined} onClose={() => setEditor(null)} onPreview={showPreview} />}
    {copy && selected && <PlanCopyDialog plan={selected} meal={copy.meal} onClose={() => setCopy(null)} onPreview={showPreview} />}
    {preview && <DietPreview value={preview} onClose={closePreview} onSaved={() => { setSuccess("餐单变更已保存。"); closePreview(); }} />}
  </div>;
}

function PlanCopyDialog({ plan, meal, onClose, onPreview }: { plan: DietPlan; meal?: PlannedMeal; onClose: () => void; onPreview: (value: Preview) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [date, setDate] = useState(meal ? shift(meal.local_date, 1) : shift(monday(plan.start_date), 7));
  const [mealType, setMealType] = useState<MealType>(meal?.meal_type || "breakfast");
  const [policy, setPolicy] = useState("reject");
  const [destinations, setDestinations] = useState<DietPlan[]>([]);
  const [destination, setDestination] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const key = useRef(crypto.randomUUID());
  useEffect(() => { const element = dialog.current; element?.showModal(); return () => element?.close(); }, []);
  useEffect(() => {
    if (meal || !date) return;
    let active = true; setLoading(true); setError(null);
    const read = async () => {
      const found: DietPlan[] = []; let cursor: string | null = null;
      do { const page = await loadPlanLibrary(`/plan-library?start_date=${date}&end_date=${shift(date, 6)}${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`); found.push(...page.items); cursor = page.next_cursor; } while (cursor);
      if (active) { setDestinations(found); setDestination(""); }
    };
    void read().catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : "目标餐单读取失败"); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [date, meal]);
  const submit = async () => {
    setBusy(true); setError(null);
    try {
      const existing = destinations.find((value) => value.id === destination);
      const args = meal ? { action: "copy", planned_meal_id: meal.id, base_version: meal.version, target_date: date, target_meal_type: mealType, evidence: evidence() } : { action: "copy_week", source_plan_id: plan.id, base_version: plan.version, parent: existing ? { kind: "existing", plan_id: existing.id, base_version: existing.version } : { kind: "new", title: `${date} 周餐单`, start_date: date }, target_start_date: date, conflict_policy: policy, evidence: evidence() };
      onPreview(await dietWrite<Preview>("/previews", { operation: meal ? "plan_copy_meal" : "plan_copy_week", arguments: args }, meal?.version || plan.version, "POST", key.current));
    } catch (caught) { setError(caught instanceof Error ? caught.message : "复制失败"); }
    finally { setBusy(false); }
  };
  return <dialog ref={dialog} className="diet-dialog" aria-labelledby="plan-copy-title" onCancel={(event) => { if (busy) event.preventDefault(); else onClose(); }}><div className="diet-dialog-heading"><h2 id="plan-copy-title">{meal ? "复制餐次" : "复制整周"}</h2><button className="icon-button" title="关闭复制" aria-label="关闭复制" disabled={busy} onClick={onClose}><X size={18} /></button></div>{error && <ErrorNotice message={error} />}<form onSubmit={(event) => { event.preventDefault(); void submit(); }} onChange={() => { key.current = crypto.randomUUID(); }}><label className="field"><span>{meal ? "复制到日期" : "目标周一"}</span><input type="date" required value={date} onChange={(event) => setDate(event.target.value)} /></label>{meal ? <label className="field"><span>目标餐次</span><select aria-label="目标餐次" value={mealType} onChange={(event) => setMealType(event.target.value as MealType)}>{Object.entries(meals).map(([value, label]) => <option value={value} key={value}>{label}</option>)}</select></label> : <><label className="field"><span>目标餐单</span><select aria-label="目标餐单" value={destination} disabled={loading} onChange={(event) => setDestination(event.target.value)}><option value="">新建餐单</option>{destinations.map((value) => <option key={value.id} value={value.id}>{value.title}</option>)}</select></label><label className="field"><span>同餐冲突处理</span><select aria-label="同餐冲突处理" value={policy} onChange={(event) => setPolicy(event.target.value)}><option value="reject">拒绝冲突，不覆盖</option><option value="append_items">追加食物</option><option value="replace_meals">替换目标餐次</option></select></label></>}<div className="modal-actions"><button type="button" className="button button-secondary" disabled={busy} onClick={onClose}>取消</button><button type="submit" className="button button-primary" disabled={busy || loading}><Copy size={16} />查看复制预览</button></div></form></dialog>;
}
