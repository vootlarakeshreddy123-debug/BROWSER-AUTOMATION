import { useCallback, useRef, useState } from "react";
import { cancelTask, createTask, getTask, respondToTask, runTask, WS_URL } from "../services/api";
import type { ProviderSelection, Task, TaskStatus, WsEvent } from "../types";
import { authHeaders } from "../services/auth";
import { isPaused, isTerminal } from "../lib/tasks";

interface ExecutionState {
  task: Task | null;
  status: TaskStatus | "idle";
  submitError: string | null;
}

const IDLE: ExecutionState = { task: null, status: "idle", submitError: null };

export function useTaskExecution() {
  const [state, setState] = useState<ExecutionState>(IDLE);
  const socketRef = useRef<WebSocket | null>(null);
  const pollRef = useRef<number | null>(null);
  const startedRef = useRef(false);
  const terminalRef = useRef(false);

  const stopPolling = () => {
    if (pollRef.current !== null) window.clearInterval(pollRef.current);
    pollRef.current = null;
  };

  const reset = useCallback(() => {
    socketRef.current?.close();
    socketRef.current = null;
    stopPolling();
    setState(IDLE);
  }, []);

  const applyTask = (task: Task) => {
    setState((prev) => ({
      ...prev,
      task,
      status: task.status,
      submitError: null,
    }));
  };

  const execute = useCallback(async (instruction: string, provider: ProviderSelection = "auto") => {
    socketRef.current?.close();
    socketRef.current = null;
    stopPolling();
    setState({ task: null, status: "idle", submitError: null });

    let taskId: string;
    try {
      const created = await createTask(instruction, provider);
      taskId = created.task_id;
    } catch (err) {
      setState({ task: null, status: "idle", submitError: (err as Error).message });
      return;
    }

    const initialTask: Task = {
      id: taskId,
      instruction,
      status: "created",
      created_at: Date.now() / 1000,
      finished_at: null,
      duration_ms: null,
      plan_source: null,
      actions_count: 0,
      retries: 0,
      final_url: null,
      final_title: null,
      screenshot_base64: null,
      message: null,
      result: null,
      error: null,
      provider,
      provider_used: null,
      pending: null,
      events: [],
      stats: {},
    };
    setState({ task: initialTask, status: "created", submitError: null });

    startedRef.current = false;
    terminalRef.current = false;
    const socket = new WebSocket(`${WS_URL}/api/ws/tasks/${taskId}`);
    socketRef.current = socket;

    const finishFromServer = (task: Task) => {
      applyTask(task);
      if (isTerminal(task.status)) stopPolling();
    };

    const poll = () => {
      getTask(taskId)
        .then(finishFromServer)
        .catch(() => undefined);
    };

    const startRun = () => {
      if (startedRef.current) return;
      startedRef.current = true;
      // The backend starts the runner immediately. HTTP polling is a safety
      // net, not a delayed execution path.
      runTask(taskId)
        .then((task) => {
          // The run endpoint returns immediately with the current task record.
          // Keep it for fallback status polling when a WebSocket is unavailable.
          applyTask(task);
        })
        .catch((err) => {
          setState((prev) => ({ ...prev, submitError: (err as Error).message }));
          poll();
        });
      window.setTimeout(poll, 120);
    };

    socket.onmessage = (evt) => {
      const data: WsEvent = JSON.parse(evt.data);
      if (data.type === "event" && data.event) {
        // Live structured event from the backend recorder. Appended verbatim
        // (deduplicated by seq); the UI derives every stage from these.
        const incoming = data.event;
        setState((prev) => {
          if (!prev.task) return prev;
          const existing = prev.task.events ?? [];
          if (existing.some((e) => e.seq === incoming.seq)) return prev;
          return {
            ...prev,
            task: { ...prev.task, events: [...existing, incoming].sort((a, b) => a.seq - b.seq), stats: data.stats ?? prev.task.stats },
          };
        });
        return;
      }
      setState((prev) => {
        if (!prev.task) return prev;
        const nextStatus: TaskStatus = data.status ?? (prev.status === "idle" ? prev.task.status : prev.status);
        return {
          submitError: null,
          status: nextStatus,
          task: {
            ...prev.task,
            status: nextStatus,
            final_url: data.final_url ?? prev.task.final_url,
            final_title: data.final_title ?? prev.task.final_title,
            screenshot_base64: data.screenshot_base64 ?? prev.task.screenshot_base64,
            message: data.message ?? prev.task.message,
            result: data.result ?? prev.task.result,
            error: data.error ?? prev.task.error,
            // a paused task carries its question; any other status clears it
            pending: isPaused(nextStatus) ? (data.pending ?? prev.task.pending ?? null) : null,
            understanding: data.understanding ?? prev.task.understanding,
            outcome: data.outcome ?? prev.task.outcome,
            verification_status: data.verification_status ?? prev.task.verification_status,
            contract_state: data.contract_state ?? prev.task.contract_state,
            contract: data.contract ?? prev.task.contract,
            persistence: data.persistence ?? prev.task.persistence,
            provider_used: data.provider_used ?? data.stats?.provider_used ?? prev.task.provider_used,
            duration_ms: data.duration_ms ?? prev.task.duration_ms,
            plan_source: (data.plan_source as Task["plan_source"]) ?? prev.task.plan_source,
            retries: data.retries ?? prev.task.retries,
            actions_count: data.actions_count ?? prev.task.actions_count,
            // The terminal payload carries the complete event list + final stats.
            events: data.events && data.events.length ? data.events : prev.task.events,
            stats: data.stats ?? prev.task.stats,
          },
        };
      });
      // "result" with a paused status is NOT the end: the task is waiting for the user, so the socket stays open
      if (data.type === "result" && !(data.status && isPaused(data.status))) {
        terminalRef.current = true;
        stopPolling();
        socket.close();
      }
    };

    socket.onopen = () => {
      // The token goes in the first message, never in the URL (URLs end up in logs).
      authHeaders()
        .then((h) => { if (h.Authorization) socket.send(JSON.stringify({ type: "auth", token: h.Authorization.replace(/^Bearer /, "") })); })
        .catch(() => undefined)
        .finally(startRun);
    };
    socket.onerror = () => {
      // The backend can still run the task. Polling keeps the UI useful if a
      // local browser/proxy drops WebSocket support.
      startRun();
      if (pollRef.current === null) {
        poll();
        pollRef.current = window.setInterval(poll, 500);
      }
    };
    socket.onclose = () => {
      if (startedRef.current && !terminalRef.current) {
        if (pollRef.current === null) {
          pollRef.current = window.setInterval(poll, 500);
        }
      }
    };
  }, []);

  const cancel = useCallback(async () => {
    if (!state.task) return;
    try {
      const updated = await cancelTask(state.task.id);
      applyTask(updated);
      terminalRef.current = true;
      stopPolling();
      socketRef.current?.close();
    } catch (err) {
      setState((prev) => ({ ...prev, submitError: (err as Error).message }));
    }
  }, [state.task]);

  /** Approve/decline a confirmation, or answer a question. The run resumes in the same live tab. */
  const respond = useCallback(async (reply: { approve?: boolean; answer?: string }) => {
    if (!state.task) return;
    try {
      const updated = await respondToTask(state.task.id, reply);
      applyTask(updated);
      if (isTerminal(updated.status)) { terminalRef.current = true; stopPolling(); socketRef.current?.close(); }
      else if (pollRef.current === null) pollRef.current = window.setInterval(() => { getTask(updated.id).then((t) => { applyTask(t); if (isTerminal(t.status)) stopPolling(); }).catch(() => undefined); }, 700);
    } catch (err) {
      setState((prev) => ({ ...prev, submitError: (err as Error).message }));
    }
  }, [state.task]);

  return { ...state, execute, cancel, respond, reset };
}
