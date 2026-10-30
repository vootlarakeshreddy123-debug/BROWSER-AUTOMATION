import { useEffect, useState } from "react";
import { Orbit, History as HistoryIcon } from "lucide-react";
import { getHealth } from "../services/api";
import AuthMenu from "./AuthMenu";
import { applyHealthResult, describeStatus, initialStatus, type SystemStatus } from "../lib/status";

interface Props {
  view: "console" | "history" | "architecture";
  onChangeView: (v: "console" | "history" | "architecture") => void;
}

export default function Header({ view, onChangeView }: Props) {
  const [status, setStatus] = useState<SystemStatus>(initialStatus);

  useEffect(() => {
    let cancelled = false;

    const check = () =>
      getHealth()
        .then((h) => {
          if (!cancelled) setStatus((prev) => applyHealthResult(prev, h));
        })
        .catch((e) => {
          if (!cancelled) setStatus((prev) => applyHealthResult(prev, e instanceof Error ? e : new Error(String(e))));
        });

    check();

    const id = window.setInterval(check, 4000);

    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, []);

  return (
    <header className="app-header">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-4 px-5 py-4 sm:px-8 lg:px-10">
        <button
          type="button"
          onClick={() => onChangeView("console")}
          className="brand-button"
          aria-label="Nexa"
        >
          <span className="brand-mark">
            <Orbit className="h-4 w-4" />
          </span>

          <span className="font-mono text-sm font-medium tracking-tight text-text">
            NEXA<span className="text-signal">.</span>
          </span>
        </button>

        <nav className="nav-shell">
          <button
            type="button"
            onClick={() => onChangeView("console")}
            className={`nav-button ${
              view === "console" ? "nav-active" : ""
            }`}
          >
            Console
          </button>

          <button
            type="button"
            onClick={() => onChangeView("history")}
            className={`nav-button ${
              view === "history" ? "nav-active" : ""
            }`}
          >
            <HistoryIcon className="h-3.5 w-3.5" />
            History
          </button>

          <button
            type="button"
            onClick={() => onChangeView("architecture")}
            className={`nav-button ${
              view === "architecture" ? "nav-active" : ""
            }`}
          >
            Architecture
          </button>
        </nav>

        <div className="browser-status" aria-label="System status">
          {describeStatus(status).map((item) => (
            <span key={item.label} className="status-chip" title={item.label}>
              <span className={`status-dot ${item.ok ? "status-dot-on" : ""}`} />
              <span className="hidden sm:inline">{item.label}</span>
            </span>
          ))}
        </div>
        <AuthMenu />
      </div>
    </header>
  );
}