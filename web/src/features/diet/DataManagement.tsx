import { useCallback, useEffect, useRef, useState } from "react";
import { Check, Download, FileArchive, LoaderCircle, RefreshCw, Trash2, X } from "lucide-react";
import { ErrorNotice, LoadingState } from "../../components/Common";
import { privateResponse } from "../../lib/api";
import { dietGet, dietWrite, loadLogs, loadPersonalFoods, loadPlanLibrary } from "./api";
import type { DietJob, DietLog, DietPlan, Page, PersonalFood, Target } from "./types";

interface PurgePreview { id: string; version: number; content_hash: string; content: { history_notice: string; impact_notices?: string[]; refs: { id: string; kind: string }[]; related_thread_ids: string[] } }

export function DataManagement({ start, end, online, onChanged }: { start: string; end: string; online: boolean; onChanged: () => void }) {
  const [jobs, setJobs] = useState<DietJob[]>([]);
  const [records, setRecords] = useState<DietLog[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[]>([]);
  const [foods, setFoods] = useState<PersonalFood[]>([]);
  const [plans, setPlans] = useState<DietPlan[]>([]);
  const [targets, setTargets] = useState<Target[]>([]);
  const [foodCursor, setFoodCursor] = useState<string | null>(null);
  const [planCursor, setPlanCursor] = useState<string | null>(null);
  const [otherSelected, setOtherSelected] = useState<{ food_ids: string[]; plan_ids: string[]; target_ids: string[] }>({ food_ids: [], plan_ids: [], target_ids: [] });
  const [media, setMedia] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<PurgePreview | null>(null);
  const [exportStart, setExportStart] = useState(start);
  const [exportEnd, setExportEnd] = useState(end);
  const [message, setMessage] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const refreshGeneration = useRef(0);
  const refresh = useCallback(async () => {
    const generation = ++refreshGeneration.current;
    setLoading(true);
    try {
      const [logs, foodPage, planPage, targetPage] = await Promise.all([
        loadLogs(`/logs?start_date=${start}&end_date=${end}&include_deleted=true`),
        loadPersonalFoods("/personal-foods?include_deleted=true"),
        loadPlanLibrary(`/plan-library?start_date=${start}&end_date=${end}&include_deleted=true`),
        dietGet<Page<{ target: Target }>>(`/targets?start_date=${start}&end_date=${end}`),
      ]);
      if (generation !== refreshGeneration.current) return;
      setRecords(logs.items.filter((log) => log.deleted_at)); setCursor(logs.next_cursor);
      setFoods(foodPage.items.filter((food) => food.deleted_at)); setFoodCursor(foodPage.next_cursor);
      setPlans(planPage.items.filter((plan) => plan.deleted_at)); setPlanCursor(planPage.next_cursor);
      setTargets(targetPage.items.map((period) => period.target));
    } catch (caught) { if (generation === refreshGeneration.current) throw caught; }
    finally { if (generation === refreshGeneration.current) setLoading(false); }
  }, [start, end]);
  useEffect(() => { setSelected([]); setOtherSelected({ food_ids: [], plan_ids: [], target_ids: [] }); }, [start, end]);
  useEffect(() => { if (online && start && end) void refresh().catch((caught) => setError(String(caught))); return () => { refreshGeneration.current += 1; }; }, [online, start, end, refresh]);
  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try { if (online) { const tasks = await dietGet<{ items: DietJob[] }>("/jobs"); if (active) setJobs(tasks.items); } }
      catch (caught) { if (active) setError(caught instanceof Error ? caught.message : "任务读取失败"); }
      if (active) timer = setTimeout(() => void poll(), 3000);
    };
    void poll(); return () => { active = false; clearTimeout(timer); };
  }, [online]);
  const act = async (operation: () => Promise<void>) => {
    setBusy(true); setError(null);
    try { await operation(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "操作失败"); }
    finally { setBusy(false); }
  };
  const download = async (id: string) => {
    const response = await privateResponse(`/api/v1/diet/exports/${id}/content`);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a"); link.href = url; link.download = "nutripilot-diet.zip"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const choice = (kind: keyof typeof otherSelected, id: string, label: string) => <label className="diet-trash-row" key={id}><input type="checkbox" checked={otherSelected[kind].includes(id)} onChange={(event) => setOtherSelected((values) => ({ ...values, [kind]: event.target.checked ? [...values[kind], id] : values[kind].filter((value) => value !== id) }))} /><span>{label}</span></label>;
  const count = selected.length + otherSelected.food_ids.length + otherSelected.plan_ids.length + otherSelected.target_ids.length;
  return <section className="diet-data-management" aria-label="数据管理" aria-busy={loading}>
    <div className="diet-section-heading"><h2>数据管理</h2><button className="icon-button" title="刷新数据管理" aria-label="刷新数据管理" disabled={busy || loading || !online} onClick={() => void act(refresh)}><RefreshCw size={17} /></button></div>
    {loading && <LoadingState label="正在读取数据管理" />}{error && <ErrorNotice message={error} />}{message && <p role="status">{message}</p>}
    <h3>导出饮食数据</h3>
    <form onSubmit={(event) => { event.preventDefault(); void act(async () => {
      const job = await dietWrite<DietJob>("/exports", { start_date: exportStart, end_date: exportEnd, include_media: media });
      setJobs((values) => [job, ...values.filter((value) => value.id !== job.id)]); setMessage("导出任务已提交。");
    }); }}>
      <div className="diet-form-grid"><label className="field"><span>导出开始日期</span><input type="date" required value={exportStart} onChange={(event) => setExportStart(event.target.value)} /></label><label className="field"><span>导出结束日期</span><input type="date" required min={exportStart} value={exportEnd} onChange={(event) => setExportEnd(event.target.value)} /></label></div>
      <label><input type="checkbox" checked={media} onChange={(event) => setMedia(event.target.checked)} />包含本人已关联图片</label>
      <div className="modal-actions"><button className="button button-secondary" disabled={busy || loading || !online}><FileArchive size={16} />创建导出</button></div>
    </form>
    <div className="diet-job-list" aria-label="后台任务">{jobs.filter((job) => ["export", "purge"].includes(job.kind)).map((job) => <div className="diet-target-row" data-job-id={job.id} key={job.id}><div><strong>{job.kind === "export" ? "饮食导出" : "永久清除"}</strong><span>{({ queued: "排队中", running: "处理中", succeeded: "已完成", failed: "失败", cancelled: "已取消" })[job.state]}</span>{job.error_code && <small>{job.error_code}</small>}</div>{job.kind === "export" && job.state === "succeeded" && <button className="button button-secondary" disabled={busy || !online} onClick={() => void act(() => download(job.id))}><Download size={16} />下载ZIP</button>}</div>)}</div>
    <h3>记录回收站</h3>
    <p className="muted">{start} 至 {end} · 永久清除不可恢复。原始聊天独立保留，备份删除受运维留存策略约束。</p>
    {!loading && records.length === 0 && <p className="muted">{cursor ? "当前已加载页面没有已删除记录，尚有记录未读取。" : "此范围没有已删除记录。"}</p>}
    {records.map((record) => <label className="diet-trash-row" key={record.id}><input type="checkbox" checked={selected.includes(record.id)} onChange={(event) => setSelected((values) => event.target.checked ? [...values, record.id] : values.filter((id) => id !== record.id))} /><span>{record.temporal.local_date} · {record.items.map((item) => item.name_snapshot).join("、")}</span></label>)}
    {cursor && <button className="button button-secondary" disabled={busy || !online} onClick={() => void act(async () => {
      const page = await loadLogs(`/logs?start_date=${start}&end_date=${end}&include_deleted=true&cursor=${encodeURIComponent(cursor)}`);
      setRecords((values) => [...values, ...page.items.filter((log) => log.deleted_at)]); setCursor(page.next_cursor);
    })}>更多回收站记录</button>}
    <h3>个人食物回收站</h3>
    {!loading && !foods.length && <p className="muted">{foodCursor ? "当前已加载页面没有已删除食物，尚有食物未读取。" : "没有已删除个人食物。"}</p>}
    {foods.map((food) => choice("food_ids", food.id, food.name))}
    {foodCursor && <button className="button button-secondary" disabled={busy || !online} onClick={() => void act(async () => { const page = await loadPersonalFoods(`/personal-foods?include_deleted=true&cursor=${encodeURIComponent(foodCursor)}`); setFoods((values) => [...values, ...page.items.filter((food) => food.deleted_at)]); setFoodCursor(page.next_cursor); })}>更多回收站食物</button>}
    <h3>归档餐单</h3>
    {!loading && !plans.length && <p className="muted">{planCursor ? "当前已加载页面没有归档餐单，尚有餐单未读取。" : "此范围没有归档餐单。"}</p>}
    {plans.map((plan) => choice("plan_ids", plan.id, `${plan.start_date} · ${plan.title}`))}
    {planCursor && <button className="button button-secondary" disabled={busy || !online} onClick={() => void act(async () => { const page = await loadPlanLibrary(`/plan-library?start_date=${start}&end_date=${end}&include_deleted=true&cursor=${encodeURIComponent(planCursor)}`); setPlans((values) => [...values, ...page.items.filter((plan) => plan.deleted_at)]); setPlanCursor(page.next_cursor); })}>更多归档餐单</button>}
    <h3>目标历史清除</h3>
    <p className="muted">清除生效点会改变对应日期的目标比较，确认前请核对影响。</p>
    {!targets.length && <p className="muted">此范围没有目标生效点。</p>}
    {targets.map((target) => choice("target_ids", target.id, `${target.effective_date} · 目标版本 ${target.version}`))}
    <div className="modal-actions"><button className="button button-secondary" disabled={busy || !online || !count} onClick={() => void act(async () => setPreview(await dietWrite<PurgePreview>("/purge/previews", { record_ids: selected, ...otherSelected })))}><Trash2 size={16} />核对永久清除（{count}）</button></div>
    {preview && <PurgeDialog preview={preview} online={online} onClose={() => setPreview(null)} onSaved={() => { setPreview(null); setSelected([]); setOtherSelected({ food_ids: [], plan_ids: [], target_ids: [] }); setMessage("所选数据已永久清除，文件清理任务已提交。"); onChanged(); void act(refresh); }} />}
  </section>;
}

