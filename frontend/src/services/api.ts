import { authHeaders } from "./auth";
import type { BrowserState, HealthStatus, PersistedEvent, PersistedTask, ProvidersResponse, ProviderSelection, Task, TaskStatus } from "../types";

export const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8000";
export const WS_URL = API_URL.replace(/^http/, "ws");

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${res.status})`);
  }
  return res.json() as Promise<T>;
}

async function call<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = { ...(init.headers as Record<string, string> | undefined), ...(await authHeaders()) };
  return json<T>(await fetch(`${API_URL}${path}`, { ...init, headers }));
}

export async function createTask(instruction: string, provider: ProviderSelection = "auto"): Promise<{ task_id: string; status: TaskStatus }> {
  return call(`/api/tasks`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ instruction, provider }) });
}

/** Answer a paused task: approve/decline a confirmation, or give the missing detail. */
export async function respondToTask(taskId: string, reply: { approve?: boolean; answer?: string }): Promise<Task> {
  return call(`/api/tasks/${taskId}/respond`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(reply) });
}

export async function getProviders(): Promise<ProvidersResponse> {
  return call(`/api/providers`);
}

export async function runTask(taskId: string): Promise<Task> {
  return call(`/api/tasks/${taskId}/run`, { method: "POST" });
}

export async function cancelTask(taskId: string): Promise<Task> {
  return call(`/api/tasks/${taskId}/cancel`, { method: "POST" });
}

export async function getTask(taskId: string): Promise<Task> {
  return call(`/api/tasks/${taskId}`);
}

export async function getHistory(limit = 50): Promise<Task[]> {
  return call(`/api/history?limit=${limit}`);
}

export async function getHealth(): Promise<HealthStatus> {
  return json(await fetch(`${API_URL}/api/health`));
}

export async function getBrowserState(): Promise<BrowserState> {
  return call(`/api/browser/state`);
}

export async function focusBrowser(): Promise<{ focused: boolean; live_tab_open: boolean }> {
  return call(`/api/browser/focus`, { method: "POST" });
}

export const getPersistedTasks = (limit = 50): Promise<PersistedTask[]> => call(`/api/persisted/tasks?limit=${limit}`);
export const getPersistedTask = (id: string): Promise<PersistedTask> => call(`/api/persisted/tasks/${encodeURIComponent(id)}`);
export const getPersistedEvents = (id: string): Promise<PersistedEvent[]> => call(`/api/persisted/tasks/${encodeURIComponent(id)}/events`);
export const retryPersistence = (): Promise<{ retried_ok: number }> => call(`/api/persisted/retry`, { method: "POST" });
