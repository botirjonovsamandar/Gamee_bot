import type { Snapshot } from "./types";

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    ...init
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      detail = data.detail || data.message || detail;
    } catch {
      // keep default detail
    }
    throw new Error(detail);
  }
  return (await res.json()) as T;
}

export function getState(): Promise<Snapshot> {
  return request<Snapshot>("/api/state");
}

export function startWorker(): Promise<{ ok: boolean; message: string }> {
  return request("/api/worker/start", { method: "POST" });
}

export function stopWorker(): Promise<{ ok: boolean; message: string }> {
  return request("/api/worker/stop", { method: "POST" });
}

export function accountAction(
  label: string,
  action: "sync" | "claim-daily" | "play-session" | "enter-draw"
): Promise<{ ok: boolean; message: string }> {
  return request(`/api/accounts/${encodeURIComponent(label)}/${action}`, {
    method: "POST"
  });
}

export function updateProxy(
  label: string,
  proxyUrl: string
): Promise<{ ok: boolean; message: string }> {
  return request(`/api/accounts/${encodeURIComponent(label)}/proxy`, {
    method: "POST",
    body: JSON.stringify({ proxy_url: proxyUrl || null })
  });
}

export function deleteAccount(label: string): Promise<{ ok: boolean; message: string }> {
  return request(`/api/accounts/${encodeURIComponent(label)}`, {
    method: "DELETE"
  });
}

export function submitMassCode(
  code: string,
  taskId?: number | null
): Promise<{ ok: boolean; message: string }> {
  return request("/api/code/mass-submit", {
    method: "POST",
    body: JSON.stringify({ code, task_id: taskId ?? null })
  });
}

export function enterDrawAll(): Promise<{ ok: boolean; message: string }> {
  return request("/api/draw/enter-all", { method: "POST" });
}

export function checkDrawWinners(drawId: number): Promise<{ ok: boolean; message: string }> {
  return request("/api/draw/check-winners", {
    method: "POST",
    body: JSON.stringify({ draw_id: drawId })
  });
}
