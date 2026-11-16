import { useEffect, useRef, useState } from "react";
import { ArchiveRestore, Edit3, Eye, Plus, Trash2, X } from "lucide-react";
import { ErrorNotice, LoadingState } from "../../components/Common";
import { PrivateImages } from "./PrivateImages";
import { dietWrite, loadPersonalFoods } from "./api";
import { nutrients, rangeText } from "./types";
import type { LabelProposal, Nutrient, PersonalFood, Preview } from "./types";

export function PersonalFoodsView({ onPreview, revision, online }: { onPreview: (preview: Preview) => void; revision: number; online: boolean }) {
  const [foods, setFoods] = useState<PersonalFood[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [deleted, setDeleted] = useState(false);
  const [editor, setEditor] = useState<{ food?: PersonalFood } | null>(null);
  const [busy, setBusy] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null);
  useEffect(() => {
    let active = true; setLoading(true);
    setError(null);
    loadPersonalFoods(`/personal-foods?include_deleted=${deleted}`).then((page) => { if (active) { setFoods(page.items); setCursor(page.next_cursor); } }).catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : "读取失败"); }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [deleted, revision, online]);
  const more = async () => {
    if (!cursor) return;
    setBusy(true); setError(null);
    try { const page = await loadPersonalFoods(`/personal-foods?include_deleted=${deleted}&cursor=${encodeURIComponent(cursor)}`); setFoods((values) => [...values, ...page.items]); setCursor(page.next_cursor); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "翻页失败"); }
    finally { setBusy(false); }
  };
  const change = async (food: PersonalFood, action: "delete" | "restore") => {
    setBusy(true); setError(null);
    try { onPreview(await dietWrite<Preview>("/previews", { operation: `personal_food_${action}`, arguments: { action, food_id: food.id, base_version: food.version } }, food.version)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "操作失败"); }
    finally { setBusy(false); }
  };
  return <section className="diet-personal-foods"><div className="diet-section-heading"><h2>个人食物</h2><div className="diet-event-actions"><label><input type="checkbox" checked={deleted} onChange={(event) => setDeleted(event.target.checked)} />显示已删除</label><button className="button button-primary" disabled={!online} onClick={() => setEditor({})}><Plus size={16} />添加食物</button></div></div>{error && <ErrorNotice message={error} />}{loading ? <LoadingState label="读取食物资料" /> : foods.length ? foods.map((food) => <article className="diet-food-list-item" key={food.id}><div><strong>{food.name}</strong><small>{food.brand || "未填品牌"} · {food.evidence.basis_quantity === "per_100g" ? "每100克" : "每份"}{food.deleted_at ? " · 已删除" : ""}</small><span>{rangeText(food.evidence.nutrients.calories_kcal.value, "calories_kcal")}</span><small>{food.allergens.length ? food.allergens.map((fact) => `${fact.name}：${({ known_present: "含有", known_absent: "不含", unknown: "未知" })[fact.state] || "未知"}`).join("；") : "过敏原资料未知"}</small></div><div className="diet-event-actions">{food.deleted_at ? <button className="icon-button" title="恢复食物" aria-label="恢复食物" disabled={busy || !online} onClick={() => void change(food, "restore")}><ArchiveRestore size={17} /></button> : <><button className="icon-button" title="修改食物" aria-label="修改食物" disabled={!online} onClick={() => setEditor({ food })}><Edit3 size={17} /></button><button className="icon-button" title="删除食物" aria-label="删除食物" disabled={busy || !online} onClick={() => void change(food, "delete")}><Trash2 size={17} /></button></>}</div></article>) : <div className="diet-empty"><h3>暂无个人食物资料</h3></div>}{cursor && <button className="button button-secondary" disabled={busy} onClick={() => void more()}>更多食物</button>}{editor && <FoodEditor food={editor.food} onClose={() => setEditor(null)} onPreview={(value) => { setEditor(null); onPreview(value); }} />}</section>;
}

