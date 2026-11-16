import { useEffect, useState } from "react";
import { Save } from "lucide-react";
import { ErrorNotice } from "../../components/Common";
import { dietWrite } from "./api";
import type { DietContext } from "./types";

export function DietSettings({ context, online, onSaved }: { context: DietContext; online: boolean; onSaved: () => void }) {
  const [timezone, setTimezone] = useState(context.timezone);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { setTimezone(context.timezone); }, [context.timezone]);
  const zones = [...new Set([context.timezone, Intl.DateTimeFormat().resolvedOptions().timeZone, "America/Denver", "America/Los_Angeles", "America/New_York", "Asia/Shanghai", "Asia/Tokyo", "Europe/London", "UTC"])];
  const save = async () => {
    setBusy(true); setError(null);
    try { await dietWrite("/context", { timezone }, context.diet_epoch, "PATCH"); onSaved(); }
    catch (caught) { setError(caught instanceof Error ? caught.message : "时区保存失败"); }
    finally { setBusy(false); }
  };
  return <div className="diet-timezone">{error && <ErrorNotice message={error} />}<form onSubmit={(event) => { event.preventDefault(); void save(); }}><label><span>记餐时区</span><input list="diet-timezones" value={timezone} onChange={(event) => setTimezone(event.target.value)} required maxLength={120} /></label><datalist id="diet-timezones">{zones.map((zone) => <option key={zone} value={zone} />)}</datalist><button className="icon-button" title="保存时区" aria-label="保存时区" disabled={busy || !online || timezone === context.timezone}><Save size={17} /></button></form></div>;
}
