import { useEffect, useState } from "react";
import { ExternalLink, Globe2, MonitorUp, MousePointer2, RefreshCw } from "lucide-react";
import type { BrowserState, Task } from "../types";
import { focusBrowser, getBrowserState } from "../services/api";

export default function BrowserPreview({ task }: { task: Task }) {
  const [browser, setBrowser] = useState<BrowserState | null>(null);
  const [focusing, setFocusing] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const state = await getBrowserState();
        if (!cancelled) setBrowser(state);
      } catch {
        if (!cancelled) setBrowser(null);
      }
    };
    load();
    const id = window.setInterval(load, 1500);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [task.id, task.status]);

  const focus = async () => {
    setFocusing(true);
    try {
      const result = await focusBrowser();
      if (result.focused) setBrowser((prev) => prev ? { ...prev, live_tab_open: true } : prev);
    } finally {
      setFocusing(false);
    }
  };

  const url = browser?.url || task.final_url;
  const live = browser?.live_tab_open;

  return (
    <section className="live-browser-card">
      <div className="live-browser-glow" />
      <div className="relative flex flex-col gap-5 p-6 sm:p-7">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <div className="live-browser-icon"><MonitorUp className="h-5 w-5" /></div>
            <div>
              <p className="text-sm font-semibold text-text">Live browser</p>
              <p className="text-xs text-muted">The real Chrome window opens automatically and stays interactive.</p>
            </div>
          </div>
          <span className={`live-state ${live ? "live-state-on" : ""}`}>
            <span className="live-dot" />
            {live ? "Live + interactive" : browser?.ready ? "Browser ready" : "Starting browser"}
          </span>
        </div>

        <div className="browser-url-bar">
          <Globe2 className="h-4 w-4 shrink-0 text-muted" />
          <span className="truncate">{url || "Waiting for the browser…"}</span>
          {url && (
            <button type="button" onClick={focus} disabled={focusing || !live} className="url-action" title="Focus the live Chrome tab">
              {focusing ? <RefreshCw className="h-4 w-4 animate-spin" /> : <ExternalLink className="h-4 w-4" />}
            </button>
          )}
        </div>

        <div className="browser-handoff">
          <div className="handoff-illustration">
            <div className="browser-window-mock">
              <div className="mock-topbar"><span /><span /><span /></div>
              <div className="mock-lines"><i /><i /><i /><i /></div>
              <MousePointer2 className="mock-pointer" />
            </div>
          </div>
          <div className="min-w-0">
            <p className="text-base font-medium text-text">Your browser is not a screenshot.</p>
            <p className="mt-1 text-sm leading-6 text-muted">
              {live
                ? "The agent has handed the real Chrome tab back to you. You can click, type, scroll, sign in, and continue the website task yourself."
                : browser?.ready
                  ? "The browser session is ready. Run a task and its new Chrome tab will be selected automatically."
                  : "The browser session is starting. Your Chrome tab will open automatically when the task runs."
              }
            </p>
            <button type="button" onClick={focus} disabled={!live || focusing} className="focus-browser-button">
              <MonitorUp className="h-4 w-4" />
              {focusing ? "Focusing…" : "Focus live browser"}
            </button>
          </div>
        </div>

        {task.status === "failed" && task.error && (
          <div className="browser-note browser-note-error">{task.error}</div>
        )}
        {task.status === "timeout" && (
          <div className="browser-note">Automation timed out, but the live tab was intentionally left open for manual continuation.</div>
        )}
      </div>
    </section>
  );
}
