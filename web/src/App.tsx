import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  CalendarDays,
  FlaskConical,
  Salad,
  ChevronDown,
  LayoutGrid,
  MessageSquareText,
  RefreshCw,
  UserRound,
  Wifi,
  WifiOff,
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
  { key: "profile" as const, label: "饮食偏好", icon: UserRound },
  { key: "plans" as const, label: "饮食计划", icon: CalendarDays },
  { key: "traces" as const, label: "执行记录", icon: Activity },
  { key: "evaluations" as const, label: "质量评估", icon: FlaskConical },
];

const viewTitles: Record<ViewKey, string> = {
  chat: "饮食对话",
  profile: "饮食偏好",
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
  const navigationRef = useRef<HTMLDivElement>(null);
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
        return null;
    }
  }, [addTraceId, connection, runtime, traceIds, view]);

  const navigate = (next: ViewKey) => {
    setView(next);
    navigationRef.current?.hidePopover();
  };

  return (
    <div className="app-shell">
      <div className="workspace">
        <header className="topbar">
          <button className="app-brand" type="button" onClick={() => navigate("chat")}
            aria-label="NutriPilot 对话首页">
            <Salad size={24} strokeWidth={1.7} aria-hidden="true" />
            <strong>NutriPilot</strong>
          </button>
          {view !== "chat" ? <span className="topbar-view-title">{viewTitles[view]}</span> : null}
          <time className="topbar-date" dateTime={new Date().toLocaleDateString("en-CA")}>
            <CalendarDays size={15} aria-hidden="true" />
            {new Intl.DateTimeFormat("zh-CN", { month: "long", day: "numeric", weekday: "long" }).format(new Date())}
          </time>
          <div
            className={`compact-connection compact-${connection}`}
            title={runtime ? `${runtime.modelName} · ${runtime.embeddingProvider}/${runtime.rerankProvider}` : `API: ${API_BASE_URL}`}
          >
            <span />
            {connection === "online"
              ? runtime?.status === "worker_unavailable" ? "等待执行器"
                : runtime?.modelProvider === "fake" ? "演示模式" : "已连接"
              : connection === "checking" ? "检查中" : "离线"}
          </div>
          <button className="navigation-trigger" type="button" popoverTarget="app-navigation"
            title="功能导航" aria-label="功能导航" aria-controls="app-navigation">
            <LayoutGrid size={18} aria-hidden="true" />
            <span>功能</span>
            <ChevronDown size={14} aria-hidden="true" />
          </button>
          <div id="app-navigation" className="navigation-popover" popover="auto" ref={navigationRef}>
            <nav aria-label="主导航">
              <div className="nav-list">
                <p className="nav-label">我的日常</p>
                {navigation.slice(0, 3).map((item) => {
                  const Icon = item.icon;
                  return <button className={`nav-item${view === item.key ? " is-active" : ""}`}
                    key={item.key} type="button" onClick={() => navigate(item.key)}
                    aria-current={view === item.key ? "page" : undefined}>
                    <Icon size={18} aria-hidden="true" /><span>{item.label}</span>
                  </button>;
                })}
              </div>
              <div className="nav-list nav-tools">
                <p className="nav-label">开发工具</p>
                {navigation.slice(3).map((item) => {
                  const Icon = item.icon;
                  return <button className={`nav-item${view === item.key ? " is-active" : ""}`}
                    key={item.key} type="button" onClick={() => navigate(item.key)}
                    aria-current={view === item.key ? "page" : undefined}>
                    <Icon size={18} aria-hidden="true" /><span>{item.label}</span>
                  </button>;
                })}
              </div>
            </nav>
            <div className="navigation-footer">
              <div className="connection-card">
                <div className={`connection-icon connection-${connection}`}>
                  {connection === "offline" ? <WifiOff size={17} /> : <Wifi size={17} />}
                </div>
                <div>
                  <strong>
                    {connection === "checking" ? "正在检查"
                      : connection === "online" ? runtime?.status === "worker_unavailable" ? "等待任务执行器" : "服务已就绪"
                      : "服务未连接"}
                  </strong>
                  <span>{runtime?.modelProvider === "fake" ? "演示模式" : "日常饮食助手"}</span>
                </div>
                <button className="icon-button" type="button" onClick={() => void refreshHealth()}
                  title="重新检查服务" aria-label="重新检查服务">
                  <RefreshCw size={15} className={connection === "checking" ? "spin" : ""} />
                </button>
              </div>
              <details className="environment-note">
                <summary>连接信息</summary>
                <p>{API_BASE_URL}</p>
                <p>{runtime ? `${runtime.modelName} · ${runtime.storage} · ${runtime.mcpTransport}` : "运行环境未识别"}</p>
              </details>
            </div>
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
          ) : runtime ? (
            <>
              <ChatView
                active={view === "chat"}
                connection={connection}
                onTraceDiscovered={addTraceId}
                onOpenTrace={(traceId) => {
                  addTraceId(traceId);
                  setView("traces");
                }}
              />
              {currentView}
            </>
          ) : null}
        </main>
      </div>

    </div>
  );
}
