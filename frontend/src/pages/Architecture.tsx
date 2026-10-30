const FLOW = [
  "USER → TASK API → ORCHESTRATOR (deadline · budgets · provider health · events)",
  "ROUTER → deterministic fast path (no AI)   |   adaptive agent loop",
  "OBSERVE live DOM → verify not already complete → PLAN ONE ACTION (Gemini primary, Ollama emergency)",
  "VALIDATE → EXECUTE (Playwright, bounded) → OBSERVE AGAIN → PROGRESS CLASSIFIER",
  "progress → next step   |   no progress → recovery ladder → success or honest failure",
];

const CHANGES: { title: string; old: string; now: string; why: string; result: string }[] = [
  { title: "Repeated Ollama calls", old: "Ollama planned every adaptive step once Gemini was down.", now: "Ollama is an emergency provider with a per-task call and time budget; failures retire it for the task.", why: "Qwen3:4B took ~46–56 s per call.", result: "A slow local model cannot consume the task deadline." },
  { title: "False done loop", old: "Rejected done=true re-asked the same provider with no real limit.", now: "Per-provider/per-task limits, identical-input protection, strategy switch, honest failure.", why: "done=true is never evidence.", result: "Bounded, explained failure." },
  { title: "Deadline awareness", old: "One outer timeout around everything.", now: "TaskBudget: AI/browser/recovery budgets checked before each operation.", why: "AI calls started with no time left.", result: "Graceful timeout with diagnostics." },
  { title: "Multiple actions per response", old: "Up to 2 actions from one AI response.", now: "Exactly one action, then re-observe.", why: "The DOM changes after every action.", result: "Each action is planned on fresh state." },
  { title: "Progress detection", old: "Identical-state counter; any success reset counters.", now: "Before/after classifier with six verdicts.", why: "Playwright success ≠ progress.", result: "Useless actions are detected and stop the task." },
  { title: "Frontend observability", old: "Fixed banner and “1 browser action” fallback.", now: "Live events, provider status, chart and timeline from recorded data.", why: "Debugging needed the exact failure point.", result: "No fake progress." },
];

export default function Architecture() {
  return (
    <div className="mx-auto max-w-4xl space-y-6 px-5 py-8">
      <section className="panel">
        <h3 className="panel-title">Architecture (as implemented)</h3>
        <ol className="space-y-2 font-mono text-xs">
          {FLOW.map((l) => (<li key={l}>{l}</li>))}
        </ol>
        <p className="panel-note">Full description: ARCHITECTURE.md. Provider budgets and fallback order are configured in backend/.env.</p>
      </section>
      <section className="panel">
        <h3 className="panel-title">Changes</h3>
        <div className="space-y-4">
          {CHANGES.map((c) => (
            <article key={c.title} className="text-xs">
              <h4 className="text-sm font-semibold">{c.title}</h4>
              <p><b>OLD</b> {c.old}</p>
              <p><b>NEW</b> {c.now}</p>
              <p><b>WHY</b> {c.why}</p>
              <p><b>RESULT</b> {c.result}</p>
            </article>
          ))}
        </div>
        <p className="panel-note">Complete list with test evidence: CHANGES_ADAPTIVE_FIX.md.</p>
      </section>
    </div>
  );
}
