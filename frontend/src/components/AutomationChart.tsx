import type { Task } from "../types";
import { buildSteps, isStats, statValue, type StepRow } from "../lib/execution";

const TONE: Record<string, string> = {
  ok: "chip-ok",
  warn: "chip-warn",
  bad: "chip-bad",
  info: "chip-info",
};

function Chip({ c }: { c: StepRow["chips"][number] }) {
  return (
    <span className={`flow-chip ${TONE[c.tone]}`} title={c.detail || undefined}>
      {c.label}
    </span>
  );
}

/**
 * Automation chart: one column per adaptive step, built ONLY from recorded backend events.
 * (Task → Router → Browser → [Observe → Planner → Action → Progress → Verify → Recovery]* → Result)
 */
export default function AutomationChart({ task }: { task: Task }) {
  const events = task.events ?? [];
  if (!events.length) return null;
  const { pre, steps, end } = buildSteps(events);
  const stats = isStats(task.stats) ? task.stats : null;

  return (
    <section className="panel" aria-label="Automation chart">
      <h3 className="panel-title">Automation chart</h3>
      <div className="flow-scroll">
        <div className="flow-col">
          <div className="flow-head">Start</div>
          {pre.map((c, i) => <Chip key={i} c={c} />)}
        </div>
        {steps.map((s) => (
          <div className="flow-col" key={s.step}>
            <div className="flow-head">Step {s.step}</div>
            {s.chips.map((c, i) => <Chip key={i} c={c} />)}
          </div>
        ))}
        {end.length > 0 && (
          <div className="flow-col">
            <div className="flow-head">Result</div>
            {end.map((c, i) => <Chip key={i} c={c} />)}
          </div>
        )}
      </div>

      <dl className="stat-grid">
        <div><dt>Total time</dt><dd>{stats ? `${(stats.elapsed_ms / 1000).toFixed(1)}s` : "—"}</dd></div>
        <div><dt>Browser actions</dt><dd>{stats ? stats.browser_actions : "—"}</dd></div>
        <div><dt>AI calls</dt><dd>{stats ? stats.ai_calls : "—"}</dd></div>
        <div><dt>Gemini calls</dt><dd>{stats ? stats.gemini_calls : "—"}</dd></div>
        <div><dt>Ollama calls</dt><dd>{stats ? stats.ollama_calls : "—"}</dd></div>
        <div><dt>Avg AI latency</dt><dd>{stats ? statValue(stats.avg_ai_latency_ms, " ms") : "—"}</dd></div>
        <div><dt>Avg browser latency</dt><dd>{stats ? statValue(stats.avg_browser_latency_ms, " ms") : "—"}</dd></div>
        <div><dt>Recoveries</dt><dd>{stats ? stats.recoveries : "—"}</dd></div>
        <div><dt>Verifications</dt><dd>{stats ? stats.verifications : "—"}</dd></div>
        <div><dt>False “done” claims</dt><dd>{stats ? stats.false_done_claims : "—"}</dd></div>
        <div className="stat-wide">
          <dt>Progress verdicts</dt>
          <dd>{stats && Object.keys(stats.progress).length ? Object.entries(stats.progress).map(([k, v]) => `${k}: ${v}`).join(" · ") : "—"}</dd>
        </div>
      </dl>
      <p className="panel-note">Values are computed by the backend from recorded events. “—” means not measured.</p>
    </section>
  );
}
