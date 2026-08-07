import type { ReactNode } from "react";
import { AlertCircle, LoaderCircle, Plus, X } from "lucide-react";

export function PageHeader({
  eyebrow,
  title,
  description,
  action,
}: {
  eyebrow: string;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <header className="page-header">
      <div>
        <p className="eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <p className="page-description">{description}</p>
      </div>
      {action ? <div className="page-header-action">{action}</div> : null}
    </header>
  );
}

export function ErrorNotice({
  title = "暂时无法完成请求",
  message,
  action,
}: {
  title?: string;
  message: string;
  action?: ReactNode;
}) {
  return (
    <div className="notice notice-error" role="alert">
      <AlertCircle size={18} aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        <p>{message}</p>
      </div>
      {action ? <div className="notice-action">{action}</div> : null}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="empty-state-icon">{icon}</div>
      <h2>{title}</h2>
      <p>{description}</p>
      {action}
    </div>
  );
}

export function LoadingState({ label = "正在读取数据" }: { label?: string }) {
  return (
    <div className="loading-state" role="status">
      <LoaderCircle className="spin" size={20} aria-hidden="true" />
      <span>{label}</span>
    </div>
  );
}

export function StatusBadge({ status }: { status: string }) {
  const normalized = status.toLowerCase();
  const tone = ["completed", "success", "passed", "active", "ready"].includes(normalized)
    ? "success"
    : ["failed", "error", "blocked", "rejected"].includes(normalized)
      ? "error"
      : ["running", "pending", "queued", "waiting"].includes(normalized)
        ? "running"
        : "neutral";
  const labels: Record<string, string> = {
    completed: "已完成",
    success: "成功",
    passed: "通过",
    active: "生效中",
    ready: "就绪",
    failed: "失败",
    error: "错误",
    blocked: "已阻止",
    rejected: "已拒绝",
    running: "运行中",
    pending: "待处理",
    queued: "排队中",
    waiting: "等待中",
    unknown: "未知",
  };
  return <span className={`status-badge status-${tone}`}>{labels[normalized] || status}</span>;
}

export function TagEditor({
  value,
  onChange,
  placeholder,
  disabled,
}: {
  value: string[];
  onChange: (next: string[]) => void;
  placeholder: string;
  disabled?: boolean;
}) {
  const remove = (item: string) => onChange(value.filter((entry) => entry !== item));
  const addFromInput = (input: HTMLInputElement) => {
    const additions = input.value
      .split(/[,，]/)
      .map((item) => item.trim())
      .filter(Boolean);
    if (!additions.length) return;
    onChange(Array.from(new Set([...value, ...additions])));
    input.value = "";
  };

  return (
    <div className={`tag-editor${disabled ? " is-disabled" : ""}`}>
      {value.map((item) => (
        <span className="editable-tag" key={item}>
          {item}
          <button
            type="button"
            onClick={() => remove(item)}
            aria-label={`移除 ${item}`}
            disabled={disabled}
          >
            <X size={13} aria-hidden="true" />
          </button>
        </span>
      ))}
      <label className="tag-input-wrap">
        <Plus size={14} aria-hidden="true" />
        <input
          type="text"
          placeholder={placeholder}
          disabled={disabled}
          onKeyDown={(event) => {
            if (event.key === "Enter" || event.key === ",") {
              event.preventDefault();
              addFromInput(event.currentTarget);
            }
          }}
          onBlur={(event) => addFromInput(event.currentTarget)}
        />
      </label>
    </div>
  );
}

export function JsonBlock({ value }: { value: unknown }) {
  if (value === undefined || value === null || value === "") return null;
  return <pre className="json-block">{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</pre>;
}
