import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  CalendarDays,
  FlaskConical,
  Leaf,
  Menu,
  MessageSquareText,
  RefreshCw,
  UserRound,
  Wifi,
  WifiOff,
  X,
  LockKeyhole,
} from "lucide-react";
import { ChatView } from "./features/chat/ChatView";
import { EvaluationView } from "./features/evaluations/EvaluationView";
import { MealPlansView } from "./features/plans/MealPlansView";
import { ProfileView } from "./features/profile/ProfileView";
import { TraceView } from "./features/traces/TraceView";
import { API_BASE_URL, authenticateAccessToken, checkHealth } from "./lib/api";
import type { ConnectionState, RuntimeStatus, ViewKey } from "./types";

const navigation = [
  { key: "chat" as const, label: "对话", icon: MessageSquareText },
  { key: "profile" as const, label: "用户画像", icon: UserRound },
  { key: "plans" as const, label: "饮食计划", icon: CalendarDays },
  { key: "traces" as const, label: "Trace", icon: Activity },
  { key: "evaluations" as const, label: "评估", icon: FlaskConical },
];

const viewTitles: Record<ViewKey, string> = {
  chat: "Agent 对话",
  profile: "用户画像",
  plans: "饮食计划",
  traces: "调用链回放",
  evaluations: "离线评估",
};

function getInitialView(): ViewKey {
  const candidate = window.location.hash.replace("#", "") as ViewKey;
  return navigation.some((item) => item.key === candidate) ? candidate : "chat";
}

