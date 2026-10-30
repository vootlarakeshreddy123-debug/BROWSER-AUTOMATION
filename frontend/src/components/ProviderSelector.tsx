import { useEffect, useState } from "react";
import { getProviders } from "../services/api";
import type { ProviderSelection, ProvidersResponse } from "../types";
import { providerChoices } from "../lib/tasks";

interface Props {
  value: ProviderSelection;
  onChange: (v: ProviderSelection) => void;
  disabled?: boolean;
}

/** AI provider choice. Only providers the backend reports as configured can be picked; no keys are ever handled here. */
export default function ProviderSelector({ value, onChange, disabled }: Props) {
  const [resp, setResp] = useState<ProvidersResponse | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => getProviders().then((r) => { if (!cancelled) setResp(r); }).catch(() => undefined);
    load();
    const id = window.setInterval(load, 15000);
    return () => { cancelled = true; window.clearInterval(id); };
  }, []);

  const choices = providerChoices(resp);
  const current = choices.find((c) => c.value === value);

  // a selected provider that stopped being selectable falls back to Auto rather than failing the next task
  useEffect(() => {
    if (current?.disabled) onChange("auto");
  }, [current?.disabled, onChange]);

  return (
    <label className="provider-select" title={current?.hint || undefined}>
      <span className="text-xs text-muted">AI</span>
      <select value={value} disabled={disabled} onChange={(e) => onChange(e.target.value as ProviderSelection)} aria-label="AI provider">
        {choices.map((c) => (
          <option key={c.value} value={c.value} disabled={c.disabled}>
            {c.label}{c.disabled ? " - not configured" : ""}
          </option>
        ))}
      </select>
    </label>
  );
}
