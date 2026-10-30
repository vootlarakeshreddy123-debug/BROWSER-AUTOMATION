import { useEffect, useState } from "react";
import { getProviders } from "../services/api";
import type { ProvidersResponse } from "../types";
import { PROVIDER_LABEL } from "../lib/tasks";

const ORDER = ["groq", "gemini", "ollama", "openai", "claude", "grok"];

/** Read-only provider status. Keys live only in backend/.env; this panel only ever sees flags, model names and reasons. */
export default function ProviderPanel() {
  const [resp, setResp] = useState<ProvidersResponse | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = () => getProviders().then((r) => { if (!cancelled) { setResp(r); setError(false); } }).catch(() => { if (!cancelled) setError(true); });
    load();
    const id = window.setInterval(load, 10000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  if (error && !resp) return <section className="panel"><p className="text-sm text-muted">Provider status unavailable (backend not reachable).</p></section>;
  if (!resp) return null;

  return (
    <details className="panel provider-panel">
      <summary className="panel-title cursor-pointer">AI providers · Auto order: {resp.auto_order.map((n) => PROVIDER_LABEL[n] ?? n).join(" → ") || "none configured"}</summary>
      <ul className="mt-3 space-y-2 text-sm">
        {ORDER.map((n) => {
          const p = resp.providers[n];
          if (!p) return null;
          return (
            <li key={n} className="flex flex-wrap items-center gap-2">
              <span className={`status-dot ${p.state === "available" ? "status-dot-on" : ""}`} />
              <b>{PROVIDER_LABEL[n]}</b>
              <span className="text-muted">{p.state === "cooldown" ? "cooling down" : p.state}</span>
              {p.model && <span className="meta-pill">{p.model}</span>}
              {p.may_bill && <span className="meta-pill" title="Hosted provider: usage may be billed. Check your plan; NEXA does not assume any free tier.">may be billed</span>}
              {!p.in_auto_order && p.configured && <span className="meta-pill">not in Auto order</span>}
              {p.reason && <span className="text-xs text-muted">{p.reason}</span>}
            </li>
          );
        })}
      </ul>
      <p className="mt-3 text-xs text-muted">
        Browser execution: {resp.browser_execution_mode ?? "local"}. {resp.note}
      </p>
    </details>
  );
}
