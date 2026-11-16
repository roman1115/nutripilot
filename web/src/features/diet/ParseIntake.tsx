import { useEffect, useRef, useState } from "react";
import { Camera, FileText, LoaderCircle, Square, X } from "lucide-react";
import { ErrorNotice } from "../../components/Common";
import { privateResponse } from "../../lib/api";
import { dietGet, dietWrite } from "./api";
import type { DietJob, Draft } from "./types";

export function ParseIntake({ imageEnabled, onClose, onDraft }: { imageEnabled: boolean; onClose: () => void; onDraft: (draft: Draft) => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [mode, setMode] = useState<"text" | "image">("text");
  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [authorized, setAuthorized] = useState(false);
  const [imageKind, setImageKind] = useState<"intake_image" | "label_image">("intake_image");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const key = useRef(crypto.randomUUID());
  const uploaded = useRef<string[]>([]);
  useEffect(() => { const element = dialog.current; element?.showModal(); return () => element?.close(); }, []);
  const submit = async () => {
    setBusy(true); setError(null);
    try {
      if (mode === "image" && !uploaded.current.length) {
        if (!files.length || files.length > 4 || files.some((file) => file.size > 10 * 1024 * 1024) || files.reduce((sum, file) => sum + file.size, 0) > 20 * 1024 * 1024) throw new Error("请选择1至4张图片，每张不超过10MiB，合计不超过20MiB。");
        const body = new FormData(); body.set("kind", imageKind); files.forEach((file) => body.append("files", file));
        const response = await privateResponse("/api/v1/diet/media", { method: "POST", headers: { "Idempotency-Key": key.current }, body });
        const result = await response.json() as { items: { id: string }[] }; uploaded.current = result.items.map((item) => item.id);
      }
      const draft = await dietWrite<Draft>("/drafts", mode === "text" ? { mode, text } : { mode, caption: text, media_ids: uploaded.current, send_images_authorized: authorized }, undefined, "POST", key.current);
      onDraft(draft);
    } catch (caught) { setError(caught instanceof Error ? caught.message : "创建草稿失败"); }
    finally { setBusy(false); }
  };
  return <dialog ref={dialog} className="diet-dialog" aria-labelledby="parse-title" onCancel={(event) => { if (busy) event.preventDefault(); else onClose(); }}><div className="diet-dialog-heading"><h2 id="parse-title">文字与照片记餐</h2><button className="icon-button" aria-label="关闭记餐" title="关闭记餐" disabled={busy} onClick={onClose}><X size={18} /></button></div>
    <div className="diet-segment" aria-label="录入方式"><button aria-pressed={mode === "text"} disabled={busy} onClick={() => { setMode("text"); key.current = crypto.randomUUID(); }}><FileText size={16} />文字</button><button aria-pressed={mode === "image"} disabled={busy || !imageEnabled} title={!imageEnabled ? "视觉模型未配置" : "照片"} onClick={() => { setMode("image"); key.current = crypto.randomUUID(); }}><Camera size={16} />照片</button></div>
    {error && <ErrorNotice message={error} />}
    <form onSubmit={(event) => { event.preventDefault(); void submit(); }}><label className="field diet-note"><span>{mode === "text" ? "饮食原话" : "照片补充说明"}</span><textarea rows={5} maxLength={4000} value={text} required={mode === "text"} onChange={(event) => { setText(event.target.value); key.current = crypto.randomUUID(); }} /></label>
      {mode === "image" && <><label className="field"><span>图片用途</span><select aria-label="图片用途" value={imageKind} onChange={(event) => { setImageKind(event.target.value as typeof imageKind); uploaded.current = []; key.current = crypto.randomUUID(); }}><option value="intake_image">实际餐食</option><option value="label_image">包装营养标签</option></select></label><label className="field"><span>食物照片</span><input type="file" accept="image/jpeg,image/png,image/webp" multiple required onChange={(event) => { setFiles(Array.from(event.target.files || [])); uploaded.current = []; key.current = crypto.randomUUID(); }} /></label><label><input type="checkbox" checked={authorized} onChange={(event) => { setAuthorized(event.target.checked); key.current = crypto.randomUUID(); }} />授权将这些照片及说明发送给配置的外部视觉模型</label></>}
      <div className="modal-actions"><button type="button" className="button button-secondary" disabled={busy} onClick={onClose}>取消</button><button type="submit" className="button button-primary" disabled={busy}>{busy ? <LoaderCircle size={16} className="spin" /> : <FileText size={16} />}{mode === "image" && !authorized ? "仅保存私有草稿" : "准备解析草稿"}</button></div>
    </form></dialog>;
}

export function ParseJobStatus({ draft, onDraft }: { draft: Draft; onDraft: (draft: Draft) => void }) {
  const [job, setJob] = useState<DietJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!draft.job_id) return;
    let active = true; let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await dietGet<DietJob>(`/jobs/${draft.job_id}`);
        if (!active) return;
        setJob(value); setError(null);
        if (value.state === "queued" || value.state === "running") timer = setTimeout(() => void poll(), 1000);
        else if (value.state === "succeeded") onDraft(await dietGet<Draft>(`/drafts/${draft.id}`));
      } catch (caught) { if (active) { setError(caught instanceof Error ? caught.message : "任务读取失败"); timer = setTimeout(() => void poll(), 3000); } }
    };
    void poll(); return () => { active = false; clearTimeout(timer); };
  }, [draft.id, draft.job_id, onDraft]);
  const act = async (cancel: boolean) => {
    setBusy(true); setError(null);
    try {
      if (cancel && draft.job_id) setJob(await dietWrite<DietJob>(`/jobs/${draft.job_id}/cancel`, {}));
      else {
        if (draft.channel === "image" && !window.confirm("将草稿中所选图片及说明发送给配置的外部视觉模型？")) return;
        await dietWrite(`/drafts/${draft.id}/parse`, { kind: draft.channel === "image" ? "image" : "text", media_ids: draft.media_ids, send_images_authorized: draft.channel === "image" }, draft.version);
        onDraft(await dietGet<Draft>(`/drafts/${draft.id}`));
      }
    } catch (caught) { setError(caught instanceof Error ? caught.message : "任务操作失败"); }
    finally { setBusy(false); }
  };
  const running = !!job && ["queued", "running"].includes(job.state);
  return <div className="diet-job-status" role="status">{error && <ErrorNotice message={error} />}<span>{job ? ({ queued: "等待解析", running: "正在解析", succeeded: job.usage.result === "applied" ? "解析完成，待核对" : "解析结果需人工核对", failed: `解析失败：${job.error_code}`, cancelled: "解析已取消" })[job.state] : draft.job_id ? "正在读取解析任务" : "尚未授权解析"}</span><button type="button" className="button button-secondary" disabled={busy} onClick={() => void act(running)}>{running ? <Square size={15} /> : <FileText size={15} />}{running ? "取消解析" : "重新解析"}</button></div>;
}