function PurgeDialog({ preview, online, onClose, onSaved }: { preview: PurgePreview; online: boolean; onClose: () => void; onSaved: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [accepted, setAccepted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { const element = dialog.current; element?.showModal(); return () => element?.close(); }, []);
  const submit = async () => {
    setBusy(true); setError(null);
    try { await dietWrite(`/purge/previews/${preview.id}/confirm`, { approved: true, preview_version: preview.version, content_hash: preview.content_hash }, preview.version); onSaved(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "清除失败"); }
    finally { setBusy(false); }
  };
  return <dialog className="diet-dialog" ref={dialog} aria-labelledby="purge-title" onCancel={(event) => { if (busy) event.preventDefault(); else onClose(); }}><div className="diet-dialog-heading"><h2 id="purge-title">确认永久清除</h2><button className="icon-button" title="关闭清除确认" aria-label="关闭清除确认" disabled={busy} onClick={onClose}><X size={18} /></button></div>{error && <ErrorNotice message={error} />}<p>将不可恢复地清除 {preview.content.refs.length} 个对象及其编辑草稿、修订副本，无其他引用的图片随后清理。</p>{preview.content.impact_notices?.map((notice) => <p className="diet-warning" key={notice}>{notice}</p>)}<p className="diet-warning">{preview.content.history_notice}</p>{preview.content.related_thread_ids.map((id) => <p key={id}><a href={`?thread=${encodeURIComponent(id)}#chat`}>查看关联会话</a></p>)}<label><input type="checkbox" checked={accepted} onChange={(event) => setAccepted(event.target.checked)} />我了解此操作不可恢复</label><div className="modal-actions"><button className="button button-secondary" disabled={busy} onClick={onClose}>取消</button><button className="button button-primary" disabled={!accepted || busy || !online} onClick={() => void submit()}>{busy ? <LoaderCircle size={16} className="spin" /> : <Check size={16} />}永久清除</button></div></dialog>;
}
