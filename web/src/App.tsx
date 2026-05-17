import {
  AlertTriangle,
  CirclePause,
  Dice5,
  Gift,
  Loader2,
  Moon,
  Play,
  PlugZap,
  RefreshCw,
  Search,
  Square,
  Sun,
  Trophy,
  Trash2,
  Wifi,
  WifiOff
} from "lucide-react";
import { memo, useCallback, useEffect, useMemo, useState } from "react";
import {
  accountAction,
  checkDrawWinners,
  deleteAccount,
  enterDrawAll,
  getState,
  startWorker,
  stopWorker,
  submitMassCode,
  updateProxy
} from "./api";
import { useWebSocketEvents } from "./hooks/useWebSocketEvents";
import type { AccountRow, LogEvent, LogKind, Snapshot, UiConfig, WorkerStatus, WsEvent } from "./types";

const emptyWorker: WorkerStatus = {
  running: false,
  stopping: false,
  phase: "stopped",
  started_at: null,
  account_count: 0,
  active_count: 0,
  manual_busy: false,
  code_busy: false,
  draw_busy: false
};

type ThemeMode = "light" | "dark";
const THEME_STORAGE_KEY = "gamee-web-theme";

const kindLabels: Record<LogKind | "all", string> = {
  all: "Все",
  info: "Info",
  move: "Ходы",
  daily: "Daily",
  season: "Season",
  error: "Ошибки",
  fatal: "Fatal"
};

function formatUsd(v: number): string {
  if (!Number.isFinite(v) || v <= 0) return "$0";
  return `$${v.toFixed(8).replace(/0+$/, "").replace(/\.$/, "")}`;
}

function formatNumber(v: number | null | undefined): string {
  return new Intl.NumberFormat("ru-RU").format(Number(v || 0));
}

