import { useEffect, useRef, useState } from "react";
import { Check, X, LoaderCircle } from "lucide-react";
import { ErrorNotice } from "../../components/Common";
import { confirmDiet } from "./api";
import { goalKeys, meals, nutrients, rangeText } from "./types";
import type { Draft, Preview, DietLog, Target } from "./types";

export function DietPreview({ value, onClose, onSaved }: { value: Draft | Preview; onClose: () => void; onSaved: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { const element = dialog.current; element?.showModal(); return () => element?.close(); }, []);
  const draft = "preview_hash" in value ? value : null;
  const operation = draft ? "log_create" : (value as Preview).content.operation;
  const titles: Record<string, string> = { log_create: "确认饮食记录", log_correct: "确认更正", log_delete: "确认移入回收站", log_restore: "确认恢复", target_set: "确认目标设置", target_correct: "确认目标更正", target_stop: "确认停用目标", day_status_set: "确认日记录状态", plan_create: "确认餐单安排", plan_update: "确认餐次更改", plan_copy_meal: "确认复制餐次", plan_copy_week: "确认复制整周", plan_delete_meal: "确认删除餐次", plan_archive: "确认归档餐单", plan_restore: "确认恢复餐单" };
  const content = draft ? null : (value as Preview).content;
  const record = content && "after" in content ? content.after : null;
  const plan = content && "after_plan" in content ? content : null;
  const beforeRecord = content && "before" in content && content.before && "timezone" in content.before ? content.before : null;
  const submit = async (approved: boolean) => {
    setBusy(true); setError(null);
    try { await confirmDiet(value, approved); if (approved) onSaved(); else onClose(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "操作失败"); }
    finally { setBusy(false); }
  };
  return <dialog className="diet-dialog" ref={dialog} onCancel={(event) => { if (busy) event.preventDefault(); else onClose(); }} aria-labelledby="diet-preview-title">
    <div className="diet-dialog-heading"><h2 id="diet-preview-title">{titles[operation] || "确认变更"}</h2><button type="button" className="icon-button" title="关闭预览" aria-label="关闭预览" disabled={busy} onClick={onClose}><X size={18} /></button></div>
    {error && <ErrorNotice message={error} />}
    {operation === "log_correct" && beforeRecord && record && "timezone" in record && beforeRecord.timezone !== record.timezone && <p>时区：{beforeRecord.timezone} → {record.timezone}</p>}
    {plan && <><h3>{plan.after_plan.title}</h3><p>开始日期：{plan.after_plan.start_date}</p>{plan.before_plan && <p>餐单版本 {plan.before_plan.version} → {plan.after_plan.version}</p>}{plan.after_meals.map((meal) => <div className="diet-preview-meal" key={meal.id}><strong>{meal.local_date} · {meals[meal.meal_type]}{meal.deleted_at ? " · 删除餐次" : ""}</strong><ul>{meal.items.map((item) => <li key={item.id}>{item.name_snapshot} · {rangeText(item.nutrition_snapshot.calories_kcal.value, "calories_kcal")}</li>)}</ul>{meal.note && <p>{meal.note}</p>}</div>)}{operation === "plan_archive" && <p className="diet-warning">餐单将移入回收站，已有实际饮食记录不受影响，7天内可恢复。</p>}{operation === "plan_restore" && <p>恢复此餐单，不会新增实际饮食记录。</p>}</>}
    {draft ? <><p>{draft.content.entry.temporal.local_date} · {meals[draft.content.entry.temporal.meal_type]} · {draft.content.anchor.timezone}</p><ul className="diet-preview-items">{draft.content.items.map((item) => <li key={item.id}><strong>{item.name_snapshot}</strong><span>{item.quantity.kind === "unknown" ? "份量未知" : item.quantity.kind === "mass" ? `${item.quantity.grams} g` : "个人份量"}</span><span>{rangeText(item.nutrition_snapshot.calories_kcal.value, "calories_kcal")}</span><small>{item.evidence_snapshot?.calculation_note || "无营养依据"}</small></li>)}</ul></> : record && "goals" in record ? <TargetSummary target={record} /> : record && "items" in record ? <RecordSummary record={record} /> : record && "evidence" in record ? <><h3>{record.name}</h3><p>{record.brand}</p><dl className="diet-target-details">{Object.entries(nutrients).map(([key, meta]) => <div key={key}><dt>{meta.name}</dt><dd>{rangeText(record.evidence.nutrients[key as keyof typeof nutrients].value, key as keyof typeof nutrients)}</dd></div>)}</dl><p>{record.evidence.original_excerpt}</p>{record.allergens.map((fact) => <p key={fact.name}>{fact.name}：{({ known_present: "含有", known_absent: "明确不含", unknown: "未知" })[fact.state] || "未知"} · {fact.explanation} · {fact.evidence_ref}</p>)}</> : record && "status" in record ? <p>{record.local_date} · {({ open: "重新开放记录", complete: "声明当天已记录完整", no_intake: "声明当天未食用" })[record.status]}</p> : null}
    {operation === "log_delete" && <p className="diet-warning">记录立即退出实际统计，7天内可恢复。</p>}
    <p className="muted">预览有效至 {new Date(value.expires_at).toLocaleString("zh-CN")}。确认不会将估算数据变为已验证数据。</p>
    <div className="modal-actions"><button type="button" className="button button-secondary" disabled={busy} onClick={() => void submit(false)}><X size={16} />放弃预览</button><button type="button" className="button button-primary" disabled={busy || value.state !== "ready"} onClick={() => void submit(true)}>{busy ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}{operation === "log_delete" ? "确认删除" : "确认保存"}</button></div>
  </dialog>;
}
function TargetSummary({ target }: { target: Target }) { return <div><p>生效日期：{target.effective_date}</p><dl className="diet-target-details">{goalKeys.map((key) => <div key={key}><dt>{nutrients[key].name}</dt><dd>{target.goals[key] ? rangeText(target.goals[key], key) : "未设置"}</dd></div>)}</dl></div>; }
function RecordSummary({ record }: { record: DietLog }) { return <div><p>{record.temporal.local_date} · {meals[record.temporal.meal_type]} · {record.timezone}</p><ul>{record.items.map((item) => <li key={item.id}>{item.name_snapshot} · {rangeText(item.nutrition_snapshot.calories_kcal.value, "calories_kcal")}</li>)}</ul></div>; }
