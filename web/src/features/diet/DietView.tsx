import { useCallback, useEffect, useRef, useState } from "react";
import { ArchiveRestore, ChevronDown, ChevronLeft, ChevronRight, ClipboardList, Coffee, Database, Edit3, Globe2, Moon, Plus, RefreshCw, Salad, ScanLine, Settings2, Sun, Target as TargetIcon, Trash2, Utensils, X } from "lucide-react";
import { ErrorNotice, LoadingState, PageHeader } from "../../components/Common";
import type { ConnectionState } from "../../types";
import { dietGet, dietWrite, loadDrafts, loadLogs } from "./api";
import { DietPreview } from "./DietPreview";
import { IntakeEditor } from "./IntakeEditor";
import { TargetsEditor } from "./TargetsEditor";
import { FoodEditor, PersonalFoodsView } from "./PersonalFoodsView";
import { DietSettings } from "./DietSettings";
import { evidence, goalKeys, meals, nutrients, quantityText, rangeText } from "./types";
import { ParseIntake } from "./ParseIntake";
import { DataManagement } from "./DataManagement";
import { DeviationView } from "./DeviationView";
import { NutritionOverview } from "./NutritionOverview";
import type { DayAnalysis, DayDeclaration, DietContext, DietItem, DietLog, Draft, LabelProposal, MealType, Page, Preview, Target, WeekAnalysis } from "./types";
import "./diet.css";

function dateShift(date: string, days: number) { const parsed = new Date(`${date}T12:00:00Z`); parsed.setUTCDate(parsed.getUTCDate() + days); return parsed.toISOString().slice(0, 10); }
function weekStart(date: string) { const weekday = new Date(`${date}T12:00:00Z`).getUTCDay(); return dateShift(date, -((weekday + 6) % 7)); }
const sections = [
  { key: "records", label: "记录与分析", icon: Utensils },
  { key: "targets", label: "目标管理", icon: TargetIcon },
  { key: "foods", label: "个人食物", icon: Salad },
  { key: "data", label: "数据管理", icon: Database },
] as const;
type DietSection = typeof sections[number]["key"];
const mealIcons = { breakfast: Coffee, lunch: Sun, dinner: Moon, snack: Salad };