function formatCountdown(iso: string | null | undefined, nowMs: number): string {
  if (!iso) return "";
  const ts = Date.parse(iso);
  if (!Number.isFinite(ts)) return "";
  const diff = Math.max(0, ts - nowMs);
  if (diff <= 0) return "готово";
  const total = Math.ceil(diff / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (h > 0) return `${h}ч ${String(m).padStart(2, "0")}м`;
  if (m > 0) return `${m}м ${String(s).padStart(2, "0")}с`;
  return `${s}с`;
}

function phaseLabel(worker: WorkerStatus): string {
  if (worker.draw_busy && worker.phase === "draw_winners") return "Draw winners выполняется";
  if (worker.draw_busy) return "Draw XP выполняется";
  if (worker.stopping) return "останавливается";
  if (!worker.running) return "остановлено";
  if (worker.phase === "fast_bootstrap") return "быстрый первый проход";
  if (worker.phase === "steady") return "ожидание регена";
  return worker.phase || "запущено";
}

function statusTone(status: string): string {
  const s = status.toLowerCase();
  if (s.includes("ошиб") || s.includes("429") || s.includes("cloudflare")) {
    return "bg-rose-50 text-rose-700 ring-rose-200";
  }
  if (s.includes("ожид") || s.includes("сон") || s.includes("regen")) {
    return "bg-sky-50 text-sky-700 ring-sky-200";
  }
  if (s.includes("ход") || s.includes("награ") || s.includes("синх") || s.includes("перв")) {
    return "bg-emerald-50 text-emerald-700 ring-emerald-200";
  }
  return "bg-slate-100 text-slate-700 ring-slate-200";
}

function socketBadge(state: string) {
  if (state === "online") {
    return (
      <span className="inline-flex items-center gap-2 rounded-md bg-emerald-50 px-2.5 py-1 text-xs font-semibold text-emerald-700 ring-1 ring-emerald-200">
        <Wifi size={14} /> live
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-2 rounded-md bg-amber-50 px-2.5 py-1 text-xs font-semibold text-amber-700 ring-1 ring-amber-200">
      <WifiOff size={14} /> reconnect
    </span>
  );
}

const AccountRowView = memo(function AccountRowView({
  row,
  now,
  selected,
  onSelect
}: {
  row: AccountRow;
  now: number;
  selected: boolean;
  onSelect: (row: AccountRow) => void;
}) {
  const regen = formatCountdown(row.regen_deadline_iso, now);
  const daily = formatCountdown(row.daily_checkin_deadline_iso, now);
  const earned = row.session_earned || { gold: 0, tickets: 0, xp: 0 };
  return (
    <tr
      className={`cursor-pointer border-b border-slate-200/80 hover:bg-slate-50 ${selected ? "bg-cyan-50/80" : "bg-white"}`}
      onClick={() => onSelect(row)}
    >
      <td className="sticky left-0 z-10 bg-inherit px-3 py-2 font-semibold text-slate-900">{row.label}</td>
      <td className="px-3 py-2 text-slate-600" title={row.proxy_tooltip}>{row.proxy_cell || "—"}</td>
      <td className="px-3 py-2">
        <div className="font-semibold text-slate-900">⚡ {row.energy}</div>
        {regen && <div className="text-xs text-slate-500">+1 {regen}</div>}
      </td>
      <td className="px-3 py-2">
        <div className="font-semibold text-slate-900">💰 {formatNumber(row.gold)}</div>
        {row.gold_estimated_usd ? <div className="text-xs text-slate-500">{formatUsd(row.gold_estimated_usd)}</div> : null}
      </td>
      <td className="px-3 py-2">
        <span className={`inline-flex max-w-[260px] items-center rounded-md px-2 py-1 text-xs font-semibold ring-1 ${statusTone(row.status)}`}>
          <span className="truncate">{row.status || "—"}</span>
        </span>
        {row.last_error && <div className="mt-1 max-w-[260px] truncate text-xs text-rose-600">{row.last_error}</div>}
      </td>
      <td className="px-3 py-2 text-slate-600">{row.last_move_at || "—"}</td>
      <td className="px-3 py-2 text-slate-600">
        <div>{row.daily_claim_rewards_text || "—"}</div>
        {daily && <div className="text-xs text-slate-500">{daily}</div>}
      </td>
      <td className="px-3 py-2 text-slate-600">{row.season_rewards_text || "—"}</td>
      <td className="px-3 py-2 text-slate-700">
        💰 {formatNumber(earned.gold)} · 🎟 {formatNumber(earned.tickets)} · ★ {formatNumber(earned.xp)}
      </td>
    </tr>
  );
});

function AccountDrawer({
  row,
  busy,
  onClose,
  onToast,
  onDeleted
}: {
  row: AccountRow | null;
  busy: boolean;
  onClose: () => void;
  onToast: (msg: string, error?: boolean) => void;
  onDeleted: () => void;
}) {
  const [proxy, setProxy] = useState("");
  const [pending, setPending] = useState(false);

  useEffect(() => {
    setProxy("");
  }, [row?.label]);

  if (!row) return null;

  const run = async (fn: () => Promise<{ message: string }>, after?: () => void) => {
    setPending(true);
    try {
      const res = await fn();
      onToast(res.message || "OK");
      after?.();
    } catch (e) {
      onToast(e instanceof Error ? e.message : String(e), true);
    } finally {
      setPending(false);
    }
  };

  return (
    <div className="fixed inset-0 z-40 flex justify-end bg-slate-950/25" onClick={onClose}>
      <aside className="h-full w-full max-w-md overflow-y-auto bg-white p-5 shadow-soft" onClick={(e) => e.stopPropagation()}>
        <div className="mb-5 flex items-start justify-between gap-3">
          <div>
            <div className="text-xs font-semibold uppercase text-slate-500">Аккаунт</div>
            <h2 className="mt-1 text-xl font-bold text-slate-950">{row.label}</h2>
          </div>
          <button className="rounded-md border border-slate-200 px-3 py-1.5 text-sm font-semibold text-slate-700 hover:bg-slate-50" onClick={onClose}>
            Закрыть
          </button>
        </div>

        <div className="grid grid-cols-2 gap-3">
          <div className="rounded-lg bg-slate-50 p-3 ring-1 ring-slate-200">
            <div className="text-xs text-slate-500">Энергия</div>
            <div className="mt-1 text-2xl font-bold text-slate-950">{row.energy}</div>
          </div>
          <div className="rounded-lg bg-slate-50 p-3 ring-1 ring-slate-200">
            <div className="text-xs text-slate-500">Золото</div>
            <div className="mt-1 text-2xl font-bold text-slate-950">{formatNumber(row.gold)}</div>
          </div>
        </div>

        <div className="mt-5 space-y-2">
          <button disabled={busy || pending} className="action-btn" onClick={() => run(() => accountAction(row.label, "sync"))}>
            <RefreshCw size={16} /> Sync now
          </button>
          <button disabled={busy || pending} className="action-btn" onClick={() => run(() => accountAction(row.label, "claim-daily"))}>
            <Gift size={16} /> Claim daily
          </button>
          <button disabled={busy || pending} className="action-btn" onClick={() => run(() => accountAction(row.label, "play-session"))}>
            <Dice5 size={16} /> Play session
          </button>
          <button disabled={busy || pending} className="action-btn" onClick={() => run(() => accountAction(row.label, "enter-draw"))}>
            <Trophy size={16} /> Draw XP
          </button>
        </div>

        <div className="mt-6">
          <label className="text-sm font-semibold text-slate-700">Прокси</label>
          <input
            className="mt-2 w-full rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100"
            value={proxy}
            onChange={(e) => setProxy(e.target.value)}
            placeholder="Пусто — убрать прокси"
          />
          <button
            disabled={pending}
            className="mt-2 inline-flex w-full items-center justify-center gap-2 rounded-md bg-cyan-700 px-3 py-2 text-sm font-semibold text-white hover:bg-cyan-800 disabled:cursor-not-allowed disabled:opacity-50"
            onClick={() => run(() => updateProxy(row.label, proxy))}
          >
            <PlugZap size={16} /> Сохранить прокси
          </button>
        </div>

        <div className="mt-6 rounded-lg bg-rose-50 p-3 ring-1 ring-rose-200">
          <button
            disabled={pending}
            className="inline-flex w-full items-center justify-center gap-2 rounded-md bg-rose-700 px-3 py-2 text-sm font-semibold text-white hover:bg-rose-800 disabled:cursor-not-allowed disabled:opacity-50"
            onClick={() => run(() => deleteAccount(row.label), () => { onDeleted(); onClose(); })}
          >
            <Trash2 size={16} /> Удалить аккаунт
          </button>
        </div>
      </aside>
    </div>
  );
}

export default function App() {
  const [worker, setWorker] = useState<WorkerStatus>(emptyWorker);
  const [rows, setRows] = useState<AccountRow[]>([]);
  const [logs, setLogs] = useState<LogEvent[]>([]);
  const [config, setConfig] = useState<UiConfig>({});
  const [selected, setSelected] = useState<AccountRow | null>(null);
  const [query, setQuery] = useState("");
  const [logQuery, setLogQuery] = useState("");
  const [logKind, setLogKind] = useState<LogKind | "all">("all");
  const [toast, setToast] = useState<{ text: string; error: boolean } | null>(null);
  const [massCode, setMassCode] = useState("");
  const [taskId, setTaskId] = useState("");
  const [drawCheckId, setDrawCheckId] = useState("");
  const [now, setNow] = useState(Date.now());
  const [theme, setTheme] = useState<ThemeMode>(() => {
    const saved = window.localStorage.getItem(THEME_STORAGE_KEY);
    return saved === "light" || saved === "dark" ? saved : "dark";
  });

  const showToast = useCallback((text: string, error = false) => {
    setToast({ text, error });
    window.setTimeout(() => setToast(null), 4500);
  }, []);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  const applySnapshot = useCallback((snap: Snapshot) => {
    setWorker(snap.worker);
    setRows(snap.rows || []);
    setLogs(snap.logs || []);
    setConfig(snap.config || {});
  }, []);

  useEffect(() => {
    getState().then(applySnapshot).catch((e) => showToast(e.message, true));
  }, [applySnapshot, showToast]);

  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);

  const handleEvent = useCallback((event: WsEvent) => {
    if (event.type === "snapshot") {
      applySnapshot(event.payload as Snapshot);
      return;
    }
    if (event.type === "rows.updated" && "rows" in event.payload) {
      setRows(event.payload.rows as AccountRow[]);
      return;
    }
    if (event.type === "log.added" && "log" in event.payload) {
      setLogs((cur) => [...cur.slice(-1999), event.payload.log as LogEvent]);
      return;
    }
    if (event.type === "worker.updated" && "worker" in event.payload) {
      setWorker(event.payload.worker as WorkerStatus);
      return;
    }
    if (event.type === "config.updated" && "config" in event.payload) {
      setConfig(event.payload.config as UiConfig);
      return;
    }
    if (event.type === "earnings.updated") {
      const payload = event.payload as { label?: string; session_earned?: AccountRow["session_earned"] };
      setRows((cur) =>
        cur.map((row) =>
          row.label === payload.label ? { ...row, session_earned: payload.session_earned } : row
        )
      );
    }
  }, [applySnapshot]);

  const socket = useWebSocketEvents(handleEvent);

  const selectedRow = useMemo(
    () => rows.find((row) => row.label === selected?.label) || selected,
    [rows, selected]
  );

  const filteredRows = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return rows;
    return rows.filter((row) =>
      `${row.label} ${row.status} ${row.proxy_cell} ${row.last_error}`.toLowerCase().includes(q)
    );
  }, [rows, query]);

  const filteredLogs = useMemo(() => {
    const q = logQuery.trim().toLowerCase();
    return logs.filter((log) => {
      if (logKind !== "all" && log.kind !== logKind) return false;
      if (!q) return true;
      return `${log.account_label || ""} ${log.message}`.toLowerCase().includes(q);
    });
  }, [logs, logKind, logQuery]);

  const totals = useMemo(() => {
    return rows.reduce(
      (acc, row) => {
        acc.gold += Number(row.gold || 0);
        acc.usd += Number(row.gold_estimated_usd || 0);
        acc.energy += Number(row.energy || 0);
        return acc;
      },
      { gold: 0, usd: 0, energy: 0 }
    );
  }, [rows]);

  const runTopAction = async (fn: () => Promise<{ message: string }>) => {
    try {
      const res = await fn();
      showToast(res.message);
    } catch (e) {
      showToast(e instanceof Error ? e.message : String(e), true);
    }
  };

  const submitCode = async () => {
    try {
      const tid = taskId.trim() ? Number(taskId.trim()) : null;
      const res = await submitMassCode(massCode, tid);
      showToast(res.message);
      setMassCode("");
    } catch (e) {
      showToast(e instanceof Error ? e.message : String(e), true);
    }
  };

  const submitDrawWinnerCheck = async () => {
    try {
      const did = Number(drawCheckId.trim());
      if (!Number.isInteger(did) || did <= 0) {
        showToast("drawId must be positive", true);
        return;
      }
      const res = await checkDrawWinners(did);
      showToast(res.message);
    } catch (e) {
      showToast(e instanceof Error ? e.message : String(e), true);
    }
  };

  const topBusy = worker.running || worker.manual_busy || worker.code_busy || !!worker.draw_busy;

  return (
    <div className="min-h-screen bg-panel text-slate-900">
      <header className="border-b border-line bg-white">
        <div className="mx-auto flex max-w-[1800px] flex-col gap-4 px-4 py-4 lg:px-6">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <div>
              <h1 className="text-2xl font-bold tracking-normal text-slate-950">{config.window_title || "Gamee Bot"}</h1>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-slate-600">
                {socketBadge(socket)}
                <span className="rounded-md bg-slate-100 px-2.5 py-1 font-semibold text-slate-700 ring-1 ring-slate-200">
                  {phaseLabel(worker)}
                </span>
                <span>{config.background_mode_label || "режим не загружен"}</span>
                <span>backend: {config.transport_backend || "—"}</span>
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              <button
                className="top-btn border border-slate-300 bg-white text-slate-800 hover:bg-slate-50"
                onClick={() => setTheme((cur) => (cur === "dark" ? "light" : "dark"))}
                title={theme === "dark" ? "Переключить на белый фон" : "Переключить на чёрный фон"}
              >
                {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
                {theme === "dark" ? "Белый фон" : "Чёрный фон"}
              </button>
              <button
                className="top-btn bg-emerald-700 text-white hover:bg-emerald-800"
                disabled={topBusy}
                onClick={() => runTopAction(startWorker)}
              >
                <Play size={16} /> Запустить всё
              </button>
              <button
                className="top-btn bg-cyan-700 text-white hover:bg-cyan-800"
                disabled={topBusy}
                onClick={() => runTopAction(enterDrawAll)}
              >
                {worker.draw_busy ? <Loader2 className="animate-spin" size={16} /> : <Trophy size={16} />}
                Draw XP
              </button>
              <button
                className="top-btn bg-slate-800 text-white hover:bg-slate-950"
                disabled={!worker.running || worker.stopping}
                onClick={() => runTopAction(stopWorker)}
              >
                <Square size={16} /> Остановить всё
              </button>
            </div>
          </div>

          <div className="grid gap-3 md:grid-cols-4">
            <div className="metric">
              <div className="metric-label">Аккаунты</div>
              <div className="metric-value">{worker.account_count}</div>
              <div className="metric-sub">активно: {worker.active_count}</div>
            </div>
            <div className="metric">
              <div className="metric-label">Энергия</div>
              <div className="metric-value">{formatNumber(totals.energy)}</div>
              <div className="metric-sub">суммарно по таблице</div>
            </div>
            <div className="metric">
              <div className="metric-label">Gold</div>
              <div className="metric-value">{formatNumber(totals.gold)}</div>
              <div className="metric-sub">{formatUsd(totals.usd)}</div>
            </div>
            <div className="metric">
              <div className="metric-label">Настройки</div>
              <div className="metric-value text-lg">
                {config.fast_bootstrap_enabled ? "fast bootstrap" : "steady only"}
              </div>
              <div className="metric-sub">targets: {(config.steady_energy_targets || []).join(", ") || "—"}</div>
            </div>
          </div>
        </div>
      </header>

      <main className="mx-auto grid max-w-[1800px] items-start gap-4 px-4 py-4 lg:grid-cols-[minmax(0,1fr)_420px] lg:px-6">
        <section className="min-w-0 overflow-hidden rounded-lg bg-white shadow-soft ring-1 ring-slate-200">
          <div className="flex flex-col gap-3 border-b border-slate-200 p-3 md:flex-row md:items-center md:justify-between">
            <div className="relative max-w-md flex-1">
              <Search className="pointer-events-none absolute left-3 top-2.5 text-slate-400" size={17} />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                className="w-full rounded-md border border-slate-300 py-2 pl-9 pr-3 text-sm outline-none focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100"
                placeholder="Фильтр аккаунтов, статуса, прокси"
              />
            </div>
            <div className="text-sm font-semibold text-slate-600">{filteredRows.length} / {rows.length}</div>
          </div>
          <div className="max-h-[calc(100vh-330px)] min-h-[360px] overflow-auto">
            <table className="w-full min-w-[1180px] border-separate border-spacing-0 text-left text-sm">
              <thead className="sticky top-0 z-20 bg-slate-100 text-xs uppercase text-slate-500">
                <tr>
                  <th className="sticky left-0 z-30 bg-slate-100 px-3 py-2">Аккаунт</th>
                  <th className="px-3 py-2">Прокси</th>
                  <th className="px-3 py-2">Энергия</th>
                  <th className="px-3 py-2">Золото</th>
                  <th className="px-3 py-2">Статус</th>
                  <th className="px-3 py-2">Последний ход</th>
                  <th className="px-3 py-2">Daily</th>
                  <th className="px-3 py-2">Season</th>
                  <th className="px-3 py-2">Сессия</th>
                </tr>
              </thead>
              <tbody>
                {filteredRows.map((row) => (
                  <AccountRowView
                    key={row.label}
                    row={row}
                    now={now}
                    selected={row.label === selectedRow?.label}
                    onSelect={setSelected}
                  />
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <aside className="flex h-[640px] max-h-[calc(100vh-2rem)] min-h-[420px] flex-col overflow-hidden rounded-lg bg-white shadow-soft ring-1 ring-slate-200 lg:sticky lg:top-4">
          <div className="border-b border-slate-200 p-3">
            <div className="mb-3 flex items-center justify-between">
              <h2 className="font-bold text-slate-950">Логи</h2>
              <span className="text-xs font-semibold text-slate-500">{logs.length} строк</span>
            </div>
            <div className="grid gap-2">
              <input
                value={logQuery}
                onChange={(e) => setLogQuery(e.target.value)}
                className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100"
                placeholder="Фильтр логов"
              />
              <div className="flex flex-wrap gap-1">
                {(Object.keys(kindLabels) as Array<LogKind | "all">).map((kind) => (
                  <button
                    key={kind}
                    className={`rounded-md px-2.5 py-1 text-xs font-semibold ring-1 ${
                      logKind === kind
                        ? "bg-cyan-700 text-white ring-cyan-700"
                        : "bg-white text-slate-600 ring-slate-200 hover:bg-slate-50"
                    }`}
                    onClick={() => setLogKind(kind)}
                  >
                    {kindLabels[kind]}
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain p-3">
            <div className="space-y-2">
              {filteredLogs.slice(-500).reverse().map((log, idx) => (
                <div key={`${log.ts}-${idx}`} className={`log-line log-${log.kind}`}>
                  <div className="flex items-center gap-2 text-[11px] font-semibold uppercase">
                    <span>{new Date(log.ts).toLocaleTimeString("ru-RU")}</span>
                    <span>{kindLabels[log.kind]}</span>
                    {log.account_label && <span>{log.account_label}</span>}
                  </div>
                  <div className="mt-1 whitespace-pre-wrap break-words text-sm">{log.message}</div>
                </div>
              ))}
              {!filteredLogs.length && (
                <div className="flex h-36 items-center justify-center text-sm text-slate-500">Логов по фильтру нет</div>
              )}
            </div>
          </div>

          <div className="border-t border-slate-200 p-3">
            <div className="mb-2 flex items-center gap-2 text-sm font-bold text-slate-900">
              <Gift size={16} /> Промокод всем
            </div>
            <div className="grid grid-cols-[1fr_110px] gap-2">
              <input
                value={massCode}
                onChange={(e) => setMassCode(e.target.value)}
                className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100"
                placeholder="Код"
              />
              <input
                value={taskId}
                onChange={(e) => setTaskId(e.target.value)}
                className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100"
                placeholder={`${config.check_task_id || 2950}`}
              />
            </div>
            <button
              className="mt-2 inline-flex w-full items-center justify-center gap-2 rounded-md bg-slate-800 px-3 py-2 text-sm font-semibold text-white hover:bg-slate-950 disabled:cursor-not-allowed disabled:opacity-50"
              disabled={!massCode.trim() || topBusy}
              onClick={submitCode}
            >
              {worker.code_busy ? <Loader2 className="animate-spin" size={16} /> : <Gift size={16} />} Отправить код
            </button>
          </div>

          <div className="border-t border-slate-200 p-3">
            <div className="mb-2 flex items-center gap-2 text-sm font-bold text-slate-900">
              <Trophy size={16} /> Draw winners
            </div>
            <div className="grid grid-cols-[1fr_150px] gap-2">
              <input
                value={drawCheckId}
                onChange={(e) => setDrawCheckId(e.target.value)}
                className="rounded-md border border-slate-300 px-3 py-2 text-sm outline-none focus:border-cyan-500 focus:ring-2 focus:ring-cyan-100"
                placeholder="drawId"
              />
              <button
                className="inline-flex items-center justify-center gap-2 rounded-md bg-cyan-700 px-3 py-2 text-sm font-semibold text-white hover:bg-cyan-800 disabled:cursor-not-allowed disabled:opacity-50"
                disabled={!drawCheckId.trim() || topBusy}
                onClick={submitDrawWinnerCheck}
              >
                {worker.draw_busy && worker.phase === "draw_winners" ? <Loader2 className="animate-spin" size={16} /> : <Trophy size={16} />}
                Check
              </button>
            </div>
          </div>
        </aside>
      </main>

      <AccountDrawer
        row={selectedRow}
        busy={topBusy}
        onClose={() => setSelected(null)}
        onToast={showToast}
        onDeleted={() => setSelected(null)}
      />

      {toast && (
        <div className={`fixed bottom-4 left-1/2 z-50 flex -translate-x-1/2 items-center gap-2 rounded-lg px-4 py-3 text-sm font-semibold shadow-soft ring-1 ${
          toast.error ? "bg-rose-50 text-rose-800 ring-rose-200" : "bg-emerald-50 text-emerald-800 ring-emerald-200"
        }`}>
          {toast.error ? <AlertTriangle size={16} /> : <CirclePause size={16} />}
          {toast.text}
        </div>
      )}
    </div>
  );
}