function getStoredTraceIds(): string[] {
  try {
    const value = JSON.parse(localStorage.getItem("nutripilot.traceIds") || "[]") as unknown;
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

export default function App() {
  const [view, setView] = useState<ViewKey>(getInitialView);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [connection, setConnection] = useState<ConnectionState>("checking");
  const [runtime, setRuntime] = useState<RuntimeStatus | null>(null);
  const [traceIds, setTraceIds] = useState<string[]>(getStoredTraceIds);
  const [authorized, setAuthorized] = useState(false);
  const [accessToken, setAccessToken] = useState("");
  const [accessError, setAccessError] = useState("");
  const [accessBusy, setAccessBusy] = useState(false);

  const refreshHealth = useCallback(async () => {
    setConnection("checking");
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 4_000);
    const status = await checkHealth(controller.signal);
    window.clearTimeout(timer);
    setRuntime(status);
    if (status?.authRequired) {
      const token = sessionStorage.getItem("nutripilot.accessToken");
      if (token) {
        try {
          await authenticateAccessToken(token);
          setAuthorized(true);
        } catch {
          setAuthorized(false);
        }
      } else {
        setAuthorized(false);
      }
    }
    setConnection(status ? "online" : "offline");
  }, []);

  useEffect(() => {
    void refreshHealth();
    const interval = window.setInterval(() => void refreshHealth(), 30_000);
    return () => window.clearInterval(interval);
  }, [refreshHealth]);

  useEffect(() => {
    window.location.hash = view;
  }, [view]);

  const addTraceId = useCallback((traceId: string) => {
    if (!traceId) return;
    setTraceIds((current) => {
      const next = [traceId, ...current.filter((item) => item !== traceId)].slice(0, 12);
      localStorage.setItem("nutripilot.traceIds", JSON.stringify(next));
      return next;
    });
  }, []);

  const currentView = useMemo(() => {
    switch (view) {
      case "profile":
        return <ProfileView connection={connection} />;
      case "plans":
        return <MealPlansView connection={connection} onNavigateChat={() => setView("chat")} />;
      case "traces":
        return <TraceView connection={connection} recentTraceIds={traceIds} />;
      case "evaluations":
        return <EvaluationView connection={connection} runtime={runtime} />;
      case "chat":
      default:
        return (
          <ChatView
            connection={connection}
            onTraceDiscovered={addTraceId}
            onOpenTrace={(traceId) => {
              addTraceId(traceId);
              setView("traces");
            }}
          />
        );
    }
  }, [addTraceId, connection, runtime, traceIds, view]);

  const navigate = (next: ViewKey) => {
    setView(next);
    setMobileOpen(false);
  };

  return (
    <div className="app-shell">
      <aside className={`sidebar${mobileOpen ? " is-open" : ""}`} aria-label="主导航">
        <div className="brand-row">
          <div className="brand-mark" aria-hidden="true">
            <Leaf size={19} />
          </div>
          <div>
            <strong>NutriPilot</strong>
            <span>Agent Workspace</span>
          </div>
          <button
            className="icon-button sidebar-close"
            type="button"
            onClick={() => setMobileOpen(false)}
            aria-label="关闭导航"
          >
            <X size={19} />
          </button>
        </div>

        <nav className="nav-list">
          <p className="nav-label">工作台</p>
          {navigation.map((item) => {
            const Icon = item.icon;
            return (
              <button
                className={`nav-item${view === item.key ? " is-active" : ""}`}
                key={item.key}
                type="button"
                onClick={() => navigate(item.key)}
              >
                <Icon size={18} aria-hidden="true" />
                <span>{item.label}</span>
              </button>
            );
          })}
        </nav>

        <div className="sidebar-footer">
          <div className="connection-card">
            <div className={`connection-icon connection-${connection}`}>
              {connection === "offline" ? <WifiOff size={17} /> : <Wifi size={17} />}
            </div>
            <div>
              <strong>
                {connection === "checking" ? "正在检查" : connection === "online" ? "服务已就绪" : "服务未连接"}
              </strong>
              <span title={API_BASE_URL}>{API_BASE_URL.replace(/^https?:\/\//, "")}</span>
            </div>
            <button className="icon-button" type="button" onClick={() => void refreshHealth()} aria-label="重新检查服务">
              <RefreshCw size={15} className={connection === "checking" ? "spin" : ""} />
            </button>
          </div>
          <p className="environment-note">
            {runtime
              ? `${runtime.modelProvider === "fake" ? "规则演示" : "真实模型"} · ${runtime.modelName} · ${runtime.storage} · ${runtime.mcpTransport}`
              : "运行环境未识别"}
          </p>
        </div>
      </aside>

      {mobileOpen ? <button className="sidebar-scrim" onClick={() => setMobileOpen(false)} aria-label="关闭导航" /> : null}

      <div className="workspace">
        <header className="topbar">
          <button className="icon-button mobile-menu" type="button" onClick={() => setMobileOpen(true)} aria-label="打开导航">
            <Menu size={20} />
          </button>
          <div>
            <span className="topbar-kicker">NutriPilot</span>
            <strong>{viewTitles[view]}</strong>
          </div>
          <div
            className={`compact-connection compact-${connection}`}
            title={runtime ? `${runtime.modelName} · ${runtime.embeddingProvider}/${runtime.rerankProvider}` : `API: ${API_BASE_URL}`}
          >
            <span />
            {connection === "online"
              ? runtime?.modelProvider === "fake" ? "规则演示" : "真实模型"
              : connection === "checking" ? "检查中" : "离线"}
          </div>
        </header>
        <main className={`main-content view-${view}`}>
          {runtime?.authRequired && !authorized ? (
            <section className="access-panel" aria-labelledby="access-title">
              <LockKeyhole size={24} aria-hidden="true" />
              <h1 id="access-title">连接 NutriPilot</h1>
              <form onSubmit={(event) => {
                event.preventDefault();
                setAccessBusy(true);
                setAccessError("");
                void authenticateAccessToken(accessToken.trim())
                  .then(() => {
                    setAuthorized(true);
                    setAccessToken("");
                    setTraceIds(getStoredTraceIds());
                  })
                  .catch((caught) => setAccessError(caught instanceof Error ? caught.message : "连接失败"))
                  .finally(() => setAccessBusy(false));
              }}>
                <label className="field">
                  <span>访问令牌</span>
                  <input type="password" autoComplete="current-password" required
                    value={accessToken} onChange={(event) => setAccessToken(event.target.value)} />
                </label>
                {accessError ? <p role="alert">{accessError}</p> : null}
                <button className="button button-primary" type="submit" disabled={accessBusy}>
                  <LockKeyhole size={16} />{accessBusy ? "连接中" : "连接"}
                </button>
              </form>
            </section>
          ) : runtime ? currentView : null}
        </main>
      </div>

      <nav className="mobile-bottom-nav" aria-label="移动端导航">
        {navigation.map((item) => {
          const Icon = item.icon;
          return (
            <button
              className={view === item.key ? "is-active" : ""}
              key={item.key}
              type="button"
              onClick={() => navigate(item.key)}
              aria-label={item.label}
            >
              <Icon size={19} />
              <span>{item.label === "用户画像" ? "画像" : item.label === "饮食计划" ? "计划" : item.label}</span>
            </button>
          );
        })}
      </nav>
    </div>
  );
}