export function FoodEditor({ food, label, onClose, onPreview }: { food?: PersonalFood; label?: LabelProposal; onClose: () => void; onPreview: (preview: Preview) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState(food?.name || label?.name || "");
  const [brand, setBrand] = useState(food?.brand || label?.brand || "");
  const [basis, setBasis] = useState(food?.evidence.basis_quantity || (label ? ["per_100g", "per_serving"].includes(label.basis) ? label.basis : "" : "per_100g"));
  const [state, setState] = useState(food?.evidence.food_state || "unknown");
  const [source, setSource] = useState(food?.evidence.source_kind === "label" || label ? "manual_label" : "user_report");
  const [note, setNote] = useState(food?.evidence.original_excerpt || label?.source_note || "");
  const [servingGrams, setServingGrams] = useState(food?.evidence.serving_grams || label?.serving_grams || "");
  const [reviewed, setReviewed] = useState(false);
  const [allergens, setAllergens] = useState<PersonalFood["allergens"]>(food?.allergens || []);
  const [values, setValues] = useState<Record<Nutrient, { known: boolean; lower: string; upper: string }>>(() => Object.fromEntries(Object.keys(nutrients).map((key) => { const value = food?.evidence.nutrients[key as Nutrient].value || label?.nutrients[key as Nutrient]; return [key, { known: !!value, lower: value?.lower || "", upper: value?.upper || "" }]; })) as Record<Nutrient, { known: boolean; lower: string; upper: string }>);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const key = useRef(crypto.randomUUID());
  useEffect(() => { const element = dialog.current; element?.showModal(); return () => element?.close(); }, []);
  const submit = async () => {
    setBusy(true); setError(null);
    try {
      onPreview(await dietWrite<Preview>("/previews", { operation: food ? "personal_food_update" : "personal_food_create", arguments: { action: food ? "update" : "create", ...(food ? { food_id: food.id, base_version: food.version } : {}), ...(label ? { label_draft_id: label.label_draft_id, label_draft_version: label.label_draft_version, label_reviewed: reviewed } : {}), data: { name, brand: brand || null, food_state: state, basis, source, source_note: note, serving_grams: servingGrams || null, ...(label ? { media_id: label.media_id, media_ids: label.media_ids } : food?.evidence.media_id ? { media_id: food.evidence.media_id, media_ids: food.evidence.media_ids || [] } : {}), nutrients: Object.fromEntries(Object.entries(values).map(([field, value]) => [field, value.known ? { lower: value.lower, upper: value.upper || value.lower } : null])), allergens } } }, food?.version, "POST", key.current));
    } catch (caught) { setError(caught instanceof Error ? caught.message : "准备失败"); }
    finally { setBusy(false); }
  };
  const updateAllergen = (index: number, changes: Partial<PersonalFood["allergens"][number]>) => {
    setAllergens((current) => current.map((fact, offset) => offset === index ? { ...fact, ...changes } : fact));
    key.current = crypto.randomUUID();
  };
  return <dialog ref={dialog} className="diet-dialog" aria-labelledby="personal-food-title" onCancel={(event) => { if (busy) event.preventDefault(); else onClose(); }}>
    <div className="diet-dialog-heading"><h2 id="personal-food-title">{food ? "修改食物资料" : "添加个人食物"}</h2><button className="icon-button" title="关闭食物编辑" aria-label="关闭食物编辑" disabled={busy} onClick={onClose}><X size={18} /></button></div>
    {error && <ErrorNotice message={error} />}
    {label && <><PrivateImages ids={label.media_ids} /><p className="diet-warning">模型提议尚未经本人核对；未知值保持未知，标签口径不等于个人摄入。</p></>}
    <form onSubmit={(event) => { event.preventDefault(); void submit(); }} onChange={() => { key.current = crypto.randomUUID(); }}>
      <div className="diet-form-grid">
        <label className="field"><span>食物名称</span><input required maxLength={120} value={name} onChange={(event) => setName(event.target.value)} /></label>
        <label className="field"><span>品牌</span><input maxLength={120} value={brand} onChange={(event) => setBrand(event.target.value)} /></label>
        <label className="field"><span>资料口径</span><select aria-label="资料口径" required value={basis} onChange={(event) => setBasis(event.target.value)}><option value="">请核对口径（不将毫升当克）</option><option value="per_100g">每100克</option><option value="per_serving">每份</option></select></label>
        <label className="field"><span>食品状态</span><select aria-label="食品状态" value={state} onChange={(event) => setState(event.target.value)}><option value="unknown">未知</option><option value="as_sold">销售状态</option><option value="raw">生</option><option value="cooked">熟</option></select></label>
        <label className="field"><span>每份克重（可留空）</span><input inputMode="decimal" pattern="(0|[1-9][0-9]*)(\.[0-9]{1,6})?" value={servingGrams} onChange={(event) => setServingGrams(event.target.value)} /></label>
        <label className="field"><span>资料来源</span><select aria-label="资料来源" value={source} onChange={(event) => setSource(event.target.value)}><option value="user_report">本人提供</option><option value="manual_label">手填包装标签</option></select></label>
      </div>
      <label className="field"><span>来源说明或标签原文</span><textarea required value={note} maxLength={2000} onChange={(event) => setNote(event.target.value)} /></label>
      <div className="diet-goal-fields">{(Object.keys(nutrients) as Nutrient[]).map((field) => <fieldset key={field}><legend><label><input type="checkbox" checked={values[field].known} onChange={(event) => setValues((current) => ({ ...current, [field]: { ...current[field], known: event.target.checked } }))} />{nutrients[field].name} ({nutrients[field].unit})</label></legend><div className="diet-form-grid">{(["lower", "upper"] as const).map((edge) => <label className="field" key={edge}><span>{edge === "lower" ? "下限或固定值" : "上限"}</span><input inputMode="decimal" value={values[field][edge]} disabled={!values[field].known} required={values[field].known && edge === "lower"} pattern="(0|[1-9][0-9]*)(\.[0-9]{1,6})?" onChange={(event) => setValues((current) => ({ ...current, [field]: { ...current[field], [edge]: event.target.value } }))} /></label>)}</div></fieldset>)}</div>
      <div className="diet-section-heading"><h3>过敏原事实</h3><button type="button" className="button button-secondary" onClick={() => { setAllergens((current) => [...current, { name: "", state: "unknown", evidence_ref: null, explanation: "" }]); key.current = crypto.randomUUID(); }}><Plus size={16} />添加过敏原</button></div>
      {allergens.map((fact, index) => <fieldset className="diet-item-fields" key={index}><legend>过敏原 {index + 1}</legend><div className="diet-form-grid"><label className="field"><span>过敏原名称</span><input required maxLength={120} value={fact.name} onChange={(event) => updateAllergen(index, { name: event.target.value })} /></label><label className="field"><span>食品过敏原状态</span><select value={fact.state} onChange={(event) => updateAllergen(index, { state: event.target.value })}><option value="unknown">未知</option><option value="known_present">含有</option><option value="known_absent">明确不含</option></select></label><label className="field"><span>过敏原依据引用</span><input required={fact.state !== "unknown"} maxLength={120} value={fact.evidence_ref || ""} onChange={(event) => updateAllergen(index, { evidence_ref: event.target.value || null })} /></label><label className="field"><span>过敏原依据说明</span><input maxLength={500} value={fact.explanation} onChange={(event) => updateAllergen(index, { explanation: event.target.value })} /></label></div><button type="button" className="icon-button" title="移除过敏原" aria-label={`移除过敏原${index + 1}`} onClick={() => { setAllergens((current) => current.filter((_, offset) => offset !== index)); key.current = crypto.randomUUID(); }}><Trash2 size={17} /></button></fieldset>)}
      <p className="muted">未填写的营养与过敏原事实保持未知。</p>
      {label && <label><input type="checkbox" required checked={reviewed} onChange={(event) => setReviewed(event.target.checked)} />已逐项核对原图、单位和标签口径</label>}
      <div className="modal-actions"><button type="button" className="button button-secondary" disabled={busy} onClick={onClose}>取消</button><button type="submit" className="button button-primary" disabled={busy || (!!label && !reviewed)}><Eye size={16} />查看食物预览</button></div>
    </form>
  </dialog>;
}
