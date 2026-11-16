import { useEffect, useState } from "react";
import { ErrorNotice, LoadingState } from "../../components/Common";
import { dietGet } from "./api";
import { goalKeys, meals, nutrients, quantityText, rangeText } from "./types";
import type { DietItem, Draft, Preview } from "./types";

export function ChatDietPreview({ id, version, hash, onVerified }: { id: string; version: number; hash: string; onVerified: (hash: string | null) => void }) {
  const [value, setValue] = useState<Draft | Preview | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    setValue(null); setError(null);
    onVerified(null);
    void dietGet<Draft | Preview>(`/previews/${encodeURIComponent(id)}`).then((preview) => {
      if (preview.version !== version || ("preview_hash" in preview ? preview.preview_hash : preview.content_hash) !== hash || preview.state !== "ready") throw new Error("预览已修改或失效，请重新读取当前会话。");
      if (active) { setValue(preview); onVerified(hash); }
    }).catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : "预览读取失败"); });
    return () => { active = false; onVerified(null); };
  }, [id, version, hash, onVerified]);
  if (error) return <ErrorNotice message={error} />;
  if (!value) return <LoadingState label="正在读取饮食预览" />;
  const content = value.content;
  const draft = "preview_hash" in value ? value : null;
  return <div className="confirmation-meals">
    {draft ? <><p>{draft.content.entry.temporal.local_date} · {meals[draft.content.entry.temporal.meal_type]} · {draft.content.anchor.timezone}</p><Items items={draft.content.items} /></> : null}
    {"after_plan" in content ? <><h3>{content.after_plan.title}</h3>{content.after_meals.map((meal) => <div key={meal.id}><p>{meal.local_date} · {meals[meal.meal_type]}{meal.deleted_at ? " · 删除" : ""}</p><Items items={meal.items} /></div>)}</> : null}
    {"after" in content && "goals" in content.after ? <><p>目标生效日期：{content.after.effective_date}</p><dl>{goalKeys.map((key) => <div key={key}><dt>{nutrients[key].name}</dt><dd>{rangeText(content.after && "goals" in content.after ? content.after.goals[key] : null, key)}</dd></div>)}</dl></> : null}
    {"after" in content && "items" in content.after ? <><p>{content.after.temporal.local_date} · {meals[content.after.temporal.meal_type]}</p><Items items={content.after.items} /></> : null}
    <p>有效至 {new Date(value.expires_at).toLocaleString("zh-CN")}。未知数据不会按零计算。</p>
  </div>;
}

function Items({ items }: { items: DietItem[] }) {
  return <ul>{items.map((item) => <li key={item.id}><strong>{item.name_snapshot}</strong><p>{quantityText(item.quantity)} · {rangeText(item.nutrition_snapshot.calories_kcal.value, "calories_kcal")}</p>{item.note && <p>{item.note}</p>}<small>{item.evidence_snapshot?.source_ref || "营养资料未知"}</small></li>)}</ul>;
}