export function DietView({ connection }: { connection: ConnectionState }) {
  const [date, setDate] = useState(new URLSearchParams(location.search).get("diet_date") || "");
  const [granularity, setGranularity] = useState<"day" | "week">(new URLSearchParams(location.search).get("diet_granularity") === "week" ? "week" : "day");
  const [context, setContext] = useState<DietContext | null>(null);
  const [records, setRecords] = useState<DietLog[]>([]);
  const [analysis, setAnalysis] = useState<DayAnalysis | WeekAnalysis | null>(null);
  const [targets, setTargets] = useState<{ target: Target; end_date_exclusive: string | null }[]>([]);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [draftCursor, setDraftCursor] = useState<string | null>(null);
  const [dayDeclaration, setDayDeclaration] = useState<DayDeclaration | null>(null);
  const [section, setSection] = useState<DietSection>(() => { const value = new URLSearchParams(location.search).get("diet_section"); return value === "foods" || value === "data" || value === "targets" ? value : "records"; });
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [includeDeleted, setDeleted] = useState(false);
  const [nextCursor, setCursor] = useState<string | null>(null);
  const [editor, setEditor] = useState<{ draft?: Draft; record?: DietLog } | null>(null);
  const [preview, setPreview] = useState<Draft | Preview | null>(null);
  const [parserOpen, setParserOpen] = useState(false);
  const [labelProposal, setLabelProposal] = useState<LabelProposal | null>(null);
  const [targetEditor, setTargetEditor] = useState<{ target?: Target } | null>(null);
  const [evidenceItem, setEvidenceItem] = useState<DietItem | null>(null);
  const refreshGeneration = useRef(0);
  const start = date ? granularity === "week" ? weekStart(date) : date : "";
  const end = start ? granularity === "week" ? dateShift(start, 6) : start : "";
  const refresh = useCallback(async () => {
    const generation = ++refreshGeneration.current;
    setLoading(true); setError(null);
    try {
      const ctx = await dietGet<DietContext>("/context");
      if (generation !== refreshGeneration.current) return;
      setContext(ctx);
      if (!date) { setDate(ctx.anchor.anchor_local_date); return; }
      const query = `start_date=${start}&end_date=${end}`;
      const [logs, stats, goalTimeline, savedDrafts, declaration] = await Promise.all([
        loadLogs(`/logs?${query}&include_deleted=${includeDeleted}`),
        dietGet<Page<DayAnalysis | WeekAnalysis>>(`/summary?${query}&granularity=${granularity}`),
        dietGet<Page<{ target: Target; end_date_exclusive: string | null }>>(`/targets?${query}`),
        loadDrafts("/drafts"),
        dietGet<DayDeclaration>(`/days/${date}`),
      ]);
      if (generation !== refreshGeneration.current) return;
      setRecords(logs.items); setCursor(logs.next_cursor); setAnalysis(stats.items[0] || null);
      setTargets(goalTimeline.items); setDrafts(savedDrafts.items); setDraftCursor(savedDrafts.next_cursor); setDayDeclaration(declaration);
    } catch (caught) { if (generation === refreshGeneration.current) { setError(caught instanceof Error ? caught.message : "读取失败"); setRecords([]); setAnalysis(null); } }
    finally { if (generation === refreshGeneration.current) setLoading(false); }
  }, [date, start, end, granularity, includeDeleted]);
  useEffect(() => { if (connection !== "checking") void refresh(); return () => { refreshGeneration.current += 1; }; }, [connection, refresh]);
  useEffect(() => {
    const url = new URL(location.href);
    if (date) url.searchParams.set("diet_date", date);
    url.searchParams.set("diet_granularity", granularity);
    url.searchParams.set("diet_section", section);
    history.replaceState({}, "", url);
  }, [date, granularity, section]);
  useEffect(() => {
    const draftId = new URLSearchParams(location.search).get("diet_draft");
    const previewId = new URLSearchParams(location.search).get("diet_preview");
    if ((!draftId && !previewId) || connection !== "online") return;
    let active = true;
    dietGet<Draft | Preview>(previewId ? `/previews/${encodeURIComponent(previewId)}` : `/drafts/${encodeURIComponent(draftId!)}`).then((value) => {
      if (!active) return;
      if (["ready", "needs_input"].includes(value.state)) {
        if (previewId) setPreview(value); else if ("preview_hash" in value) setEditor({ draft: value });
      } else setSuccess(value.state === "committed" ? "这项变更已保存，没有重复提交。" : "此预览已失效或放弃。");
    }).catch((caught) => { if (active) setError(caught instanceof Error ? caught.message : "草稿读取失败"); });
    return () => { active = false; };
  }, [connection]);
  const openEditor = (value: { draft?: Draft; record?: DietLog }) => {
    setEditor(value); const url = new URL(location.href); if (value.draft) url.searchParams.set("diet_draft", value.draft.id); else url.searchParams.delete("diet_draft"); history.replaceState({}, "", url);
  };
  const closeEditor = () => { setEditor(null); const url = new URL(location.href); url.searchParams.delete("diet_draft"); history.replaceState({}, "", url); };
  const showPreview = (value: Draft | Preview) => { closeEditor(); setTargetEditor(null); setPreview(value); const url = new URL(location.href); url.searchParams.set("diet_preview", value.id); history.replaceState({}, "", url); };
  const closePreview = () => { setPreview(null); const url = new URL(location.href); url.searchParams.delete("diet_preview"); history.replaceState({}, "", url); setRevision((value) => value + 1); void refresh(); };
  const declareDay = async (status: DayDeclaration["status"]) => {
    if (!dayDeclaration) return;
    setBusy(true); setError(null);
    try { showPreview(await dietWrite<Preview>("/previews", { operation: "day_status_set", arguments: { local_date: date, status, base_version: dayDeclaration.version } }, dayDeclaration.version)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "准备失败"); }
    finally { setBusy(false); }
  };
  const moreDrafts = async () => {
    if (!draftCursor) return;
    setBusy(true);
    try { const page = await loadDrafts(`/drafts?cursor=${encodeURIComponent(draftCursor)}`); setDrafts((values) => [...values, ...page.items]); setDraftCursor(page.next_cursor); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "草稿翻页失败"); }
    finally { setBusy(false); }
  };
  const recordChange = async (record: DietLog, action: "delete" | "restore") => {
    setBusy(true); setError(null);
    try { showPreview(await dietWrite<Preview>("/previews", { operation: `log_${action}`, arguments: { action, record_id: record.id, base_version: record.version, evidence: evidence() } }, record.version)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "准备失败"); }
    finally { setBusy(false); }
  };
  const more = async () => {
    if (!nextCursor) return;
    setBusy(true);
    try { const page = await loadLogs(`/logs?start_date=${start}&end_date=${end}&include_deleted=${includeDeleted}&cursor=${encodeURIComponent(nextCursor)}`); setRecords((current) => [...current, ...page.items]); setCursor(page.next_cursor); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "翻页失败"); }
    finally { setBusy(false); }
  };
  const stopTarget = async (target: Target) => {
    setBusy(true); setError(null);
    try { showPreview(await dietWrite<Preview>("/previews", { operation: "target_stop", arguments: { action: "stop", target_id: target.id, base_version: target.version, effective_date: date, evidence: evidence("goal") } }, target.version)); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "准备失败"); }
    finally { setBusy(false); }
  };
  const renderRecord = (record: DietLog) => (
    <article key={record.id} className={`diet-event${record.deleted_at ? " is-deleted" : ""}`}>
      <div className="diet-event-heading">
        <div><span>{record.temporal.local_time?.slice(0, 5) || "未填写时间"}</span>{record.deleted_at && <span className="diet-deleted-label">已删除</span>}</div>
        <div className="diet-event-actions">
          {record.deleted_at ? <button className="icon-button" title="恢复记录" aria-label="恢复记录" disabled={busy || connection !== "online"} onClick={() => void recordChange(record, "restore")}><ArchiveRestore size={16} /></button> : <>
            <button className="icon-button" title="更正记录" aria-label="更正记录" disabled={busy || connection !== "online"} onClick={() => openEditor({ record })}><Edit3 size={16} /></button>
            <button className="icon-button" title="删除记录" aria-label="删除记录" disabled={busy || connection !== "online"} onClick={() => void recordChange(record, "delete")}><Trash2 size={16} /></button>
          </>}
        </div>
      </div>
      {record.items.map((item) => <div className="diet-event-food" key={item.id}>
        <div><strong>{item.name_snapshot}</strong><span>{quantityText(item.quantity)}{item.evidence_snapshot?.source_kind === "model_estimate" ? " · 模型估算" : ""}</span></div>
        <span>{rangeText(item.nutrition_snapshot.calories_kcal.value, "calories_kcal")}</span>
        <button className="button button-ghost" onClick={() => setEvidenceItem(item)}>依据</button>
      </div>)}
      {record.temporal.note && <p className="diet-record-note">{record.temporal.note}</p>}
    </article>
  );
  const recordDays = granularity === "day" ? [date] : [...new Set(records.map((record) => record.temporal.local_date))].sort();
  return <div className="page-container diet-view">
    <PageHeader title="饮食记录" description="" eyebrow="" action={<div className="page-action-group">
      <button className="icon-button diet-refresh" title="刷新" aria-label="刷新" disabled={loading} onClick={() => void refresh()}><RefreshCw size={17} /></button>
      {section === "records" && <>
        <button className="button button-secondary" disabled={!context?.capabilities.text || connection !== "online"} onClick={() => setParserOpen(true)}><ScanLine size={17} />文字与照片记餐</button>
        <button className="button button-primary" disabled={!context || connection !== "online"} onClick={() => openEditor({})}><Plus size={17} />记录已吃</button>
      </>}
      {section === "targets" && <button className="button button-primary diet-target-create" disabled={!context || connection !== "online"} onClick={() => setTargetEditor({})}><Plus size={17} />营养目标</button>}
    </div>} />
    <nav className="diet-section-tabs" aria-label="饮食视图">{sections.map(({ key, label, icon: Icon }) => (
      <button key={key} aria-pressed={section === key} onClick={() => setSection(key)}><Icon size={17} aria-hidden="true" />{label}</button>
    ))}</nav>
    {error && <ErrorNotice message={error} />}{success && <p className="notice notice-success" role="status">{success}</p>}
    {section !== "foods" && <div className="diet-toolbar">
      <div className="diet-date">
        <button className="icon-button" title={granularity === "week" ? "上一周" : "前一天"} aria-label={granularity === "week" ? "上一周" : "前一天"} disabled={!date} onClick={() => setDate(dateShift(date, granularity === "week" ? -7 : -1))}><ChevronLeft size={18} /></button>
        <label><span className="sr-only">查询日期</span><input type="date" value={date} onChange={(event) => { if (event.target.value) setDate(event.target.value); }} required /></label>
        <button className="icon-button" title={granularity === "week" ? "下一周" : "后一天"} aria-label={granularity === "week" ? "下一周" : "后一天"} disabled={!date} onClick={() => setDate(dateShift(date, granularity === "week" ? 7 : 1))}><ChevronRight size={18} /></button>
      </div>
      <button className="button button-ghost diet-today" disabled={!context || date === context.anchor.anchor_local_date} onClick={() => context && setDate(context.anchor.anchor_local_date)}>今天</button>
      <div className="diet-segment" aria-label="分析粒度">{(["day", "week"] as const).map((mode) => <button key={mode} aria-pressed={granularity === mode} onClick={() => setGranularity(mode)}>{mode === "day" ? "每日" : "每周"}</button>)}</div>
      {context && <details className="diet-timezone-menu diet-disclosure">
        <summary><Globe2 size={15} aria-hidden="true" /><span>{context.timezone}</span><ChevronDown size={14} aria-hidden="true" /></summary>
        <div><DietSettings context={context} online={connection === "online"} onSaved={() => void refresh()} /></div>
      </details>}
    </div>}
    {section === "data" ? start && end && <DataManagement start={start} end={end} online={connection === "online"} onChanged={() => void refresh()} />
      : section === "foods" ? <PersonalFoodsView revision={revision} online={connection === "online"} onPreview={showPreview} />
      : loading ? <LoadingState label="正在读取饮食记录" />
      : section === "targets" ? <section className="diet-targets">
        <div className="diet-section-heading"><div><h2>目标时间线</h2><p className="muted">{start} 至 {end}</p></div></div>
        {targets.length ? targets.map(({ target, end_date_exclusive }) => <div className="diet-target-row" key={target.id}><div><strong>{target.effective_date}</strong><small>{end_date_exclusive ? `至 ${end_date_exclusive} 前一日` : "持续生效"}</small></div><div>{goalKeys.map((key) => <span key={key}>{nutrients[key].name} {target.goals[key] ? rangeText(target.goals[key], key) : "未设置"}</span>)}</div><div className="diet-event-actions"><button className="icon-button" aria-label="更正目标" title="更正目标" disabled={busy || connection !== "online"} onClick={() => setTargetEditor({ target })}><Edit3 size={17} /></button><button className="button button-secondary" disabled={busy || connection !== "online"} onClick={() => void stopTarget(target)}>从所选日期停用</button></div></div>)
          : <div className="diet-empty"><TargetIcon size={28} /><h3>所选日期没有营养目标</h3></div>}
      </section>
      : <>
        {(drafts.length > 0 || draftCursor) && <details className="diet-drafts diet-disclosure">
          <summary><ClipboardList size={18} aria-hidden="true" /><strong>待确认草稿 {drafts.length}{draftCursor ? "+" : ""}</strong><span className="diet-draft-hint">尚未入账</span><ChevronDown size={16} aria-hidden="true" /></summary>
          <section className="diet-draft-strip" aria-label="草稿箱">
            {drafts.map((draft) => <button className="button button-secondary" key={draft.id} onClick={() => openEditor({ draft })}><span>{draft.content.items[0]?.name_snapshot || "待补食物"}</span><small>{draft.content.entry.temporal.local_date || "日期待补"} · {meals[draft.content.entry.temporal.meal_type] || "餐次待补"}</small><ChevronRight size={16} aria-hidden="true" /></button>)}
          </section>
          {draftCursor && <button className="button button-ghost" disabled={busy} onClick={() => void moreDrafts()}>更多草稿</button>}
        </details>}
        <div className="diet-diary-layout">
          <section className="diet-records" aria-label="实际饮食">
            <div className="diet-section-heading"><div><h2>{granularity === "week" ? "本周饮食" : "实际饮食"}{includeDeleted ? "（含已删除）" : ""}</h2><p className="muted">{granularity === "week" ? `${start} 至 ${end}` : date}{records.length > 0 ? ` · 已加载 ${records.length} 条${nextCursor ? "，还有更多" : ""}` : ""}</p></div><label><input type="checkbox" checked={includeDeleted} onChange={(event) => setDeleted(event.target.checked)} />显示已删除记录</label></div>
            {granularity === "day" && dayDeclaration && <div className="diet-day-status">
              <span><i className={`diet-status-dot status-${dayDeclaration.status}`} aria-hidden="true" />{({ open: "尚未声明完整", complete: "已声明完整", no_intake: "已声明未食用" })[dayDeclaration.status]}</span>
              <details className="diet-disclosure diet-status-menu"><summary><Settings2 size={15} aria-hidden="true" />记录状态<ChevronDown size={14} aria-hidden="true" /></summary><div className="diet-event-actions">{(["open", "complete", "no_intake"] as const).map((status) => <button key={status} className="button button-secondary" disabled={busy || connection !== "online" || dayDeclaration.status === status || (status !== "open" && date > (context?.anchor.anchor_local_date || ""))} onClick={() => void declareDay(status)}>{({ open: "重新开放", complete: "声明已记完整", no_intake: "声明当天未食用" })[status]}</button>)}</div></details>
            </div>}
            {records.length === 0 ? <div className="diet-empty"><Utensils size={30} /><h3>尚未记录</h3><p>这不代表摄入为零。</p><button className="button button-secondary" disabled={connection !== "online"} onClick={() => openEditor({})}><Plus size={16} />添加记录</button></div>
              : <div className="diet-timeline">{recordDays.map((day) => <div className="diet-record-day" key={day}>
                {granularity === "week" && <h3 className="diet-record-date">{day}</h3>}
                {(Object.keys(meals) as MealType[]).map((mealType) => {
                  const entries = records.filter((record) => record.temporal.local_date === day && record.temporal.meal_type === mealType);
                  if (!entries.length) return null;
                  const Icon = mealIcons[mealType];
                  return <section className={`diet-meal meal-${mealType}`} key={mealType} aria-label={`${day} ${meals[mealType]}`}>
                    <div className="diet-meal-heading"><Icon size={19} aria-hidden="true" /><h3>{meals[mealType]}</h3><span>{entries.length} 条{nextCursor ? "已加载记录" : "记录"}</span></div>
                    {entries.map(renderRecord)}
                  </section>;
                })}
              </div>)}</div>}
            {nextCursor && <button className="button button-secondary diet-load-more" disabled={busy} onClick={() => void more()}>加载更多</button>}
          </section>
          {analysis && <NutritionOverview analysis={analysis} onEditTarget={() => setTargetEditor({})} />}
        </div>
        {start && end && <details className="diet-deviation-disclosure diet-disclosure"><summary><TargetIcon size={17} aria-hidden="true" />计划与实际偏差<ChevronDown size={16} aria-hidden="true" /></summary><DeviationView start={start} end={end} revision={revision} /></details>}
      </>}
    {editor && <IntakeEditor date={date} initial={editor.draft} record={editor.record} onClose={closeEditor} onPreview={showPreview} onLabel={(draft) => { void dietGet<LabelProposal>(`/drafts/${draft.id}/label`).then((value) => { closeEditor(); setLabelProposal(value); }).catch((caught) => setError(String(caught))); }} onSplit={() => { closeEditor(); setSection("records"); setSuccess("已拆分到草稿箱，每餐仍需分别核对确认。"); void refresh(); }} />}
    {labelProposal && <FoodEditor label={labelProposal} onClose={() => setLabelProposal(null)} onPreview={(value) => { setLabelProposal(null); showPreview(value); }} />}
    {parserOpen && <ParseIntake imageEnabled={!!context?.capabilities.image} onClose={() => setParserOpen(false)} onDraft={(draft) => { setParserOpen(false); openEditor({ draft }); void refresh(); }} />}
    {targetEditor && <TargetsEditor date={date} target={targetEditor.target} onClose={() => setTargetEditor(null)} onPreview={showPreview} />}
    {preview && <DietPreview value={preview} onClose={closePreview} onSaved={() => { setSuccess("已保存。当前统计已更新。"); closePreview(); }} />}
    {evidenceItem && <EvidenceDialog item={evidenceItem} onClose={() => setEvidenceItem(null)} />}
  </div>;
}

function EvidenceDialog({ item, onClose }: { item: DietItem; onClose: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => { const element = ref.current; element?.showModal(); return () => element?.close(); }, []);
  return <dialog ref={ref} className="diet-dialog" aria-labelledby="evidence-title" onCancel={onClose}><div className="diet-dialog-heading"><h2 id="evidence-title">{item.name_snapshot} · 营养依据</h2><button className="icon-button" title="关闭依据" aria-label="关闭依据" onClick={onClose}><X size={18} /></button></div><dl className="diet-target-details">{Object.entries(nutrients).map(([key, meta]) => <div key={key}><dt>{meta.name}</dt><dd>{rangeText(item.nutrition_snapshot[key as keyof typeof nutrients].value, key as keyof typeof nutrients)}</dd></div>)}</dl><p>{item.evidence_snapshot?.calculation_note || "当前没有可计算的营养资料。"}</p><p className="muted">{item.evidence_snapshot?.source_ref} {item.evidence_snapshot?.source_version}</p>{item.evidence_snapshot?.license && <p>来源授权：{item.evidence_snapshot.license}</p>}</dialog>;
}
