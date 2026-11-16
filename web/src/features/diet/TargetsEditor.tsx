import { useEffect, useRef, useState } from "react";
import { Eye, LoaderCircle, X } from "lucide-react";
import { ErrorNotice } from "../../components/Common";
import { dietWrite } from "./api";
import { evidence, goalKeys, nutrients } from "./types";
import type { GoalNutrient, Preview, Target } from "./types";

export function TargetsEditor({ date, target, onClose, onPreview }: { date: string; target?: Target; onClose: () => void; onPreview: (value: Preview) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [effectiveDate, setDate] = useState(target?.effective_date || date);
  const [goals, setGoals] = useState<Record<GoalNutrient, { enabled: boolean; lower: string; upper: string }>>(() => Object.fromEntries(goalKeys.map((key) => [key, { enabled: !!target?.goals[key], lower: target?.goals[key]?.lower || "", upper: target?.goals[key]?.upper || "" }])) as Record<GoalNutrient, { enabled: boolean; lower: string; upper: string }>);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestKey = useRef(crypto.randomUUID());
  useEffect(() => { const element = dialog.current; element?.showModal(); return () => element?.close(); }, []);
  const submit = async () => {
    setBusy(true); setError(null);
    try {
      const values = Object.fromEntries(goalKeys.map((key) => [key, goals[key].enabled ? { lower: goals[key].lower, upper: goals[key].upper || goals[key].lower } : null]));
      onPreview(await dietWrite<Preview>("/previews", { operation: target ? "target_correct" : "target_set", arguments: { action: target ? "correct" : "set", ...(target ? { target_id: target.id, base_version: target.version } : {}), effective_date: effectiveDate, goals: values, evidence: evidence("goal") } }, target?.version, "POST", requestKey.current));
    } catch (caught) { setError(caught instanceof Error ? caught.message : "目标预览失败"); }
    finally { setBusy(false); }
  };
  return <dialog className="diet-dialog" ref={dialog} aria-labelledby="target-title" onCancel={(event) => { if (busy) event.preventDefault(); else onClose(); }}>
    <div className="diet-dialog-heading"><h2 id="target-title">{target ? "更正历史目标" : "设置营养目标"}</h2><button className="icon-button" type="button" title="关闭目标设置" aria-label="关闭目标设置" disabled={busy} onClick={onClose}><X size={18} /></button></div>
    {error && <ErrorNotice message={error} />}
    <form onSubmit={(event) => { event.preventDefault(); void submit(); }} onChange={() => { requestKey.current = crypto.randomUUID(); }}>
      <label className="field"><span>生效日期</span><input type="date" value={effectiveDate} required onChange={(event) => setDate(event.target.value)} /></label>
      <div className="diet-goal-fields">{goalKeys.map((key) => <fieldset key={key}><legend><label><input type="checkbox" checked={goals[key].enabled} onChange={(event) => setGoals((current) => ({ ...current, [key]: { ...current[key], enabled: event.target.checked } }))} />{nutrients[key].name} ({nutrients[key].unit})</label></legend><div className="diet-form-grid">{(["lower", "upper"] as const).map((edge) => <label className="field" key={edge}><span>{edge === "lower" ? "下限或固定值" : "上限"}</span><input inputMode="decimal" type="text" pattern="(0|[1-9][0-9]*)(\.[0-9]{1,6})?" value={goals[key][edge]} required={edge === "lower" && goals[key].enabled} disabled={!goals[key].enabled} onChange={(event) => setGoals((current) => ({ ...current, [key]: { ...current[key], [edge]: event.target.value } }))} /></label>)}</div></fieldset>)}</div>
      <div className="modal-actions"><button className="button button-secondary" type="button" disabled={busy} onClick={onClose}>取消</button><button className="button button-primary" type="submit" disabled={busy}>{busy ? <LoaderCircle className="spin" size={16} /> : <Eye size={16} />}查看目标预览</button></div>
    </form>
  </dialog>;
}
