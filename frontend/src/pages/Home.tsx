import { motion } from "framer-motion";
import { ArrowDown, Command, Keyboard, ShieldCheck, Zap } from "lucide-react";
import { useTaskExecution } from "../hooks/useTaskExecution";
import ExecutionView from "../components/ExecutionView";
import TaskInput from "../components/TaskInput";
import ProviderPanel from "../components/ProviderPanel";
import { isBusy } from "../lib/tasks";

const WORDS = ["OPEN", "SEARCH", "NAVIGATE", "ACT"];

export default function Home() {
  const { task, status, submitError, execute, cancel, respond, reset } = useTaskExecution();
  const busy = isBusy(status);

  return (
    <main className="mx-auto max-w-6xl px-5 pb-16 pt-10 sm:px-8 lg:px-10">
      {!task && (
        <motion.section
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          transition={{ duration: 0.4 }}
          className="hero-section"
        >
          <div className="hero-eyebrow"><span className="hero-pulse" />REAL BROWSER AUTOMATION</div>
          <div className="hero-word-stage" aria-hidden="true">
            {WORDS.map((word, index) => (
              <motion.span
                key={word}
                initial={{ opacity: 0, y: index % 2 ? -45 : 45, rotateX: index % 2 ? -35 : 35 }}
                animate={{ opacity: index === 0 ? 1 : 0, y: 0, rotateX: 0 }}
                transition={{ duration: 0.65, delay: index * 0.55, repeat: Infinity, repeatType: "reverse", repeatDelay: 2.2 }}
                className="hero-word"
              >{word}</motion.span>
            ))}
          </div>
          <h1 className="hero-title">Tell it what to do.<br /><span>Take over when you want.</span></h1>
          <p className="hero-subtitle">Natural language goes in. A real, maximized Chrome tab does the work — then stays open and interactive for you.</p>
        </motion.section>
      )}

      <motion.section layout className={task ? "task-area task-area-active" : "task-area"}>
        <TaskInput onSubmit={execute} busy={busy} onStop={cancel} onClear={reset} />
        {submitError && <div className="submit-error">{submitError}</div>}
      </motion.section>

      {!task && <div className="mt-6"><ProviderPanel /></div>}

      {!task && (
        <motion.div initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: 0.25 }} className="feature-strip">
          <div><Zap className="h-4 w-4 text-signal" /><span>Fast direct navigation</span></div>
          <div><Command className="h-4 w-4 text-signal" /><span>Natural-language tasks</span></div>
          <div><Keyboard className="h-4 w-4 text-signal" /><span>Type or speak</span></div>
          <div><ShieldCheck className="h-4 w-4 text-signal" /><span>User stays in control</span></div>
        </motion.div>
      )}

      {task && (
        <>
          <ExecutionView task={task} onCancel={cancel} onRespond={respond} />
          {!busy && (
            <button type="button" onClick={reset} className="new-task-button">
              <ArrowDown className="h-4 w-4" /> Start another task
            </button>
          )}
        </>
      )}
    </main>
  );
}
