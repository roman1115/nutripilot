import { useEffect, useRef, useState } from "react";
import { Plus, Search, Trash2, X, Eye, LoaderCircle, Split, ScanText } from "lucide-react";
import { ErrorNotice } from "../../components/Common";
import { dietGet, dietWrite } from "./api";
import { evidence, meals } from "./types";
import { ParseJobStatus } from "./ParseIntake";
import { PrivateImages } from "./PrivateImages";
import type { DietLog, Draft, Entry, FoodHit, FoodRef, MealType, Page, PlanParent, PlannedMeal, Preview, ProposedItem, Quantity } from "./types";

interface EditableItem { key: string; name: string; food: FoodRef; quantity: Quantity; note: string; existingId?: string; servingEvidence?: string }
function emptyItem(): EditableItem { return { key: `f${crypto.randomUUID().replaceAll("-", "").slice(0, 20)}`, name: "", food: { kind: "unknown", name: "" }, quantity: { kind: "unknown", description: "未提供份量" }, note: "" }; }

export function IntakeEditor({ date, initial: initialDraft, record, plan, sourcePlan, onClose, onPreview, onSplit, onLabel }: { date: string; initial?: Draft; record?: DietLog; plan?: { parent: PlanParent; meal?: PlannedMeal }; sourcePlan?: { meal: PlannedMeal; recordAgain: boolean }; onClose: () => void; onPreview: (preview: Draft | Preview) => void; onSplit?: () => void; onLabel?: (draft: Draft) => void }) {
  const [initial, setInitial] = useState(initialDraft);
  const dialog = useRef<HTMLDialogElement>(null);
  const plannedMeal = plan?.meal || sourcePlan?.meal;
  const [localDate, setDate] = useState(initial?.content.entry.temporal.local_date || record?.temporal.local_date || plannedMeal?.local_date || date);
  const [mealType, setMealType] = useState<MealType | "">(initial?.content.entry.temporal.meal_type || record?.temporal.meal_type || plannedMeal?.meal_type || "");
  const [note, setNote] = useState(initial?.content.entry.temporal.note || record?.temporal.note || plannedMeal?.note || "");
  const [localTime, setTime] = useState(initial?.content.entry.temporal.local_time || record?.temporal.local_time || "");
  const [offset, setOffset] = useState(String(initial?.content.entry.temporal.utc_offset_minutes ?? record?.temporal.utc_offset_minutes ?? ""));
  const [timezone, setTimezone] = useState(record?.timezone || "");
  const [actuality, setActuality] = useState<Entry["actuality"]>(initial?.content.entry.actuality || "actual");
  const [subject, setSubject] = useState<Entry["subject"]>(initial?.content.entry.subject || "self");
  const [unlocks, setUnlocks] = useState<string[]>([]);
  const [items, setItems] = useState<EditableItem[]>(() => (initial?.content.items.length ? initial.content.items : record?.items || plannedMeal?.items)?.map((item) => ({ key: item.client_item_key, name: item.name_snapshot, food: item.food_ref, quantity: item.quantity, note: item.note || "", existingId: sourcePlan ? undefined : item.id })) || [emptyItem()]);
  const [staged, setStaged] = useState<Entry[]>([]);
  const [planTitle, setPlanTitle] = useState(plan?.parent.kind === "new" ? plan.parent.title : "");
  const [matches, setMatches] = useState<{ key: string; foods: FoodHit[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submitKey = useRef(crypto.randomUUID());
  const splitKey = useRef(crypto.randomUUID());
  const split = async () => {
    if (!initial || !onSplit) return;
    setBusy(true); setError(null);
    try { await dietWrite<{ items: Draft[] }>(`/drafts/${initial.id}/split`, {}, initial.version, "POST", splitKey.current); onSplit(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "拆分失败"); }
    finally { setBusy(false); }
  };
  const loadEntry = (entry: Entry) => {
    setDate(entry.temporal.local_date || date); setMealType(entry.temporal.meal_type || "");
    setActuality(entry.actuality); setSubject(entry.subject); setNote(entry.temporal.note || "");
    setTime(entry.temporal.local_time || ""); setOffset(String(entry.temporal.utc_offset_minutes ?? ""));
    setItems(entry.items.map((item) => ({ key: item.client_item_key, name: item.food.kind === "unknown" ? item.food.name : "已匹配食物", food: item.food, quantity: item.quantity, note: item.note || "", existingId: initial?.content.items.find((old) => old.client_item_key === item.client_item_key)?.id })));
    submitKey.current = crypto.randomUUID();
  };
  useEffect(() => { const element = dialog.current; element?.showModal(); return () => element?.close(); }, []);
  const update = (key: string, changes: Partial<EditableItem>) => { setItems((current) => current.map((item) => item.key === key ? { ...item, ...changes } : item)); submitKey.current = crypto.randomUUID(); setError(null); };
  const search = async (item: EditableItem) => {
    setBusy(true); setError(null);
    try { const result = await dietGet<Page<FoodHit>>(`/foods?query=${encodeURIComponent(item.name)}`); setMatches({ key: item.key, foods: result.items }); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "查询失败"); }
    finally { setBusy(false); }
  };
  const submit = async () => {
    if (!mealType) { setError("请选择餐次。"); return; }
    setBusy(true); setError(null);
    try {
      const proposed: ProposedItem[] = items.map((item) => ({ client_item_key: item.key, food: item.food.kind === "unknown" ? { kind: "unknown", name: item.name.trim() } : item.food, quantity: item.quantity, ...(item.note ? { note: item.note } : {}) }));
      const entry: Entry = { actuality: plan ? "planned" : actuality, subject, temporal: { local_date: localDate, meal_type: mealType, note, ...(!plan ? { local_time: localTime || null, utc_offset_minutes: offset === "" || !localTime ? null : Number(offset) } : {}) }, items: proposed };
      if (plan && !plan.meal) {
        const parent = plan.parent.kind === "new" ? { ...plan.parent, title: planTitle } : plan.parent;
        onPreview(await dietWrite<Preview>("/previews", { operation: "plan_create", arguments: { action: "create", parent, entries: [...staged, entry].map((value, index) => ({ client_entry_key: `entry${index}`, entry: value })), evidence: evidence("food") } }, parent.kind === "existing" ? parent.base_version : undefined, "POST", submitKey.current));
      } else if (initial || record || plan?.meal) {
        const original = initial?.content.items || record?.items || plan!.meal!.items;
        const edits: unknown[] = items.map((item, index) => item.existingId ? { action: "update", item_id: item.existingId, changes: { food: proposed[index].food, quantity: proposed[index].quantity, note: item.note || null } } : { action: "add", item: proposed[index] });
        for (const old of original) if (!items.some((item) => item.existingId === old.id)) edits.push({ action: "remove", item_id: old.id });
        const patch = { temporal: entry.temporal, item_edits: edits };
        if (initial) onPreview(await dietWrite<Draft>(`/drafts/${initial.id}`, { patch, evidence: evidence("food"), user_edit: { actuality, subject, unlock_fields: unlocks } }, initial.version, "PATCH", submitKey.current));
        else if (plan?.meal) onPreview(await dietWrite<Preview>("/previews", { operation: "plan_update", arguments: { action: "update", planned_meal_id: plan.meal.id, base_version: plan.meal.version, patch, evidence: evidence("food") } }, plan.meal.version, "POST", submitKey.current));
        else onPreview(await dietWrite<Preview>("/previews", { operation: "log_correct", timezone, arguments: { action: "correct", record_id: record!.id, base_version: record!.version, patch, evidence: evidence("food") } }, record!.version, "POST", submitKey.current));
      } else onPreview(await dietWrite<Draft>("/drafts", { mode: "structured", channel: sourcePlan ? "plan" : "manual", entry, evidence: evidence("food"), ...(sourcePlan ? { source: { kind: "plan", planned_meal_id: sourcePlan.meal.id, base_version: sourcePlan.meal.version, record_again: sourcePlan.recordAgain }, record_again_authorized: sourcePlan.recordAgain } : {}) }, undefined, "POST", submitKey.current));
    } catch (caught) { setError(caught instanceof Error ? caught.message : "准备失败"); }
    finally { setBusy(false); }
  };
  return <dialog ref={dialog} className="diet-dialog diet-editor" aria-labelledby="intake-title" onCancel={(event) => { if (busy) event.preventDefault(); else onClose(); }}>
    <div className="diet-dialog-heading"><h2 id="intake-title">{plan ? plan.meal ? "编辑计划餐次" : "添加计划餐次" : sourcePlan ? sourcePlan.recordAgain ? "再记一次" : "标记已吃" : record ? "更正饮食记录" : initial ? "编辑草稿" : "记录已吃"}</h2><button type="button" className="icon-button" aria-label="关闭编辑" title="关闭编辑" disabled={busy} onClick={onClose}><X size={18} /></button></div>
    {error && <ErrorNotice message={error} />}
    {initial && ["text", "image"].includes(initial.channel) && <ParseJobStatus draft={initial} onDraft={setInitial} />}
    {!!initial?.media_ids.length && <PrivateImages ids={initial.media_ids} />}
    {initial?.channel === "image" && !initial.label_suggestion && <p className="muted">照片识别的食物、份量和营养为粗略估算，可直接修改，不需要精确称重。</p>}
    {initial && initial.version !== initialDraft?.version && <div className="notice diet-draft-update" role="status"><p>草稿已产生新版本。当前表单输入仍保留，请核对解析结果后编辑。</p><button type="button" className="button button-secondary" disabled={!initial.content.items.length} onClick={() => { loadEntry(initial.content.entry); setItems(initial.content.items.map((item) => ({ key: item.client_item_key, name: item.name_snapshot, food: item.food_ref, quantity: item.quantity, note: item.note || "", existingId: item.id }))); }}>载入解析结果</button></div>}
    {initial?.parse_suggestion && <div role="status"><p>迟到解析仅作为建议，尚未覆盖人工输入。</p><p>{initial.parse_suggestion.items.map((item) => item.food.kind === "unknown" ? item.food.name : "已匹配食物").join("、")}</p><button type="button" className="button button-secondary" onClick={() => loadEntry(initial.parse_suggestion!)}>采用建议到表单</button></div>}
    {initial?.parse_message && <p role="status">{initial.parse_message}</p>}
    {initial?.label_suggestion && onLabel && <button type="button" className="button button-secondary" disabled={busy} onClick={() => onLabel(initial)}><ScanText size={16} />核对标签资料</button>}
    {!!initial?.parse_candidates?.length && <div><ul>{initial.parse_candidates.map((entry, index) => <li key={index}>{entry.temporal.local_date || "日期待补"} · {meals[entry.temporal.meal_type]} · {entry.items.map((item) => item.food.kind === "unknown" ? item.food.name : "已匹配食物").join("、")}</li>)}</ul>{onSplit && <button type="button" className="button button-secondary" disabled={busy} onClick={() => void split()}><Split size={16} />拆分为独立草稿（{initial.parse_candidates.length}）</button>}</div>}
    <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      {plan?.parent.kind === "new" && <label className="field"><span>餐单标题</span><input value={planTitle} required maxLength={120} onChange={(event) => { setPlanTitle(event.target.value); submitKey.current = crypto.randomUUID(); }} /></label>}
      {staged.length > 0 && <ul className="diet-staged-entries">{staged.map((entry, index) => <li key={index}><span>{entry.temporal.local_date} · {meals[entry.temporal.meal_type]} · {entry.items.map((item) => item.food.kind === "unknown" ? item.food.name : "已匹配食物").join("、")}</span><button type="button" className="icon-button" title="移除此餐次" aria-label={`移除批量餐次${index + 1}`} onClick={() => { setStaged((values) => values.filter((_, offset) => offset !== index)); submitKey.current = crypto.randomUUID(); }}><Trash2 size={16} /></button></li>)}</ul>}
      <div className="diet-form-grid"><label className="field"><span>食用日期</span><input type="date" value={localDate} required onChange={(event) => { setDate(event.target.value); submitKey.current = crypto.randomUUID(); }} /></label><label className="field"><span>餐次</span><select aria-label="餐次" value={mealType} required onChange={(event) => { setMealType(event.target.value as MealType); submitKey.current = crypto.randomUUID(); }}><option value="">请选择</option>{Object.entries(meals).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label></div>
      {!plan && <div className="diet-form-grid"><label className="field"><span>食用时间（可留空）</span><input type="time" value={localTime} onChange={(event) => { setTime(event.target.value); submitKey.current = crypto.randomUUID(); }} /></label><label className="field"><span>UTC偏移分钟（夏令时歧义时必填）</span><input type="number" min={-840} max={840} step={1} value={offset} onChange={(event) => { setOffset(event.target.value); submitKey.current = crypto.randomUUID(); }} /></label></div>}
      {record && <label className="field"><span>此记录的食用时区（IANA）</span><input required maxLength={120} value={timezone} onChange={(event) => { setTimezone(event.target.value); setOffset(""); submitKey.current = crypto.randomUUID(); }} /></label>}
      {initial && <div className="diet-form-grid"><label className="field"><span>主体</span><select value={subject} onChange={(event) => { setSubject(event.target.value as Entry["subject"]); submitKey.current = crypto.randomUUID(); }}><option value="unknown">待确认</option><option value="self">本人</option><option value="third_party">其他人</option></select></label><label className="field"><span>实际发生情况</span><select value={actuality} onChange={(event) => { setActuality(event.target.value as Entry["actuality"]); submitKey.current = crypto.randomUUID(); }}><option value="unknown">待确认</option><option value="actual">已食用</option><option value="planned">计划食用</option><option value="hypothetical">假设</option></select></label></div>}
      {initial && initial.content.field_locks.length > 0 && <fieldset><legend>人工字段锁</legend>{initial.content.field_locks.map((field) => <label key={field}><input type="checkbox" checked={unlocks.includes(field)} onChange={(event) => { setUnlocks((values) => event.target.checked ? [...values, field] : values.filter((value) => value !== field)); submitKey.current = crypto.randomUUID(); }} />解锁 {field}</label>)}</fieldset>}
      {items.map((item, index) => <fieldset key={item.key} className="diet-item-fields"><legend>食物 {index + 1}</legend><div className="diet-food-row"><label className="field"><span>名称</span><input value={item.name} maxLength={120} required onChange={(event) => update(item.key, { name: event.target.value, food: { kind: "unknown", name: event.target.value } })} /></label><button type="button" className="icon-button" disabled={busy || !item.name.trim()} title="匹配食品资料" aria-label={`匹配食物${index + 1}资料`} onClick={() => void search(item)}><Search size={18} /></button><button type="button" className="icon-button" disabled={busy || items.length === 1} title="移除食物" aria-label={`移除食物${index + 1}`} onClick={() => { setItems((current) => current.filter((value) => value.key !== item.key)); submitKey.current = crypto.randomUUID(); }}><Trash2 size={18} /></button></div>
        <small className="muted">{initial?.content.items.find((value) => value.client_item_key === item.key && JSON.stringify(value.food_ref) === JSON.stringify(item.food))?.evidence_snapshot?.source_kind === "model_estimate" ? "模型估算，非实测数据" : item.food.kind === "unknown" ? "未匹配资料，营养保持未知" : "已匹配版本化资料"}</small>
        {matches?.key === item.key && <div className="diet-food-results">{matches.foods.length ? matches.foods.map((hit) => <button type="button" key={JSON.stringify(hit.food_ref)} onClick={() => { update(item.key, { name: hit.name, food: hit.food_ref, servingEvidence: hit.serving_evidence_id || undefined }); setMatches(null); }}><strong>{hit.name}</strong><small>{hit.food_state === "unknown" ? "生熟未知" : hit.food_state} · {hit.basis === "recipe_batch" ? "整批菜谱" : hit.basis === "per_100g" ? "每100克" : "每份"}</small></button>) : <p>未找到匹配资料</p>}</div>}
        <QuantityFields item={item} onChange={(quantity) => update(item.key, { quantity })} />
        <label className="field"><span>食物备注</span><input value={item.note} maxLength={500} onChange={(event) => update(item.key, { note: event.target.value })} /></label>
      </fieldset>)}
      <button type="button" className="button button-secondary" disabled={busy || items.length >= 50} onClick={() => { setItems((current) => [...current, emptyItem()]); submitKey.current = crypto.randomUUID(); }}><Plus size={16} />添加食物</button>
      <label className="field diet-note"><span>本餐备注</span><input value={note} maxLength={500} onChange={(event) => { setNote(event.target.value); submitKey.current = crypto.randomUUID(); }} /></label>
      {plan && !plan.meal && <button type="button" className="button button-secondary" disabled={busy || staged.length >= 27} onClick={(event) => {
        if (!event.currentTarget.form?.reportValidity() || !mealType) return;
        const entry: Entry = { actuality: "planned", subject: "self", temporal: { local_date: localDate, meal_type: mealType, note }, items: items.map((item) => ({ client_item_key: item.key, food: item.food.kind === "unknown" ? { kind: "unknown", name: item.name.trim() } : item.food, quantity: item.quantity, ...(item.note ? { note: item.note } : {}) })) };
        setStaged((values) => [...values, entry]); setItems([emptyItem()]); setMealType(""); setNote(""); setMatches(null); submitKey.current = crypto.randomUUID();
      }}><Plus size={16} />加入本餐并继续添加</button>}
      <div className="modal-actions"><button type="button" className="button button-secondary" disabled={busy} onClick={onClose}>暂不提交</button><button type="submit" className="button button-primary" disabled={busy}>{busy ? <LoaderCircle className="spin" size={16} /> : <Eye size={16} />}查看预览</button></div>
    </form>
  </dialog>;
}

function QuantityFields({ item, onChange }: { item: EditableItem; onChange: (quantity: Quantity) => void }) {
  const value = item.quantity;
  const setKind = (kind: string) => {
    if (kind === "mass") onChange({ kind, grams: "100", food_state: "unknown" });
    else if (kind === "mass_range") onChange({ kind, grams_lower: "50", grams_upper: "100", food_state: "unknown" });
    else if (kind === "recipe_fraction") onChange({ kind, batch_multiplier: "1", share: "0.5" });
    else if (kind === "cooked_mass") onChange({ kind, batch_multiplier: "1", consumed_grams: "100", batch_cooked_grams: "400" });
    else if (kind === "servings" && item.servingEvidence) onChange({ kind, count: "1", serving_evidence_id: item.servingEvidence });
    else onChange({ kind: "unknown", description: "未提供份量" });
  };
  const numberInput = (label: string, field: string, current: string) => <label className="field" key={field}><span>{label}</span><input inputMode="decimal" type="text" pattern="(0|[1-9][0-9]*)(\.[0-9]{1,6})?" required value={current} onChange={(event) => onChange({ ...value, [field]: event.target.value } as Quantity)} /></label>;
  return <div className="diet-form-grid"><label className="field"><span>份量口径</span><select value={value.kind} onChange={(event) => setKind(event.target.value)}><option value="unknown">未知份量</option><option value="mass">克重</option><option value="mass_range">克重区间</option>{item.food.kind === "recipe" && <><option value="recipe_fraction">整批占比</option><option value="cooked_mass">熟重分摊</option></>}{(value.kind === "servings" || item.servingEvidence) && <option value="servings">有依据的份数</option>}</select></label>
    {value.kind === "unknown" && <label className="field"><span>原始份量描述</span><input value={value.description} required maxLength={200} onChange={(event) => onChange({ ...value, description: event.target.value })} /></label>}
    {(value.kind === "mass" || value.kind === "mass_range") && <><label className="field"><span>生熟状态</span><select value={value.food_state} onChange={(event) => onChange({ ...value, food_state: event.target.value })}><option value="unknown">未知</option><option value="raw">生重</option><option value="cooked">熟重</option><option value="as_sold">销售状态</option></select></label>{value.kind === "mass" ? numberInput("克重 (g)", "grams", value.grams) : <>{numberInput("下限 (g)", "grams_lower", value.grams_lower)}{numberInput("上限 (g)", "grams_upper", value.grams_upper)}</>}</>}
    {(value.kind === "recipe_fraction" || value.kind === "cooked_mass") && numberInput("烹饪批量倍数", "batch_multiplier", value.batch_multiplier)}
    {value.kind === "recipe_fraction" && numberInput("本人占比 (0–1)", "share", value.share)}
    {value.kind === "cooked_mass" && <>{numberInput("本人熟重 (g)", "consumed_grams", value.consumed_grams)}{numberInput("整批熟重 (g)", "batch_cooked_grams", value.batch_cooked_grams)}</>}
    {value.kind === "servings" && numberInput("本人份数", "count", value.count)}
  </div>;
}
