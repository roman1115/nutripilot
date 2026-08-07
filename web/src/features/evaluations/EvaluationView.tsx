import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  FlaskConical,
  LoaderCircle,
  Play,
  RefreshCw,
  Search,
  XCircle,
} from "lucide-react";
import { EmptyState, ErrorNotice, JsonBlock, PageHeader, StatusBadge } from "../../components/Common";
import { getEvaluation, startEvaluation } from "../../lib/api";
import { formatDate, formatMetric, titleFromKey } from "../../lib/format";
import type { ConnectionState, EvaluationRun, RuntimeStatus } from "../../types";

const runningStates = new Set(["queued", "pending", "running", "in_progress"]);

export function EvaluationView({
  connection,
  runtime,
}: {
  connection: ConnectionState;
  runtime: RuntimeStatus | null;
}) {
  const [runIdInput, setRunIdInput] = useState("");
  const [datasetVersion, setDatasetVersion] = useState("");
  const [promptVersion, setPromptVersion] = useState("");
  const [run, setRun] = useState<EvaluationRun | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  const clearPoll = () => {
    if (pollRef.current !== null) window.clearTimeout(pollRef.current);
    pollRef.current = null;
  };

  useEffect(() => clearPoll, []);

  const loadRun = async (runId: string, silent = false) => {
    if (!runId.trim()) return;
    if (!silent) setLoading(true);
    setError(null);
    try {
      const value = await getEvaluation(runId.trim());
      setRun(value);
      setRunIdInput(value.runId);
      clearPoll();
      if (runningStates.has(value.status.toLowerCase())) {
        pollRef.current = window.setTimeout(() => void loadRun(value.runId, true), 3_000);
      }
    } catch (caught) {
      clearPoll();
      setError(caught instanceof Error ? caught.message : "无法读取评估报告");
    } finally {
      if (!silent) setLoading(false);
    }
  };

  const launch = async () => {
    clearPoll();
    setLoading(true);
    setError(null);
    try {
      const value = await startEvaluation({ datasetVersion, promptVersion });
      setRun(value);
      setRunIdInput(value.runId);
      if (value.runId && runningStates.has(value.status.toLowerCase())) {
        pollRef.current = window.setTimeout(() => void loadRun(value.runId, true), 2_000);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "无法启动评估");
    } finally {
      setLoading(false);
    }
  };

  const submitLookup = (event: FormEvent) => {
    event.preventDefault();
    clearPoll();
    void loadRun(runIdInput);
  };

  const passRate = useMemo(() => {
    if (!run?.totalCases || run.passedCases === undefined) return undefined;
    return run.passedCases / run.totalCases;
  }, [run]);

  return (
    <div className="page-container">
      <PageHeader
        eyebrow="Agent eval"
        title={runtime?.modelProvider === "fake" ? "确定性回归基线" : "真实模型评估"}
        description={
          runtime?.modelProvider === "fake"
            ? "当前使用 FakeModel，只验证工程流程、工具与安全规则，不代表真实大模型质量。"
            : `当前评估模型为 ${runtime?.modelName || "未识别模型"}，结果由后端版本化测试集产生。`
        }
      />

      <div className="evaluation-actions">
        <section className="evaluation-launcher">
          <div className="section-heading compact-heading">
            <div className="section-icon"><Play size={17} /></div>
            <div><h2>发起评估</h2><p>留空时使用服务端默认版本。</p></div>
          </div>
          <div className="evaluation-form-row">
            <label className="field"><span>数据集版本</span><input value={datasetVersion} onChange={(event) => setDatasetVersion(event.target.value)} placeholder="例如：eval-v1" /></label>
            <label className="field"><span>Prompt 版本</span><input value={promptVersion} onChange={(event) => setPromptVersion(event.target.value)} placeholder="例如：planner-v2" /></label>
            <button className="button button-primary" type="button" onClick={() => void launch()} disabled={loading || connection !== "online"}>
              {loading ? <LoaderCircle className="spin" size={16} /> : <Play size={16} />}
              开始运行
            </button>
          </div>
        </section>

        <form className="evaluation-lookup" onSubmit={submitLookup}>
          <span>已有 Run ID</span>
          <label><Search size={16} /><input value={runIdInput} onChange={(event) => setRunIdInput(event.target.value)} placeholder="输入评估 Run ID" aria-label="评估 Run ID" /></label>
          <button className="button button-secondary" type="submit" disabled={!runIdInput.trim() || loading || connection !== "online"}>
            <Search size={15} />查询
          </button>
        </form>
      </div>

      {error ? <ErrorNotice message={error} /> : null}

      {!run ? (
        <EmptyState
          icon={<FlaskConical size={24} />}
          title="还没有评估结果"
          description={
            runtime?.modelProvider === "fake"
              ? "启动确定性回归评估，或输入已有 Run ID。该分数不能作为真实模型准确率。"
              : "启动一轮真实模型评估，或输入已有 Run ID。页面不会预置示例分数。"
          }
        />
      ) : (
        <div className="evaluation-report">
          <section className="evaluation-summary">
            <div className="evaluation-summary-heading">
              <div><span className="eyebrow">Evaluation run</span><h2 title={run.runId}>{run.runId || "服务端未返回 Run ID"}</h2></div>
              <div className="evaluation-summary-actions">
                <StatusBadge status={run.status} />
                {run.runId ? <button className="icon-button" type="button" onClick={() => void loadRun(run.runId)} aria-label="刷新评估状态"><RefreshCw size={16} /></button> : null}
              </div>
            </div>
            <div className="evaluation-meta">
              <span>数据集：{run.datasetVersion || "未返回"}</span>
              <span>
                Prompt：
                {Object.keys(run.promptVersions).length
                  ? Object.entries(run.promptVersions).map(([name, version]) => `${name}=${version}`).join("，")
                  : run.promptVersion || "未返回"}
              </span>
              <span>开始：{formatDate(run.startedAt)}</span>
              {run.completedAt ? <span>完成：{formatDate(run.completedAt)}</span> : null}
            </div>
            {runningStates.has(run.status.toLowerCase()) ? (
              <div className="evaluation-running"><LoaderCircle className="spin" size={17} /><span>后端评估任务仍在运行，页面会自动刷新。</span></div>
            ) : null}
          </section>

          <section className="evaluation-counts">
            <div><FlaskConical size={18} /><span>用例总数</span><strong>{run.totalCases ?? "--"}</strong></div>
            <div className="count-pass"><CheckCircle2 size={18} /><span>通过</span><strong>{run.passedCases ?? "--"}</strong></div>
            <div className="count-fail"><XCircle size={18} /><span>失败</span><strong>{run.failedCases ?? "--"}</strong></div>
            <div><span className="rate-mark">%</span><span>通过率</span><strong>{passRate === undefined ? "--" : formatMetric(passRate)}</strong></div>
          </section>

          <section className="metrics-section">
            <div className="section-title-row"><div><h2>核心指标</h2><p>指标名称与数值由评估服务返回。</p></div><span>{Object.keys(run.metrics).length} 项</span></div>
            {Object.keys(run.metrics).length ? (
              <div className="metrics-grid">
                {Object.entries(run.metrics).map(([key, value]) => (
                  <div className="metric-item" key={key}><span>{titleFromKey(key)}</span><strong>{formatMetric(value)}</strong></div>
                ))}
              </div>
            ) : (
              <div className="inline-empty"><FlaskConical size={20} /><div><strong>暂未返回指标</strong><p>任务可能仍在运行，或报告尚未生成指标字段。</p></div></div>
            )}
          </section>

          <section className="failures-section">
            <div className="section-title-row"><div><h2>失败用例</h2><p>用于定位低分维度并回流到下一轮迭代。</p></div><span>{run.failures.length} 条</span></div>
            {run.failures.length ? (
              <div className="failure-list">
                {run.failures.map((failure) => (
                  <details className="failure-item" key={failure.caseId}>
                    <summary><AlertTriangle size={16} /><span>{failure.caseId}</span><strong>{failure.reason}</strong></summary>
                    <div className="failure-body">
                      {failure.input ? <div><span>输入</span><p>{failure.input}</p></div> : null}
                      <div className="trace-io-grid">
                        <div><span>Expected</span><JsonBlock value={failure.expected} /></div>
                        <div><span>Actual</span><JsonBlock value={failure.actual} /></div>
                      </div>
                    </div>
                  </details>
                ))}
              </div>
            ) : (
              <div className="inline-empty"><CheckCircle2 size={20} /><div><strong>没有失败用例</strong><p>服务端当前报告未返回失败样本。</p></div></div>
            )}
          </section>
        </div>
      )}
    </div>
  );
}
