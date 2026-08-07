import { FormEvent, useEffect, useMemo, useState } from "react";
import {
  Activity,
  ArrowRight,
  Bot,
  Braces,
  Clock3,
  Coins,
  LoaderCircle,
  RefreshCw,
  Search,
  ShieldCheck,
  Wrench,
} from "lucide-react";
import { EmptyState, ErrorNotice, JsonBlock, PageHeader, StatusBadge } from "../../components/Common";
import { getTrace } from "../../lib/api";
import { formatDate, formatDuration, formatMoney, titleFromKey } from "../../lib/format";
import type { ConnectionState, TraceEvent, TraceRun } from "../../types";

type TraceFilter = "all" | "agent" | "tool" | "guard" | "system";

function eventCategory(event: TraceEvent): TraceFilter {
  const value = `${event.type} ${event.component}`.toLowerCase();
  if (value.includes("guard") || value.includes("risk") || value.includes("safety")) return "guard";
  if (value.includes("tool") || value.includes("mcp") || value.includes("retriev")) return "tool";
  if (value.includes("agent") || value.includes("model") || value.includes("llm")) return "agent";
  return "system";
}

function traceIcon(event: TraceEvent) {
  const category = eventCategory(event);
  if (category === "agent") return <Bot size={16} />;
  if (category === "tool") return <Wrench size={16} />;
  if (category === "guard") return <ShieldCheck size={16} />;
  return <Activity size={16} />;
}

export function TraceView({
  connection,
  recentTraceIds,
}: {
  connection: ConnectionState;
  recentTraceIds: string[];
}) {
  const [query, setQuery] = useState(recentTraceIds[0] || "");
  const [trace, setTrace] = useState<TraceRun | null>(null);
  const [filter, setFilter] = useState<TraceFilter>("all");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = async (traceId: string) => {
    if (!traceId.trim()) return;
    setLoading(true);
    setError(null);
    try {
      setTrace(await getTrace(traceId.trim()));
    } catch (caught) {
      setTrace(null);
      setError(caught instanceof Error ? caught.message : "无法读取 Trace");
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    const newest = recentTraceIds[0];
    if (newest && !trace && connection === "online") {
      setQuery(newest);
      void load(newest);
    }
  }, [connection, recentTraceIds]);

  const filteredEvents = useMemo(
    () => trace?.events.filter((event) => filter === "all" || eventCategory(event) === filter) || [],
    [filter, trace],
  );

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void load(query);
  };

  return (
    <div className="page-container">
      <PageHeader
        eyebrow="Observability"
        title="调用链回放"
        description="按执行顺序检查 Agent、模型、工具、安全节点及状态迁移，定位真实失败位置。"
      />

      <form className="lookup-bar" onSubmit={submit}>
        <label>
          <Search size={17} />
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="输入 Trace ID" aria-label="Trace ID" />
        </label>
        <button className="button button-primary" type="submit" disabled={!query.trim() || loading || connection !== "online"}>
          {loading ? <LoaderCircle className="spin" size={16} /> : <Search size={16} />}
          查询
        </button>
      </form>

      {recentTraceIds.length ? (
        <div className="recent-queries">
          <span>最近请求</span>
          {recentTraceIds.slice(0, 5).map((traceId) => (
            <button key={traceId} type="button" onClick={() => { setQuery(traceId); void load(traceId); }} title={traceId}>
              {traceId.slice(0, 8)}
            </button>
          ))}
        </div>
      ) : null}

      {error ? <ErrorNotice message={error} /> : null}

      {!trace && !loading ? (
        <EmptyState
          icon={<Activity size={24} />}
          title="输入 Trace ID 查看执行链"
          description="完成一次 Agent 对话后，可从回复下方直接进入对应 Trace。页面不会生成模拟调用记录。"
        />
      ) : null}

      {trace ? (
        <div className="trace-workspace">
          <section className="trace-summary">
            <div className="trace-summary-title">
              <div><span>Trace ID</span><h2 title={trace.traceId}>{trace.traceId}</h2></div>
              <StatusBadge status={trace.status} />
            </div>
            <div className="stat-grid stat-grid-four">
              <div className="stat-item"><Clock3 size={17} /><span>总耗时</span><strong>{formatDuration(trace.durationMs)}</strong></div>
              <div className="stat-item"><Braces size={17} /><span>Token</span><strong>{trace.totalTokens?.toLocaleString() ?? "--"}</strong></div>
              <div className="stat-item"><Coins size={17} /><span>模型成本</span><strong>{formatMoney(trace.totalCost)}</strong></div>
              <div className="stat-item"><Activity size={17} /><span>事件数</span><strong>{trace.events.length}</strong></div>
            </div>
            <div className="trace-meta-row">
              <span>开始于 {formatDate(trace.startedAt)}</span>
              {trace.threadId ? <span title={trace.threadId}>Thread {trace.threadId.slice(0, 8)}</span> : null}
            </div>
          </section>

          <div className="trace-controls">
            <div className="segmented-control" aria-label="Trace 类型筛选">
              {(["all", "agent", "tool", "guard", "system"] as const).map((value) => (
                <button key={value} type="button" className={filter === value ? "is-active" : ""} onClick={() => setFilter(value)}>
                  {{ all: "全部", agent: "Agent", tool: "工具", guard: "安全", system: "流程" }[value]}
                </button>
              ))}
            </div>
            <button className="button button-ghost button-compact" type="button" onClick={() => void load(trace.traceId)} disabled={loading}>
              <RefreshCw size={15} className={loading ? "spin" : ""} />刷新
            </button>
          </div>

          {filteredEvents.length ? (
            <ol className="trace-timeline">
              {filteredEvents.map((event) => (
                <li className={`trace-event trace-${eventCategory(event)}`} key={event.id}>
                  <div className="trace-event-rail"><span>{traceIcon(event)}</span></div>
                  <div className="trace-event-main">
                    <div className="trace-event-heading">
                      <div>
                        <span>#{event.order} · {titleFromKey(event.type)}</span>
                        <h3>{titleFromKey(event.component)}</h3>
                      </div>
                      <StatusBadge status={event.status} />
                    </div>
                    <div className="trace-event-meta">
                      {event.timestamp ? <span>{formatDate(event.timestamp)}</span> : null}
                      {event.durationMs !== undefined ? <span>{formatDuration(event.durationMs)}</span> : null}
                      {event.model ? <span>{event.model}</span> : null}
                      {(event.inputTokens ?? 0) + (event.outputTokens ?? 0) > 0 ? (
                        <span>{((event.inputTokens ?? 0) + (event.outputTokens ?? 0)).toLocaleString()} tokens</span>
                      ) : null}
                      {event.fromPhase || event.toPhase ? (
                        <span className="phase-change">{event.fromPhase || "--"}<ArrowRight size={13} />{event.toPhase || "--"}</span>
                      ) : null}
                    </div>
                    {event.error ? <p className="trace-error-copy">{event.error}</p> : null}
                    {event.input !== undefined || event.output !== undefined ? (
                      <details className="trace-details">
                        <summary>查看输入与输出</summary>
                        <div className="trace-io-grid">
                          <div><span>Input</span><JsonBlock value={event.input} /></div>
                          <div><span>Output</span><JsonBlock value={event.output} /></div>
                        </div>
                      </details>
                    ) : null}
                  </div>
                </li>
              ))}
            </ol>
          ) : (
            <div className="inline-empty"><Activity size={20} /><div><strong>当前筛选没有事件</strong><p>切换筛选条件查看其他执行节点。</p></div></div>
          )}
        </div>
      ) : null}
    </div>
  );
}
