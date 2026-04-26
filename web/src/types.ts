export type WorkerPhase = "stopped" | "stopping" | "fast_bootstrap" | "steady" | string;

export type WorkerStatus = {
  running: boolean;
  stopping: boolean;
  phase: WorkerPhase;
  started_at: string | null;
  account_count: number;
  active_count: number;
  manual_busy: boolean;
  code_busy: boolean;
};

export type SessionEarned = {
  gold: number;
  tickets: number;
  xp: number;
};

export type AccountRow = {
  label: string;
  energy: number;
  gold: number;
  gold_estimated_usd: number | null;
  status: string;
  last_error: string;
  last_move_at: string;
  regen_deadline_iso: string | null;
  daily_claim_rewards_text: string;
  daily_checkin_deadline_iso: string | null;
  daily_checkin_streak: number;
  daily_checkin_streak_total: number;
  season_rewards_text: string;
  proxy_cell: string;
  proxy_tooltip: string;
  session_earned?: SessionEarned;
};

export type LogKind = "info" | "move" | "daily" | "season" | "error" | "fatal";

export type LogEvent = {
  ts: string;
  account_label: string | null;
  message: string;
  kind: LogKind;
};

export type UiConfig = {
  window_title?: string;
  background_mode?: string;
  background_mode_label?: string;
  fast_bootstrap_enabled?: boolean;
  steady_energy_targets?: number[];
  transport_backend?: string;
  quiet_hours_enabled?: boolean;
  daily_move_budget?: number;
  max_moves_per_session?: number;
  gold_micro_divisor?: number;
  gold_estimate_usd_micro_divisor?: number;
  check_task_id?: number;
};

export type Snapshot = {
  worker: WorkerStatus;
  rows: AccountRow[];
  logs: LogEvent[];
  session_earnings: Record<string, SessionEarned>;
  config: UiConfig;
};

export type WsEvent =
  | { type: "snapshot"; seq: number; payload: Snapshot }
  | { type: "rows.updated"; seq: number; payload: { rows: AccountRow[] } }
  | { type: "log.added"; seq: number; payload: { log: LogEvent } }
  | { type: "worker.updated"; seq: number; payload: { worker: WorkerStatus } }
  | { type: "earnings.updated"; seq: number; payload: { label: string; session_earned: SessionEarned } }
  | { type: "config.updated"; seq: number; payload: { config: UiConfig } }
  | { type: string; seq: number; payload: Record<string, unknown> };

